#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""chimera-rawmodem — standalone LoRa APRS digipeater daemon.

Pure RF relay, zero internet dependency (§4.2 of the project brief).
Connects to the chimera-bridge KISS TCP port as an ordinary client,
so it is fully decoupled from the iGate daemon: either can run without
the other.

On-air format handled: LoRa-APRS "OE" convention — payload is
0x3C 0xFF 0x01 ('<' 0xFF 0x01) followed by a TNC2-style ASCII packet
(SRC>DEST,PATH1,PATH2:body).

Digipeat rules implemented:
- duplicate suppression window (default 30 s, keyed on src+body)
- never digipeat a packet that already carries our callsign used ('*')
- first unused WIDEn-N path element: decrement N, insert MYCALL* in
  front; drop the element when N reaches 0
- direct addressing: an unused path element equal to MYCALL is consumed
  and marked used

Stdlib only, runs on Python 2.7 and 3.x (the Dragino factory firmware
ships Python 2.7 only — see docs/hardware-notes.md).
"""

import re
import socket
import sys
import time

FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD
CMD_DATA = 0x00
OE_HEADER = b"\x3c\xff\x01"

SOCK_ERRORS = (OSError, IOError, socket.error)  # py2: three distinct types

WIDE_RE = re.compile(r"^WIDE([1-7])-([1-7])$")


def log(msg):
    sys.stdout.write("chimera-digipeater: %s\n" % msg)
    sys.stdout.flush()


# ---- config loader + KISS helpers: kept identical to chimera-bridge.py ----
# (each daemon is a self-contained single file for dead-simple deployment
# on the device; keep these blocks in sync manually)

def _scalar(v):
    v = v.strip().strip('"').strip("'")
    if v.lower() in ("true", "yes", "on"):
        return True
    if v.lower() in ("false", "no", "off"):
        return False
    try:
        return int(v, 0)
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
                    self.buf = bytearray()
                    self.in_frame = False
                    continue
            elif b == FESC:
                self.escaped = True
                continue
            self.buf.append(b)
        return frames


# ---------------- digipeat logic ----------------

def digipeat_path(path, mycall):
    """Return the new path list if we should digipeat, else None."""
    for elem in path:
        if elem.rstrip("*") == mycall and elem.endswith("*"):
            return None  # we already handled this packet

    new_path = []
    consumed = False
    for elem in path:
        if consumed or elem.endswith("*"):
            new_path.append(elem)
            continue
        if elem == mycall:
            new_path.append(mycall + "*")
            consumed = True
            continue
        m = WIDE_RE.match(elem)
        if m:
            n = int(m.group(2)) - 1
            new_path.append(mycall + "*")
            if n > 0:
                new_path.append("WIDE%s-%d" % (m.group(1), n))
            consumed = True
            continue
        new_path.append(elem)
    return new_path if consumed else None


def parse_tnc2(payload):
    """OE-header LoRa APRS payload -> (src, dest, path list, body) or None."""
    if not payload.startswith(OE_HEADER):
        return None
    try:
        text = payload[len(OE_HEADER):].decode("ascii")
    except UnicodeDecodeError:
        return None
    header, sep, body = text.partition(":")
    if not sep or ">" not in header:
        return None
    src, _, rest = header.partition(">")
    parts = rest.split(",")
    return src.strip(), parts[0], parts[1:], body


def main():
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "/etc/chimera/config.yaml"
    cfg = load_config(cfg_path)
    digi = cfg.get("digipeater", {})
    bridge = cfg.get("bridge", {})

    if not digi.get("enabled", False):
        log("disabled in config, exiting")
        return

    mycall = str(digi.get("callsign", "N0CALL-1")).upper()
    if mycall.startswith("N0CALL"):
        log("refusing to start with placeholder callsign %s — edit /etc/chimera/config.yaml" % mycall)
        sys.exit(1)

    dedupe_s = int(digi.get("dedupe_seconds", 30))
    host = str(digi.get("bridge_host", "127.0.0.1"))
    port = int(bridge.get("listen_port", 8001))

    seen = {}  # (src, body) -> timestamp

    while True:
        try:
            sock = socket.create_connection((host, port), timeout=10)
            sock.settimeout(None)
            log("connected to bridge %s:%d as %s" % (host, port, mycall))
            deframer = KissDeframer()
            while True:
                data = sock.recv(4096)
                if not data:
                    raise OSError("bridge closed connection")
                for cmd, payload in deframer.feed(data):
                    if cmd != CMD_DATA:
                        continue
                    parsed = parse_tnc2(payload)
                    if parsed is None:
                        continue
                    src, dest, path, body = parsed
                    if src.rstrip("*") == mycall:
                        continue  # our own transmission echoed back

                    now = time.time()
                    for k in [k for k, t in seen.items() if now - t > dedupe_s]:
                        del seen[k]
                    key = (src, body)
                    if key in seen:
                        continue
                    seen[key] = now

                    new_path = digipeat_path(path, mycall)
                    if new_path is None:
                        continue
                    out = "%s>%s" % (src, ",".join([dest] + new_path))
                    out_payload = OE_HEADER + ("%s:%s" % (out, body)).encode("ascii")
                    # Small fixed hold-off so the original transmission is
                    # clear of the channel before we key up. Single-threaded:
                    # blocks RX for its duration, acceptable at APRS rates.
                    time.sleep(0.5)
                    sock.sendall(kiss_frame(CMD_DATA, out_payload))
                    log("digipeated %s>%s" % (src, ",".join([dest] + new_path)))
        except SOCK_ERRORS as e:
            log("bridge connection lost (%s), retrying in 5 s" % e)
            time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
