#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""chimera-rawmodem — AR9331 serial<->TCP bridge daemon.

Owns the serial link to the ATmega328P modem (/dev/ttyATH0) and exposes it
as a KISS-over-TCP server. This is the single process allowed to touch the
serial port; every consumer (external KISS client, digipeater daemon, iGate
daemon, Reticulum host interface) connects to the TCP port instead.

- CMD_DATA (0x00) frames pass through in both directions, unmodified.
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

import os
import select
import socket
import struct
import sys
import termios
import time

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

    # Bind before touching the serial port: local consumers (digipeater,
    # iGate) must be able to connect even while the serial link is down.
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bridge_cfg.get("listen_host", "0.0.0.0"),
              int(bridge_cfg.get("listen_port", 8001))))
    srv.listen(4)
    log("mode=%s, listening on %s:%s" % (
        mode, bridge_cfg.get("listen_host", "0.0.0.0"),
        bridge_cfg.get("listen_port", 8001)))

    ser_fd = serial_connect(serial_cfg, profile)
    retry_at = time.time() + SERIAL_RETRY_S

    clients = {}  # sock -> KissDeframer
    ser_deframer = KissDeframer()

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

        rlist = [srv] + list(clients)
        if ser_fd is not None:
            rlist.append(ser_fd)
        timeout = None if ser_fd is not None else max(0.5, retry_at - time.time())
        readable, _, _ = select.select(rlist, [], [], timeout)

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
                        if ser_fd is None:
                            log("serial link down, dropped client frame")
                            continue
                        try:
                            os.write(ser_fd, kiss_frame(cmd, payload))
                        except (OSError, IOError):
                            ser_fd = drop_serial(ser_fd)
                            retry_at = time.time() + SERIAL_RETRY_S
                    else:
                        log("dropped client cmd 0x%02x (config not allowed)" % cmd)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
