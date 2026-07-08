#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the AX.25 <-> LoRa APRS text conversion in chimera-bridge.py.

The RX test vectors are real packets captured over the air on the Italian
433.775 MHz LoRa APRS network (RadioGroup/PIRS nodes, tocall APLRG1).

Run from the repo root:  python -m unittest discover tests -v
"""

import os
import sys
import types
import unittest

BRIDGE = os.path.join(os.path.dirname(__file__), "..",
                      "openwrt", "bridge", "chimera-bridge.py")

# The bridge imports termios (POSIX-only) at module level; stub it so the
# conversion functions can be tested on any development OS.
if "termios" not in sys.modules:
    try:
        import termios  # noqa: F401
    except ImportError:
        sys.modules["termios"] = types.ModuleType("termios")

if sys.version_info[0] >= 3:
    import importlib.util
    _spec = importlib.util.spec_from_file_location("chimera_bridge", BRIDGE)
    bridge = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(bridge)
else:  # py2 fallback, same interpreter the Dragino runs
    import imp
    bridge = imp.load_source("chimera_bridge", BRIDGE)

OE = bridge.OE_HEADER


def addr(call, ssid, hbit=False, last=False):
    """Reference AX.25 address encoder, independent of the implementation."""
    out = bytearray()
    for ch in call.ljust(6):
        out.append(ord(ch) << 1)
    b = 0x60 | ((ssid & 0x0F) << 1)
    if hbit:
        b |= 0x80
    if last:
        b |= 0x01
    out.append(b)
    return bytes(out)


# Real capture: base91-compressed position after '!', double spaces in the
# comment — all of it must survive verbatim.
CAPTURE_1 = (b"IK0EHZ>APLRG1,IU0JIW-10*:!L9Sj?Qg*Za  GIK0EHZ op. Patrizio"
             b" - www.meteolatina.it|'-%a|")
CAPTURE_1_INFO = CAPTURE_1.split(b":", 1)[1]

CAPTURE_2 = b"IZ0ABC-7>APLRG1,IU0JIW-10*,WIDE2-1:=4151.84N/01301.89E-test"

PINPOINT_BEACON = (b"IZ0KEW>APIN22,WIDE1-1,WIDE2-1:!4151.84N/01301.89E-"
                   b"/A=001772 PinPoint test beacon")


class TestRxTextToAx25(unittest.TestCase):

    def test_real_capture_field_by_field(self):
        frame, note = bridge.rx_convert(OE + CAPTURE_1)
        self.assertIsNotNone(frame, note)
        self.assertEqual(frame[0:7], addr("APLRG1", 0))               # dest
        self.assertEqual(frame[7:14], addr("IK0EHZ", 0))              # src
        self.assertEqual(frame[14:21],
                         addr("IU0JIW", 10, hbit=True, last=True))    # digi
        self.assertEqual(bytearray(frame)[21], 0x03)                  # UI
        self.assertEqual(bytearray(frame)[22], 0xF0)                  # PID
        self.assertEqual(frame[23:], CAPTURE_1_INFO)  # verbatim, spaces kept

    def test_multi_digi_h_bits(self):
        frame, note = bridge.rx_convert(OE + CAPTURE_2)
        self.assertIsNotNone(frame, note)
        self.assertEqual(frame[7:14], addr("IZ0ABC", 7))
        # IU0JIW-10* -> H bit set, not last
        self.assertEqual(frame[14:21], addr("IU0JIW", 10, hbit=True))
        # WIDE2-1 after the '*' -> H clear, last address
        self.assertEqual(frame[21:28], addr("WIDE2", 1, last=True))
        self.assertEqual(frame[30:], b"=4151.84N/01301.89E-test")

    def test_star_implies_h_on_earlier_digis(self):
        # '*' only on the second digi: the first must get the H bit too
        text = b"IZ0ABC>APLRG1,IR0AA-1,IU0JIW-10*,WIDE2-1:>status"
        frame, note = bridge.rx_convert(OE + text)
        self.assertIsNotNone(frame, note)
        self.assertEqual(frame[14:21], addr("IR0AA", 1, hbit=True))
        self.assertEqual(frame[21:28], addr("IU0JIW", 10, hbit=True))
        self.assertEqual(frame[28:35], addr("WIDE2", 1, last=True))

    def test_headerless_text_still_converted(self):
        frame_hdr, _ = bridge.rx_convert(OE + CAPTURE_1)
        frame_bare, note = bridge.rx_convert(CAPTURE_1)
        self.assertIsNotNone(frame_bare, note)
        self.assertEqual(frame_bare, frame_hdr)

    def test_no_digi_last_bit_on_src(self):
        frame, note = bridge.rx_convert(OE + b"IZ0KEW-7>APLRG1:>hello")
        self.assertIsNotNone(frame, note)
        self.assertEqual(frame[7:14], addr("IZ0KEW", 7, last=True))
        self.assertEqual(bytearray(frame)[14], 0x03)

    def test_info_with_colons_kept_verbatim(self):
        text = b"IZ0KEW>APLRG1::IK0EHZ   :hello{001"
        frame, note = bridge.rx_convert(OE + text)
        self.assertIsNotNone(frame, note)
        self.assertEqual(frame[16:], b":IK0EHZ   :hello{001")

    def test_ax25_passthrough(self):
        raw = (addr("APLRG1", 0) + addr("IK0EHZ", 0, last=True)
               + b"\x03\xf0" + b">already binary")
        frame, note = bridge.rx_convert(raw)
        self.assertEqual(frame, raw)
        self.assertIn("passthrough", note)

    def test_garbage_dropped(self):
        for junk in (b"\x00\x01\x02", b"\xff" * 10, OE, OE + b"\x80\x81",
                     b"", b"just some text without a header"):
            frame, note = bridge.rx_convert(junk)
            self.assertIsNone(frame, "should drop %r (%s)" % (junk, note))

    def test_invalid_callsigns_dropped(self):
        for text in (b"TOOLONG1>APLRG1:x",      # 7-char callsign
                     b"IZ0KEW-16>APLRG1:x",     # SSID > 15
                     b"IZ0KEW>APLRG1,BAD_CALL:x",
                     b"IZ0KEW>APLRG1,D1,D2,D3,D4,D5,D6,D7,D8,D9:x"):  # >8 digis
            frame, note = bridge.rx_convert(OE + text)
            self.assertIsNone(frame, "should drop %r (%s)" % (text, note))


class TestTxAx25ToText(unittest.TestCase):

    def test_pinpoint_beacon(self):
        frame = (addr("APIN22", 0) + addr("IZ0KEW", 0)
                 + addr("WIDE1", 1) + addr("WIDE2", 1, last=True)
                 + b"\x03\xf0"
                 + b"!4151.84N/01301.89E-/A=001772 PinPoint test beacon")
        out, note = bridge.tx_convert(frame)
        self.assertIsNotNone(out, note)
        self.assertEqual(out, OE + PINPOINT_BEACON)

    def test_h_bit_becomes_star_on_last_repeater_only(self):
        frame = (addr("APLRG1", 0) + addr("IZ0ABC", 7)
                 + addr("IR0AA", 1, hbit=True)
                 + addr("IU0JIW", 10, hbit=True)
                 + addr("WIDE2", 1, last=True)
                 + b"\x03\xf0>status")
        out, note = bridge.tx_convert(frame)
        self.assertIsNotNone(out, note)
        self.assertEqual(
            out, OE + b"IZ0ABC-7>APLRG1,IR0AA-1,IU0JIW-10*,WIDE2-1:>status")

    def test_non_ui_frame_dropped(self):
        sabm = addr("APLRG1", 0) + addr("IZ0KEW", 0, last=True) + b"\x2f"
        out, note = bridge.tx_convert(sabm)
        self.assertIsNone(out, note)

    def test_oversize_dropped_not_truncated(self):
        frame = (addr("APLRG1", 0) + addr("IZ0KEW", 0, last=True)
                 + b"\x03\xf0" + b"X" * 300)
        out, note = bridge.tx_convert(frame)
        self.assertIsNone(out)
        self.assertIn("exceeds", note)

    def test_text_from_client_passes_through(self):
        # e.g. a legacy client already speaking the OE text format
        out, note = bridge.tx_convert(OE + CAPTURE_1)
        self.assertEqual(out, OE + CAPTURE_1)
        self.assertIn("passthrough", note)


class TestRoundTrip(unittest.TestCase):

    def test_text_ax25_text_identical(self):
        for text in (CAPTURE_1, CAPTURE_2,
                     b"IZ0KEW-7>APLRG1:>no path",
                     b"IZ0ABC>APLRG1,IR0AA-1,IU0JIW-10*,WIDE2-1:>multi"):
            frame, note = bridge.text_to_ax25(text)
            self.assertIsNotNone(frame, note)
            back, note = bridge.ax25_to_text(frame)
            self.assertIsNotNone(back, note)
            self.assertEqual(back, OE + text)

    def test_ax25_text_ax25_identical(self):
        # binary beacon as generated by PinPoint -> text -> binary
        pinpoint = (addr("APIN22", 0) + addr("IZ0KEW", 0)
                    + addr("WIDE1", 1) + addr("WIDE2", 1, last=True)
                    + b"\x03\xf0"
                    + b"!4151.84N/01301.89E-/A=001772 PinPoint test beacon")
        text, note = bridge.ax25_to_text(pinpoint)
        self.assertIsNotNone(text, note)
        back, note = bridge.text_to_ax25(text[len(OE):])
        self.assertIsNotNone(back, note)
        self.assertEqual(back, pinpoint)


if __name__ == "__main__":
    unittest.main()
