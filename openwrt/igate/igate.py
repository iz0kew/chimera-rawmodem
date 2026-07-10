#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""chimera-rawmodem — APRS iGate daemon (RF -> APRS-IS, optional downlink).

Connects to the chimera-bridge KISS TCP port as an ordinary client and
forwards received LoRa APRS packets to an APRS-IS Tier 2 server. Fully
decoupled from the digipeater daemon (§4.2): either runs without the other.

Gating rules (per aprs-is.net conventions):
- never gate packets whose path contains TCPIP, TCPXX, NOGATE or RFONLY
- never gate third-party packets (body starting with '}')
- the q construct is added by the server, not by us

Credentials live in /etc/chimera/aprs-is.conf (gitignored real copy of
config/aprs-is.example.conf). Refuses to start with the N0CALL placeholder.

Downlink (APRS-IS -> RF, igate.downlink, default off) follows the
aprs-is.net IGating conventions: only APRS messages addressed to stations
recently heard on RF (igate.heard_seconds) are transmitted, wrapped in
third-party format (MYCALL>TOCALL:}SRC>DEST,TCPIP,MYCALL*:body). It
requires a valid passcode — with the receive-only passcode -1 the server
routes nothing to us, so downlink is forced off. With downlink off,
server traffic is read and discarded to keep the connection alive.

