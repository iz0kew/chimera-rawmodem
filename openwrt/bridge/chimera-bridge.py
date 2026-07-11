#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""chimera-rawmodem — AR9331 serial<->TCP bridge daemon.

Owns the serial link to the ATmega328P modem (/dev/ttyATH0) and exposes it
as a KISS-over-TCP server. This is the single process allowed to touch the
serial port; every consumer (external KISS client, digipeater daemon, iGate
daemon, Reticulum host interface) connects to the TCP port instead.

- CMD_DATA (0x00) frames pass through in both directions, unmodified —
  except in TNC mode with `kiss_text_translation` on (default), where the
  bridge converts between the LoRa APRS text format used on air (OE header
  0x3C 0xFF 0x01 + TNC2 ASCII) and the binary AX.25 UI frames KISS clients
  (PinPoint, Xastir...) expect. See the conversion section below.
- CMD_SIGREPORT (0x07) frames from the modem are logged, not forwarded
  (KISS clients would misinterpret them).
- At startup the radio profile for the active mode (config `mode:`) is
  pushed to the modem as CMD_SET* frames.
- Radio/hardware commands arriving from TCP clients are dropped unless
  `allow_client_config` is enabled.

Stdlib only, runs on Python 2.7 and 3.x: the Dragino factory firmware
(OpenWrt Chaos Calmer 15.05.1) ships Python 2.7 and its feeds have no
python3 package, so 2.7 compatibility is a deployment requirement, not a
choice (see docs/hardware-notes.md). Serial access via termios, no
pyserial dependency. Wire protocol spec: docs/architecture.md.
"""

import errno
import math
import os
import re
import select
import socket
import struct
import sys
import termios
import time
from collections import deque

SOCK_ERRORS = (OSError, IOError, socket.error)  # py2: three distinct types

FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD

CMD_DATA = 0x00
CMD_SIGREPORT = 0x07
CMD_SETFREQ, CMD_SETSF, CMD_SETBW, CMD_SETCR = 0x10, 0x11, 0x12, 0x13
CMD_SETPOWER, CMD_SETSYNC, CMD_SETPREAMBLE = 0x14, 0x15, 0x16


def log(msg):
    sys.stdout.write("chimera-bridge: %s\n" % msg)
    sys.stdout.flush()


# ---------------- minimal config loader ----------------
# Parses the two-level "section: / key: value" YAML subset used by
# config/config.example.yaml. Scalars only — deliberately not a YAML parser,
# the on-device Python has no yaml module.

def _scalar(v):
    v = v.strip().strip('"').strip("'")
    if v.lower() in ("true", "yes", "on"):
        return True
    if v.lower() in ("false", "no", "off"):
        return False
    try:
        return int(v, 0)  # handles 0x.. too
    except ValueError:
        return v


def load_config(path):
    cfg = {}
    section = None
    with open(path) as f:
        for raw in f:
            line = raw.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            indented = line[0] in (" ", "\t")
            key, sep, val = line.strip().partition(":")
            if not sep:
                continue
            key, val = key.strip(), val.strip()
            if not indented:
                if val == "":
                    section = key
                    cfg[key] = {}
                else:
                    section = None
                    cfg[key] = _scalar(val)
            elif section is not None:
                cfg[section][key] = _scalar(val)
    return cfg


# ---------------- KISS framing ----------------

def kiss_frame(cmd, payload=b""):
    # bytearray iteration yields ints on both py2 and py3 (bytes does not)
    out = bytearray([FEND])
    for b in bytearray([cmd]) + bytearray(payload):
        if b == FEND:
            out += bytearray([FESC, TFEND])
        elif b == FESC:
            out += bytearray([FESC, TFESC])
        else:
            out.append(b)
    out.append(FEND)
    return bytes(out)


class KissDeframer:
    """Incremental KISS deframer; feed() yields (cmd, payload) tuples."""

    def __init__(self):
        self.buf = bytearray()
        self.in_frame = False
        self.escaped = False

    def feed(self, data):
        frames = []
        for b in bytearray(data):
            if b == FEND:
                if self.in_frame and self.buf:
                    frames.append((self.buf[0], bytes(self.buf[1:])))
                self.in_frame = True
                self.escaped = False
                self.buf = bytearray()
                continue
            if not self.in_frame:
                continue
            if self.escaped:
                self.escaped = False
                if b == TFEND:
                    b = FEND
                elif b == TFESC:
                    b = FESC
                else:
                    self.buf = bytearray()  # protocol error, drop frame
                    self.in_frame = False
                    continue
            elif b == FESC:
                self.escaped = True
                continue
            self.buf.append(b)
        return frames


# ---------------- AX.25 <-> LoRa APRS text conversion (TNC mode) ----------
# The 433.775 LoRa APRS ecosystem (OE5BPA trackers/iGates, RadioGroup/PIRS
# nodes) transmits TNC2-style ASCII payloads, usually prefixed with the OE
# header 0x3C 0xFF 0x01. KISS clients expect binary AX.25 UI frames and
# silently discard the text. With `mode: tnc` and
# `bridge.kiss_text_translation` on (default), the bridge converts at the
# TCP<->serial boundary: the air side stays text, the client side AX.25.
# Only the local KISS TCP link changes — nothing extra leaks on air.

OE_HEADER = b"\x3c\xff\x01"
AX25_UI, AX25_PID = 0x03, 0xF0
MAX_AIR_PAYLOAD = 255  # SX127x LoRa frame limit

_CALL_RE = re.compile(r"^([A-Z0-9]{1,6})(?:-([0-9]{1,2}))?(\*)?$")


def _parse_call(s):
    """'IZ0KEW-7' / 'IU0JIW-10*' -> (call, ssid, starred) or None."""
    m = _CALL_RE.match(s)
    if m is None:
        return None
    ssid = int(m.group(2)) if m.group(2) else 0
    if ssid > 15:
        return None
    return m.group(1), ssid, m.group(3) is not None


def _encode_addr(call, ssid, hbit, last):
    out = bytearray()
    for ch in call.ljust(6):
        out.append(ord(ch) << 1)
    ssid_byte = 0x60 | ((ssid & 0x0F) << 1)
    if hbit:
        ssid_byte |= 0x80  # H: has-been-repeated
    if last:
        ssid_byte |= 0x01  # address-field extension bit
    out.append(ssid_byte)
    return out


def text_to_ax25(text):
    """TNC2 text payload (OE header already stripped) -> AX.25 UI frame.

    Returns (frame_bytes, header_str) or (None, reason). Everything past the
    first ':' is the info field, copied verbatim — it can contain further
    ':', double spaces, or arbitrary base91-compressed bytes; never trim or
    re-encode it.
    """
    idx = text.find(b":")
    if idx < 0:
        return None, "no ':' separator"
    try:
        header = text[:idx].decode("ascii")
    except UnicodeDecodeError:
        return None, "non-ASCII header"
    info = text[idx + 1:]
    src_s, sep, rest = header.partition(">")
    if not sep:
        return None, "no '>' in header"
    parts = rest.split(",")
    if len(parts) - 1 > 8:
        return None, "more than 8 path elements"
    src = _parse_call(src_s.upper())
    dest = _parse_call(parts[0].upper())
    if src is None or src[2] or dest is None or dest[2]:
        return None, "bad src/dest callsign in %r" % header
    digis, starred_at = [], -1
    for k, d in enumerate(parts[1:]):
        c = _parse_call(d.upper())
        if c is None:
            return None, "bad path element %r" % d
        digis.append(c)
        if c[2]:
            # TNC2: '*' marks the last repeater that handled the packet and
            # implies the H bit on every digi before it as well
            starred_at = k
    frame = bytearray()
    frame += _encode_addr(dest[0], dest[1], False, False)
    frame += _encode_addr(src[0], src[1], False, not digis)
    for k, (call, ssid, _) in enumerate(digis):
        frame += _encode_addr(call, ssid, k <= starred_at, k == len(digis) - 1)
    frame.append(AX25_UI)
    frame.append(AX25_PID)
    frame += info
    return bytes(frame), header


def _decode_addrs(frame):
    """AX.25 address field -> ((call, ssid, hbit) list, control offset) or None."""
    b = bytearray(frame)
    addrs = []
    i = 0
    while True:
        if i + 7 > len(b) or len(addrs) == 10:  # 2 + max 8 digis
            return None
        call = ""
        for c in b[i:i + 6]:
            if c & 0x01:  # extension bit inside a callsign
                return None
            ch = c >> 1
            if not (ch == 0x20 or 0x30 <= ch <= 0x39 or 0x41 <= ch <= 0x5A):
                return None
            call += chr(ch)
        call = call.rstrip()
        if not call or " " in call:
            return None
        ssid_byte = b[i + 6]
        addrs.append((call, (ssid_byte >> 1) & 0x0F, bool(ssid_byte & 0x80)))
        i += 7
        if ssid_byte & 0x01:
            break
    if len(addrs) < 2:
        return None
    return addrs, i


def looks_like_ax25(payload):
    """True if payload plausibly already is a binary AX.25 UI frame."""
    dec = _decode_addrs(payload)
    if dec is None:
        return False
    _, i = dec
    b = bytearray(payload)
    return i + 2 <= len(b) and b[i] == AX25_UI and b[i + 1] == AX25_PID


def ax25_to_text(frame):
    """AX.25 UI frame -> (OE-header LoRa APRS payload, header_str) or (None, reason)."""
    dec = _decode_addrs(frame)
    if dec is None:
        return None, "bad address field"
    addrs, i = dec
    b = bytearray(frame)
    if i + 2 > len(b):
        return None, "truncated frame"
    if b[i] != AX25_UI or b[i + 1] != AX25_PID:
        return None, "not a UI frame (control 0x%02x, pid 0x%02x)" % (b[i], b[i + 1])
    info = bytes(b[i + 2:])
    dest, src, digis = addrs[0], addrs[1], addrs[2:]

    def fmt(a):
        return a[0] + ("-%d" % a[1] if a[1] else "")

    last_h = -1
    for k, d in enumerate(digis):
        if d[2]:
            last_h = k
    header = fmt(src) + ">" + fmt(dest)
    for k, d in enumerate(digis):
        header += "," + fmt(d) + ("*" if k == last_h else "")
    payload = OE_HEADER + header.encode("ascii") + b":" + info
    if len(payload) > MAX_AIR_PAYLOAD:
        return None, "%d B exceeds LoRa payload limit (%d)" % (
            len(payload), MAX_AIR_PAYLOAD)
    return payload, header


def _hexdump(data, limit=32):
    h = "".join("%02x" % b for b in bytearray(data[:limit]))
    return h + ("..." if len(data) > limit else "")


def rx_convert(payload):
    """Air payload -> payload for KISS clients. Returns (bytes or None, note)."""
    if payload.startswith(OE_HEADER):
        frame, note = text_to_ax25(payload[len(OE_HEADER):])
        if frame is None:
            return None, "dropped OE packet (%s): %s" % (note, _hexdump(payload))
        return frame, "text->ax25 %s (%d B)" % (note, len(frame))
    frame, note = text_to_ax25(payload)  # some firmwares omit the OE header
    if frame is not None:
        return frame, "headerless text->ax25 %s (%d B)" % (note, len(frame))
    if looks_like_ax25(payload):
        return payload, "ax25 passthrough (%d B)" % len(payload)
    return None, "dropped unrecognized payload (%d B): %s" % (
        len(payload), _hexdump(payload))


def tx_convert(payload):
    """KISS client payload -> on-air payload. Returns (bytes or None, note)."""
    if payload.startswith(OE_HEADER):
        # client already speaks LoRa APRS text: pass through
        return payload, "text passthrough (%d B)" % len(payload)
    out, note = ax25_to_text(payload)
    if out is None:
        return None, "dropped client frame (%s): %s" % (note, _hexdump(payload))
    return out, "ax25->text %s (%d B)" % (note, len(out))


# ---------------- TX pacing ----------------
# The ATmega modem is effectively deaf while it transmits: txRaw blocks in
# waitPacketSent() for the whole airtime (~0.73 s for a full frame at
# SF8/BW125, ~9 s at SF12) and the ATmega328P hardware serial buffer holds
# only 64 bytes, so any frame written to the serial link during an ongoing
# transmission arrives truncated/corrupted. Verified on hardware 2026-07-11:
# RNode split packets (two back-to-back frames) systematically lost their
# second frame in Reticulum mode. Every serial write therefore goes through
# a queue paced by the computed airtime of the previous DATA frame.


def lora_airtime_s(payload_len, params):
    """LoRa time-on-air (Semtech AN1200.13), explicit header + CRC as
    configured by RH_RF95. `params` is a radio profile dict."""
    sf = int(params.get("spreading_factor", 12))
    bw = float(params.get("bandwidth_hz", 125000))
    cr = int(params.get("coding_rate", 5))  # denominator 5..8
    n_pre = int(params.get("preamble_symbols", 8))
    t_sym = (2.0 ** sf) / bw
    de = 2 if t_sym > 0.016 else 0  # LowDataRateOptimize
    num = 8.0 * payload_len - 4.0 * sf + 28 + 16
    n_pay = 8 + max(int(math.ceil(num / (4.0 * (sf - de)))) * cr, 0)
    return (n_pre + 4.25 + n_pay) * t_sym


class TxPacer:
    """Serial write queue paced by the modem's TX airtime.

    push() enqueues a frame; pop_due() hands it back once the modem is
    guaranteed idle again; sent() must be called after the actual write and
    accounts for the new frame's airtime (plus serial transfer time and a
    guard margin). Config frames update the tracked radio parameters so
    subsequent airtime estimates stay correct.
    """

    MAX_QUEUE = 32
    GUARD_S = 0.1

    def __init__(self, params, baud):
        self.params = dict(params)
        self.baud = float(baud)
        self.queue = deque()
        self.ready_at = 0.0

    def push(self, cmd, payload):
        if len(self.queue) >= self.MAX_QUEUE:
            return False
        self.queue.append((cmd, payload))
        return True

    def timeout(self, now):
        """Seconds until the next queued frame may be written; None if empty."""
        if not self.queue:
            return None
        return max(0.0, self.ready_at - now)

    def pop_due(self, now):
        if self.queue and now >= self.ready_at:
            return self.queue.popleft()
        return None

    def sent(self, cmd, payload, wire_len, now):
        air = lora_airtime_s(len(payload), self.params) if cmd == CMD_DATA else 0.0
        self.ready_at = now + wire_len * 10.0 / self.baud + air + self.GUARD_S
        p = bytearray(payload)
        if cmd == CMD_SETSF and len(p) == 1:
            self.params["spreading_factor"] = p[0]
        elif cmd == CMD_SETBW and len(p) == 4:
            self.params["bandwidth_hz"] = struct.unpack(">I", bytes(p))[0]
        elif cmd == CMD_SETCR and len(p) == 1:
            self.params["coding_rate"] = p[0]
        elif cmd == CMD_SETPREAMBLE and len(p) == 2:
            self.params["preamble_symbols"] = struct.unpack(">H", bytes(p))[0]

    def clear(self):
        self.queue.clear()
        self.ready_at = 0.0


def write_all(fd, data):
    """os.write() the whole buffer to a non-blocking fd."""
    while data:
        try:
            n = os.write(fd, data)
        except (OSError, IOError) as e:
            if getattr(e, "errno", None) in (
                    errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                time.sleep(0.005)
                continue
            raise
        data = data[n:]


# ---------------- serial port ----------------

def open_serial(device, baud):
    fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    speed = getattr(termios, "B%d" % baud)
    # raw 8N1, no flow control
    attrs[0] = 0                            # iflag
    attrs[1] = 0                            # oflag
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag
    attrs[3] = 0                            # lflag
    attrs[4] = speed                        # ispeed
    attrs[5] = speed                        # ospeed
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd


# ---------------- radio profile push ----------------

def push_profile(ser_fd, profile):
    frames = [
        (CMD_SETFREQ, struct.pack(">I", int(profile.get("frequency_hz", 433775000)))),
        (CMD_SETBW, struct.pack(">I", int(profile.get("bandwidth_hz", 125000)))),
        (CMD_SETSF, struct.pack(">B", int(profile.get("spreading_factor", 12)))),
        (CMD_SETCR, struct.pack(">B", int(profile.get("coding_rate", 5)))),
        (CMD_SETPOWER, struct.pack(">B", int(profile.get("tx_power_dbm", 10)))),
        (CMD_SETSYNC, struct.pack(">B", int(profile.get("sync_word", 0x12)))),
        (CMD_SETPREAMBLE, struct.pack(">H", int(profile.get("preamble_symbols", 8)))),
    ]
    for cmd, payload in frames:
        os.write(ser_fd, kiss_frame(cmd, payload))
        time.sleep(0.05)  # let the ATmega apply each setting
    log("radio profile pushed: %s" % profile)


def serial_connect(serial_cfg, profile):
    """Open the serial link and push the radio profile. Returns fd or None.

    Never raises: a tty hangup (e.g. something else reclaiming the port)
    must degrade the serial link, not kill the TCP server.
    """
    device = serial_cfg.get("device", "/dev/ttyATH0")
    fd = None
    try:
        fd = open_serial(device, int(serial_cfg.get("baud", 115200)))
        time.sleep(2)  # ATmega328P resets on serial open on some boards
        push_profile(fd, profile)
        return fd
    except (OSError, IOError) as e:
        log("serial link failed on %s: %s" % (device, e))
        if fd is not None:
            try:
                os.close(fd)
            except (OSError, IOError):
                pass
        return None


# ---------------- main loop ----------------

SERIAL_RETRY_S = 5


def main():
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "/etc/chimera/config.yaml"
    cfg = load_config(cfg_path)

    serial_cfg = cfg.get("serial", {})
    bridge_cfg = cfg.get("bridge", {})
    mode = cfg.get("mode", "tnc")
    profile = cfg.get("radio_%s" % mode, {})
    allow_client_config = bool(bridge_cfg.get("allow_client_config", False))
    # AX.25<->text conversion is a TNC-mode feature only: aprs mode daemons
    # (digipeater/igate) parse the raw text themselves, reticulum mode
    # requires a byte-exact pass-through (§4.3.1).
    translate = mode == "tnc" and bool(
        bridge_cfg.get("kiss_text_translation", True))

    # Bind before touching the serial port: local consumers (digipeater,
    # iGate) must be able to connect even while the serial link is down.
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bridge_cfg.get("listen_host", "0.0.0.0"),
              int(bridge_cfg.get("listen_port", 8001))))
    srv.listen(4)
    log("mode=%s, listening on %s:%s, kiss_text_translation=%s" % (
        mode, bridge_cfg.get("listen_host", "0.0.0.0"),
        bridge_cfg.get("listen_port", 8001), "on" if translate else "off"))

    ser_fd = serial_connect(serial_cfg, profile)
    retry_at = time.time() + SERIAL_RETRY_S

    clients = {}  # sock -> KissDeframer
    ser_deframer = KissDeframer()
    pacer = TxPacer(profile, int(serial_cfg.get("baud", 115200)))

    def drop_serial(fd):
        try:
            os.close(fd)
        except (OSError, IOError):
            pass
        log("serial link down, retrying in %d s" % SERIAL_RETRY_S)
        return None

    while True:
        if ser_fd is None and time.time() >= retry_at:
            ser_fd = serial_connect(serial_cfg, profile)
            retry_at = time.time() + SERIAL_RETRY_S
            if ser_fd is not None:
                ser_deframer = KissDeframer()
                pacer.clear()

        rlist = [srv] + list(clients)
        if ser_fd is not None:
            rlist.append(ser_fd)
        now = time.time()
        timeout = None if ser_fd is not None else max(0.5, retry_at - now)
        if ser_fd is not None:
            wait = pacer.timeout(now)
            if wait is not None:
                timeout = wait if timeout is None else min(timeout, wait)
        readable, _, _ = select.select(rlist, [], [], timeout)

        # drain the TX queue: write the next frame only once the modem is
        # guaranteed done transmitting the previous one (see TxPacer)
        while ser_fd is not None:
            item = pacer.pop_due(time.time())
            if item is None:
                break
            cmd, payload = item
            wire = kiss_frame(cmd, payload)
            try:
                write_all(ser_fd, wire)
                pacer.sent(cmd, payload, len(wire), time.time())
            except (OSError, IOError):
                ser_fd = drop_serial(ser_fd)
                retry_at = time.time() + SERIAL_RETRY_S
                pacer.clear()

        for r in readable:
            if r is srv:
                sock, addr = srv.accept()
                sock.setblocking(False)
                clients[sock] = KissDeframer()
                log("client connected: %s:%s" % addr)

            elif ser_fd is not None and r == ser_fd:
                try:
                    data = os.read(ser_fd, 4096)
                except (OSError, IOError):
                    data = b""
                if not data:
                    # select() readable + empty read on a tty = hangup
                    ser_fd = drop_serial(ser_fd)
                    retry_at = time.time() + SERIAL_RETRY_S
                    continue
                for cmd, payload in ser_deframer.feed(data):
                    if cmd == CMD_DATA:
                        if translate:
                            payload, note = rx_convert(payload)
                            log("translate rx: %s" % note)
                            if payload is None:
                                continue
                        frame = kiss_frame(CMD_DATA, payload)
                        for c in list(clients):
                            try:
                                c.sendall(frame)
                            except SOCK_ERRORS:
                                log("client dropped (write error)")
                                c.close()
                                del clients[c]
                    elif cmd == CMD_SIGREPORT and len(payload) == 3:
                        rssi = struct.unpack(">h", payload[0:2])[0]
                        snr = struct.unpack(">b", payload[2:3])[0]
                        log("rx signal: RSSI %d dBm, SNR %d dB" % (rssi, snr))
                    else:
                        log("modem frame cmd=0x%02x len=%d" % (cmd, len(payload)))

            else:  # TCP client
                try:
                    data = r.recv(4096)
                except SOCK_ERRORS:
                    data = b""
                if not data:
                    log("client disconnected")
                    r.close()
                    del clients[r]
                    continue
                for cmd, payload in clients[r].feed(data):
                    if cmd == CMD_DATA or allow_client_config:
                        if cmd == CMD_DATA and translate:
                            payload, note = tx_convert(payload)
                            log("translate tx: %s" % note)
                            if payload is None:
                                continue
                        if ser_fd is None:
                            log("serial link down, dropped client frame")
                            continue
                        if not pacer.push(cmd, payload):
                            log("tx queue full, dropped client frame (%d B)"
                                % len(payload))
                    else:
                        log("dropped client cmd 0x%02x (config not allowed)" % cmd)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
