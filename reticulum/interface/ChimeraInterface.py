# chimera-rawmodem — custom Reticulum interface for the Dragino LG01-P modem.
#
# Runs on the EXTERNAL HOST (PC/homelab/phone), never on the Dragino. The
# Dragino is purely the radio modem; this class connects to its chimera-bridge
# KISS TCP port and moves raw Reticulum packet bytes in and out.
#
# CRITICAL INVARIANT (§4.3.1 of the project brief): the KISS framing used
# here exists ONLY on the TCP link between this host and the AR9331. The
# bytes inside a CMD_DATA frame are transmitted over the air unmodified.
#
# RNode ON-AIR FRAMING (verified against markqvist/RNode_Firmware source,
# 2026-07 — RNode_Firmware.ino transmit()/receive_callback()): contrary to
# the original project assumption, RNode firmware DOES prepend one framing
# byte to every LoRa frame: high nibble = random per-packet sequence, bit 0
# = FLAG_SPLIT. Packets longer than 254 bytes are sent as exactly two LoRa
# frames (254 + up to 254 data bytes), both carrying the same header byte;
# the receiver completes reassembly when the second frame with a matching
# sequence arrives. Host-side packets are capped at MTU 508 = 2 * 254.
# This class replicates that framing exactly (build_frames() on TX, the
# reassembly state machine in process_incoming_frame() on RX), so what the
# modem puts on air is byte-identical to a genuine RNode. The modem and
# bridge stay pass-through and know nothing about it.
#
# Not replicated (known limitation): RNode's CSMA/CAD channel access. The
# Dragino transmits immediately; on a busy channel collisions are more
# likely than between two RNodes.
#
# Installation (standard RNS custom-interface mechanism — works identically
# for rnsd, MeshChat and Sideband, which all read the same config format):
#
#   1. copy this file to ~/.reticulum/interfaces/ChimeraInterface.py
#   2. add to ~/.reticulum/config (or via MeshChat's Interface Editor;
#      Sideband needs the config file edited by hand):
#
#        [[Chimera LoRa]]
#          type = ChimeraInterface
#          enabled = yes
#          target_host = 10.130.1.1   # the Dragino's IP
#          target_port = 8001
#          # informative only — used to report an accurate bitrate to RNS;
#          # keep in sync with radio_reticulum in the Dragino's config.yaml
#          spreading_factor = 8
#          bandwidth_hz = 125000
#          coding_rate = 5
#
# Requires RNS >= 0.7.0 (custom interface loading from the interfaces dir).

import os
import socket
import threading
import time

from RNS.Interfaces.Interface import Interface
import RNS

FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD
CMD_DATA = 0x00

# RNode PHY framing constants (mirror RNode_Firmware Config.h/Framing.h)
FLAG_SPLIT = 0x01
SEQ_UNSET = 0xFF
SINGLE_MTU = 255          # max LoRa frame incl. the 1-byte framing header
FRAME_DATA_MAX = SINGLE_MTU - 1   # 254 data bytes per frame


def kiss_frame(cmd, payload=b""):
    out = bytearray([FEND])
    for b in bytes([cmd]) + payload:
        if b == FEND:
            out += bytes([FESC, TFEND])
        elif b == FESC:
            out += bytes([FESC, TFESC])
        else:
            out.append(b)
    out.append(FEND)
    return bytes(out)


