#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the serial TX pacing in chimera-bridge.py.

The ATmega modem is deaf while transmitting (blocking TX + 64-byte serial
buffer), so the bridge must space serial writes by the previous DATA frame's
airtime. Found on hardware 2026-07-11: RNode split packets (two back-to-back
LoRa frames) systematically lost their second frame in Reticulum mode.

Run from the repo root:  python -m unittest discover tests -v
"""

import os
import struct
import sys
import types
import unittest

BRIDGE = os.path.join(os.path.dirname(__file__), "..",
                      "openwrt", "bridge", "chimera-bridge.py")

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

RETICULUM = {"spreading_factor": 8, "bandwidth_hz": 125000,
             "coding_rate": 5, "preamble_symbols": 18}
APRS = {"spreading_factor": 12, "bandwidth_hz": 125000,
        "coding_rate": 5, "preamble_symbols": 8}


class TestAirtime(unittest.TestCase):

    def test_full_frame_sf8(self):
        # 255 B at SF8/BW125/CR5, preamble 18: 355.25 symbols * 2.048 ms.
        # Matches the ~0.73 s observed on hardware during the split-frame
        # investigation.
        t = bridge.lora_airtime_s(255, RETICULUM)
        self.assertAlmostEqual(t, 0.7276, delta=0.002)

    def test_sf12_with_ldro(self):
        # 100 B at SF12/BW125/CR5, preamble 8, LDRO active (t_sym > 16 ms):
        # n_pay = 8 + ceil((800-48+44)/40)*5 = 108 -> 120.25 sym * 32.768 ms.
        t = bridge.lora_airtime_s(100, APRS)
        self.assertAlmostEqual(t, 3.9403, delta=0.005)

    def test_scales_with_length(self):
        self.assertLess(bridge.lora_airtime_s(20, RETICULUM),
                        bridge.lora_airtime_s(200, RETICULUM))


class TestTxPacer(unittest.TestCase):

    def setUp(self):
        self.pacer = bridge.TxPacer(RETICULUM, 115200)

    def test_empty_queue_no_timeout(self):
        self.assertIsNone(self.pacer.timeout(0.0))
        self.assertIsNone(self.pacer.pop_due(0.0))

    def test_first_frame_immediately_due(self):
        self.pacer.push(bridge.CMD_DATA, b"x" * 100)
        self.assertEqual(self.pacer.timeout(10.0), 0.0)
        self.assertEqual(self.pacer.pop_due(10.0),
                         (bridge.CMD_DATA, b"x" * 100))

    def test_second_frame_waits_for_airtime(self):
        f1, f2 = b"a" * 255, b"b" * 40
        self.pacer.push(bridge.CMD_DATA, f1)
        self.pacer.push(bridge.CMD_DATA, f2)
        cmd, payload = self.pacer.pop_due(100.0)
        self.pacer.sent(cmd, payload, len(payload) + 3, 100.0)
        # not due while the modem is still on the air with f1
        self.assertIsNone(self.pacer.pop_due(100.0 + 0.5))
        wait = self.pacer.timeout(100.0 + 0.5)
        self.assertGreater(wait, 0.2)
        # due after airtime (~0.73 s) + serial time + guard
        self.assertEqual(self.pacer.pop_due(100.0 + 0.9),
                         (bridge.CMD_DATA, f2))

    def test_fifo_order(self):
        self.pacer.push(bridge.CMD_DATA, b"first")
        self.pacer.push(bridge.CMD_SETSF, b"\x08")
        self.assertEqual(self.pacer.pop_due(0.0)[1], b"first")

    def test_config_frames_no_airtime(self):
        self.pacer.push(bridge.CMD_SETSF, b"\x08")
        cmd, payload = self.pacer.pop_due(50.0)
        self.pacer.sent(cmd, payload, 4, 50.0)
        # only serial time + guard, far below any airtime
        self.assertLess(self.pacer.ready_at - 50.0, 0.2)

    def test_config_frames_update_params(self):
        base = bridge.lora_airtime_s(255, self.pacer.params)
        self.pacer.sent(bridge.CMD_SETSF, b"\x0c", 4, 0.0)  # SF12
        self.pacer.sent(bridge.CMD_SETBW, struct.pack(">I", 62500), 7, 0.0)
        self.pacer.sent(bridge.CMD_SETCR, b"\x08", 4, 0.0)
        self.pacer.sent(bridge.CMD_SETPREAMBLE, struct.pack(">H", 18), 5, 0.0)
        self.assertEqual(self.pacer.params["spreading_factor"], 12)
        self.assertEqual(self.pacer.params["bandwidth_hz"], 62500)
        self.assertEqual(self.pacer.params["coding_rate"], 8)
        self.assertEqual(self.pacer.params["preamble_symbols"], 18)
        self.assertGreater(bridge.lora_airtime_s(255, self.pacer.params), base)

    def test_queue_cap(self):
        for _ in range(self.pacer.MAX_QUEUE):
            self.assertTrue(self.pacer.push(bridge.CMD_DATA, b"x"))
        self.assertFalse(self.pacer.push(bridge.CMD_DATA, b"x"))

    def test_clear(self):
        self.pacer.push(bridge.CMD_DATA, b"x")
        self.pacer.sent(bridge.CMD_DATA, b"x" * 255, 258, 1000.0)
        self.pacer.clear()
        self.assertIsNone(self.pacer.timeout(0.0))
        self.assertEqual(self.pacer.ready_at, 0.0)


if __name__ == "__main__":
    unittest.main()