Stdlib only, runs on Python 2.7 and 3.x (the Dragino factory firmware
ships Python 2.7 only — see docs/hardware-notes.md).
"""

import select
import socket
import sys
import time

FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD
CMD_DATA = 0x00
OE_HEADER = b"\x3c\xff\x01"

SOCK_ERRORS = (OSError, IOError, socket.error)  # py2: three distinct types

NO_GATE_TOKENS = ("TCPIP", "TCPXX", "NOGATE", "RFONLY")

VERSION = "0.1.0"


def log(msg):
    sys.stdout.write("chimera-igate: %s\n" % msg)
    sys.stdout.flush()


# ---- config loader + KISS helpers: kept identical to chimera-bridge.py ----

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


def load_credentials(path):
    """aprs-is.conf: one 'key value' pair per line, '#' comments."""
    creds = {}
    with open(path) as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            key, _, val = line.partition(" ")
            creds[key.strip().lower()] = val.strip()
    return creds


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


def gateable(tnc2):
    """Apply the no-gate rules to a TNC2 line; return True if gateable."""
    header, sep, body = tnc2.partition(":")
    if not sep or ">" not in header:
        return False
    if body.startswith("}"):
        return False
    path_part = header.partition(">")[2]
    for elem in path_part.split(","):
        if elem.rstrip("*").upper() in NO_GATE_TOKENS:
            return False
    return True


MAX_RF_PAYLOAD = 255  # SX127x LoRa payload limit, OE header included


def downlink_frame(line, mycall, tocall, heard, now, heard_seconds):
    """APRS-IS line (bytes) -> OE payload to transmit on RF, or None.

    Gate-to-RF rules per aprs-is.net conventions: only APRS messages
    (':ADDRESSEE:text'), only when the addressee was heard on RF within
    heard_seconds, wrapped in third-party format with the internet path
    replaced by TCPIP,MYCALL*.
    """
    header, sep, body = line.partition(b":")
    if not sep or b">" not in header:
        return None
    # APRS message: ':' + addressee padded to 9 chars + ':' + text
    if not body.startswith(b":") or len(body) < 11 or body[10:11] != b":":
        return None
    src = header.partition(b">")[0].strip()
    try:
        src_call = src.decode("ascii").upper()
        addressee = body[1:10].decode("ascii").strip().upper()
    except UnicodeDecodeError:
        return None
    if not addressee or src_call == mycall or addressee == mycall:
        return None
    ts = heard.get(addressee)
    if ts is None or now - ts > heard_seconds:
        return None
    dest = header.partition(b">")[2].split(b",")[0].strip()
    mycall_b = mycall.encode("ascii")
    third = (mycall_b + b">" + tocall.encode("ascii") + b":}" +
             src + b">" + dest + b",TCPIP," + mycall_b + b"*:" + body)
    if len(OE_HEADER) + len(third) > MAX_RF_PAYLOAD:
        return None
    return OE_HEADER + third


def connect_aprsis(server, port, callsign, passcode, srv_filter=""):
    sock = socket.create_connection((server, port), timeout=15)
    login = "user %s pass %s vers chimera-rawmodem %s" % (
        callsign, passcode, VERSION)
    if srv_filter:
        login += " filter %s" % srv_filter
    sock.sendall((login + "\r\n").encode("ascii"))
    log("logged in to %s:%d as %s" % (server, port, callsign))
    return sock


def main():
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "/etc/chimera/config.yaml"
    creds_path = sys.argv[2] if len(sys.argv) > 2 else "/etc/chimera/aprs-is.conf"
    cfg = load_config(cfg_path)
    igate = cfg.get("igate", {})
    bridge = cfg.get("bridge", {})

    if not igate.get("enabled", False):
        log("disabled in config, exiting")
        return

    creds = load_credentials(creds_path)
    callsign = creds.get("callsign", "N0CALL").upper()
    passcode = creds.get("passcode", "-1")
    if callsign.startswith("N0CALL"):
        log("refusing to start with placeholder callsign %s — edit /etc/chimera/aprs-is.conf" % callsign)
        sys.exit(1)

    # Regional rotate address by default — never a single fixed server.
    server = str(igate.get("server", "euro.aprs2.net"))
    server_port = int(igate.get("port", 14580))
    bridge_host = str(igate.get("bridge_host", "127.0.0.1"))
    bridge_port = int(bridge.get("listen_port", 8001))

    downlink = bool(igate.get("downlink", False))
    heard_seconds = int(igate.get("heard_seconds", 1800))
    tocall = str(igate.get("tocall", "APRS")).upper()
    srv_filter = str(igate.get("filter", "") or "")
    if downlink and passcode.strip() == "-1":
        log("downlink enabled but passcode is -1 (receive-only) — "
            "forcing downlink off")
        downlink = False
    if downlink:
        log("downlink on: tocall %s, heard window %d s" %
            (tocall, heard_seconds))

    heard = {}  # callsign -> last time heard on RF, for downlink gating

    while True:
        rf = aprsis = None
        try:
            rf = socket.create_connection((bridge_host, bridge_port), timeout=10)
            rf.settimeout(None)
            log("connected to bridge %s:%d" % (bridge_host, bridge_port))
            aprsis = connect_aprsis(server, server_port, callsign, passcode,
                                    srv_filter)
            deframer = KissDeframer()
            isbuf = b""

            while True:
                readable, _, _ = select.select([rf, aprsis], [], [])
                if aprsis in readable:
                    data = aprsis.recv(4096)
                    if not data:
                        raise OSError("APRS-IS closed connection")
                    # With downlink off, server lines (including '#'
                    # keepalives) are read and discarded.
                    if downlink:
                        isbuf += data
                        while b"\n" in isbuf:
                            line, _, isbuf = isbuf.partition(b"\n")
                            line = line.strip()
                            if not line or line.startswith(b"#"):
                                continue
                            frame = downlink_frame(line, callsign, tocall,
                                                   heard, time.time(),
                                                   heard_seconds)
                            if frame is None:
                                continue
                            rf.sendall(kiss_frame(CMD_DATA, frame))
                            log("rf-gated: %s" % frame[len(OE_HEADER):]
                                .decode("ascii", "replace"))
                if rf in readable:
                    data = rf.recv(4096)
                    if not data:
                        raise OSError("bridge closed connection")
                    for cmd, payload in deframer.feed(data):
                        if cmd != CMD_DATA or not payload.startswith(OE_HEADER):
                            continue
                        try:
                            tnc2 = payload[len(OE_HEADER):].decode("ascii")
                        except UnicodeDecodeError:
                            continue
                        src_call = tnc2.split(">", 1)[0].rstrip("*").upper()
                        if src_call == callsign:
                            continue  # our own RF transmission
                        # Heard-on-RF table for downlink, updated before the
                        # no-gate rules: a NOGATE station doesn't get gated
                        # to the internet but is still reachable on RF.
                        now = time.time()
                        for k in [k for k, t in heard.items()
                                  if now - t > heard_seconds]:
                            del heard[k]
                        if src_call:
                            heard[src_call] = now
                        if not gateable(tnc2):
                            continue
                        aprsis.sendall((tnc2 + "\r\n").encode("ascii"))
                        log("gated: %s" % tnc2)
        except SOCK_ERRORS as e:
            log("connection lost (%s), retrying in 10 s" % e)
            for s in (rf, aprsis):
                if s is not None:
                    try:
                        s.close()
                    except SOCK_ERRORS:
                        pass
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