class ChimeraInterface(Interface):
    DEFAULT_IFAC_SIZE = 8

    # 508 matches RNode firmware's MTU (Config.h): the largest host packet
    # that fits in two 255-byte LoRa frames after the 1-byte framing header
    # each (2 * 254). Packets <= 254 bytes go out as a single frame.
    HW_MTU = 508

    RECONNECT_WAIT = 5

    def __init__(self, owner, configuration):
        super().__init__()
        c = Interface.get_config_obj(configuration)

        self.owner = owner
        self.name = c["name"]
        self.target_host = c["target_host"]
        self.target_port = int(c["target_port"])

        sf = int(c.get("spreading_factor", 8))
        bw = int(c.get("bandwidth_hz", 125000))
        cr = int(c.get("coding_rate", 5))
        # LoRa raw bitrate: SF * (BW / 2^SF) * (4 / CR-denominator)
        self.bitrate = int(sf * (bw / (2 ** sf)) * (4.0 / cr))

        self.socket = None
        self.online = False
        self.detached = False

        # RNode split-packet reassembly state (mirrors receive_callback)
        self.rx_seq = SEQ_UNSET
        self.rx_buf = b""

        thread = threading.Thread(target=self._connect_loop, daemon=True)
        thread.start()

    # ---------------- connection handling ----------------

    def _connect_loop(self):
        while not self.detached:
            try:
                self.socket = socket.create_connection(
                    (self.target_host, self.target_port), timeout=10)
                self.socket.settimeout(None)
                self.rx_seq = SEQ_UNSET
                self.rx_buf = b""
                self.online = True
                RNS.log("%s connected to %s:%d" % (
                    self, self.target_host, self.target_port), RNS.LOG_INFO)
                self._read_loop()
            except OSError as e:
                RNS.log("%s connection error: %s" % (self, e), RNS.LOG_ERROR)
            self.online = False
            if self.socket is not None:
                try:
                    self.socket.close()
                except OSError:
                    pass
                self.socket = None
            if not self.detached:
                time.sleep(ChimeraInterface.RECONNECT_WAIT)

    def _read_loop(self):
        in_frame = False
        escaped = False
        buf = bytearray()
        while not self.detached:
            data = self.socket.recv(4096)
            if not data:
                raise OSError("bridge closed connection")
            for b in data:
                if b == FEND:
                    if in_frame and len(buf) > 1 and buf[0] == CMD_DATA:
                        self._process_frame(bytes(buf[1:]))
                    in_frame = True
                    escaped = False
                    buf = bytearray()
                    continue
                if not in_frame:
                    continue
                if escaped:
                    escaped = False
                    if b == TFEND:
                        b = FEND
                    elif b == TFESC:
                        b = FESC
                    else:
                        in_frame = False
                        buf = bytearray()
                        continue
                elif b == FESC:
                    escaped = True
                    continue
                buf.append(b)

    # ---------------- RNode PHY framing ----------------

    def _process_frame(self, frame):
        # `frame` is one raw over-the-air LoRa frame: 1 RNode framing byte
        # followed by data. State machine mirrors RNode receive_callback().
        if len(frame) < 2:
            return
        header = frame[0]
        body = frame[1:]
        sequence = (header >> 4) & 0x0F
        if header & FLAG_SPLIT:
            if self.rx_seq == SEQ_UNSET:
                # first half of a split packet
                self.rx_seq = sequence
                self.rx_buf = body
            elif self.rx_seq == sequence:
                # second half: packet complete
                self.rx_buf += body
                self.rx_seq = SEQ_UNSET
                self.process_incoming(self.rx_buf)
                self.rx_buf = b""
            else:
                # different split packet started; drop the stale half
                self.rx_seq = sequence
                self.rx_buf = body
        else:
            if self.rx_seq != SEQ_UNSET:
                # single frame interrupts a pending split; drop the half
                self.rx_seq = SEQ_UNSET
                self.rx_buf = b""
            self.process_incoming(body)

    @staticmethod
    def build_frames(data):
        # Mirror RNode transmit(): one random-sequence header byte per LoRa
        # frame; packets > 254 bytes split into two frames sharing the header.
        header = os.urandom(1)[0] & 0xF0
        if len(data) > FRAME_DATA_MAX:
            header |= FLAG_SPLIT
            hb = bytes([header])
            return [hb + data[:FRAME_DATA_MAX], hb + data[FRAME_DATA_MAX:]]
        return [bytes([header]) + data]

    # ---------------- RNS Interface API ----------------

    def process_incoming(self, data):
        # Reassembled packet, framing header(s) stripped — what RNS expects.
        self.rxb += len(data)
        self.owner.inbound(data, self)

    def process_outgoing(self, data):
        # Each frame goes on the air byte-for-byte (framing byte included,
        # as RNode does); KISS wrapping stays on the TCP link.
        if not self.online:
            return
        try:
            for frame in self.build_frames(data):
                self.socket.sendall(kiss_frame(CMD_DATA, frame))
            self.txb += len(data)
        except OSError as e:
            RNS.log("%s send failed: %s" % (self, e), RNS.LOG_ERROR)
            self.online = False

    def detach(self):
        self.detached = True
        self.online = False
        if self.socket is not None:
            try:
                self.socket.close()
            except OSError:
                pass

    def __str__(self):
        return "ChimeraInterface[" + self.name + "]"


# Standard RNS custom-interface hook: tells the loader which class in this
# module implements the interface declared as `type = ChimeraInterface`.
interface_class = ChimeraInterface
