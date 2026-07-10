#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the APRS-IS -> RF downlink gating logic in igate.py.

Covers downlink_frame(): the aprs-is.net gate-to-RF rules (messages only,
addressee recently heard on RF, third-party encapsulation) and the LoRa
payload size limit.

Run from the repo root:  python -m unittest discover tests -v
"""

import os
import sys
import unittest

IGATE = os.path.join(os.path.dirname(__file__), "..",
                     "openwrt", "igate", "igate.py")

if sys.version_info[0] >= 3:
    import importlib.util
    _spec = importlib.util.spec_from_file_location("chimera_igate", IGATE)
    igate = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(igate)
else:  # py2 fallback, same interpreter the Dragino runs
    import imp
    igate = imp.load_source("chimera_igate", IGATE)

OE = igate.OE_HEADER

MYCALL = "IZ0KEW-10"
TOCALL = "APRS"
NOW = 1000000.0
HEARD_S = 1800
# IZ0ABC-7 heard just now, IK0XYZ heard at the edge of the window.
HEARD = {"IZ0ABC-7": NOW - 10.0, "IK0XYZ": NOW - 1799.0}


def dl(line, heard=None, now=NOW):
    if heard is None:
        heard = dict(HEARD)
    return igate.downlink_frame(line, MYCALL, TOCALL, heard, now, HEARD_S)


class DownlinkGating(unittest.TestCase):

    def test_message_for_heard_station(self):
        line = b"IW1ABC-5>APRS,TCPIP*,qAC,T2ITALY::IZ0ABC-7 :ciao{001"
        self.assertEqual(
            dl(line),
            OE + b"IZ0KEW-10>APRS:}IW1ABC-5>APRS,TCPIP,IZ0KEW-10*"
                 b"::IZ0ABC-7 :ciao{001")

    def test_ack_is_gated_too(self):
        line = b"IW1ABC-5>APRS,TCPIP*,qAC,T2ITALY::IZ0ABC-7 :ack001"
        self.assertIsNotNone(dl(line))

    def test_addressee_padding_stripped(self):
        # 6-char callsign padded to 9 in the addressee field
        line = b"IW1ABC-5>APRS,TCPIP*::IK0XYZ   :hello"
        self.assertEqual(
            dl(line),
            OE + b"IZ0KEW-10>APRS:}IW1ABC-5>APRS,TCPIP,IZ0KEW-10*"
                 b"::IK0XYZ   :hello")

    def test_addressee_case_insensitive(self):
        line = b"IW1ABC-5>APRS,TCPIP*::iz0abc-7 :hi"
        self.assertIsNotNone(dl(line))

    def test_station_not_heard(self):
        line = b"IW1ABC-5>APRS,TCPIP*::I1AAA-9  :hello"
        self.assertIsNone(dl(line))

    def test_heard_expired(self):
        line = b"IW1ABC-5>APRS,TCPIP*::IK0XYZ   :hello"
        # within the window at NOW, expired 2 s later
        self.assertIsNotNone(dl(line, now=NOW))
        self.assertIsNone(dl(line, now=NOW + 2.0))

    def test_non_message_bodies_dropped(self):
        for body in (b"!4151.84N/01301.89E-pos", b">status text",
                     b"T#005,199,000,255,073,123,01101001",
                     b"=4151.84N/01301.89E-", b"}THIRD>PARTY,TCPIP*:x"):
            line = b"IZ0ABC-7>APRS,TCPIP*:" + body
            self.assertIsNone(dl(line), body)

    def test_bad_addressee_padding_dropped(self):
        # ':' terminator in the wrong column (addressee not padded to 9)
        self.assertIsNone(dl(b"IW1ABC-5>APRS,TCPIP*::IZ0ABC-7:hi"))

    def test_own_call_as_source_dropped(self):
        line = b"IZ0KEW-10>APRS,TCPIP*::IZ0ABC-7 :hello"
        self.assertIsNone(dl(line))

    def test_own_call_as_addressee_dropped(self):
        heard = dict(HEARD)
        heard[MYCALL] = NOW  # even if somehow in the heard table
        line = b"IW1ABC-5>APRS,TCPIP*::IZ0KEW-10:hello"
        self.assertIsNone(dl(line, heard=heard))

    def test_oversize_frame_dropped(self):
        line = (b"IW1ABC-5>APRS,TCPIP*::IZ0ABC-7 :" + b"x" * 250)
        self.assertIsNone(dl(line))

    def test_garbage_lines_dropped(self):
        for line in (b"garbage", b"# javAPRSSrvr 4.3.2b17",
                     b"# logresp IZ0KEW-10 verified", b"no-header:body",
                     b":ORPHAN   :message with no header"):
            self.assertIsNone(dl(line), line)


if __name__ == "__main__":
    unittest.main()
