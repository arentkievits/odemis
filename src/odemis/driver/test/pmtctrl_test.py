# -*- coding: utf-8 -*-
'''
Created on 13 Mar 2015

@author: Kimon Tsitsikas

Copyright © 2015 Kimon Tsitsikas, Delmic

This file is part of Odemis.

Odemis is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License version 2 as published by the Free Software Foundation.

Odemis is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with Odemis. If not, see http://www.gnu.org/licenses/.
'''
from __future__ import division

import glob
import logging
from odemis import model
from odemis.driver import pmtctrl
from odemis.driver import semcomedi
import threading
import unittest
from unittest.case import skip
from logging.handlers import BufferingHandler


logger = logging.getLogger().setLevel(logging.DEBUG)

# SN = "12345678"  # put the serial number written on the component to test

# Test using the hardware
CLASS = pmtctrl.PMTControl
# KWARGS = dict(name="test", role="pmt_control", port="/dev/ttyPMT*")
# Test using the simulator
KWARGS = dict(name="test", role="pmt_control", port="/dev/fake")

# Control unit used for PMT testing
CLASS_CTRL = CLASS
KWARGS_CTRL = KWARGS

# arguments used for the creation of basic components
CONFIG_SED = {"name": "sed", "role": "sed", "channel":5, "limits": [-3, 3]}
CONFIG_BSD = {"name": "bsd", "role": "bsd", "channel":6, "limits": [-0.1, 0.2]}
CONFIG_SCANNER = {"name": "scanner", "role": "ebeam", "limits": [[-5, 5], [3, -3]],
                  "channels": [0, 1], "settle_time": 10e-6, "hfw_nomag": 10e-3,
                  "park": [8, 8]}
CONFIG_SEM2 = {"name": "sem", "role": "sem", "device": "/dev/comedi0",
              "children": {"detector0": CONFIG_SED, "detector1": CONFIG_BSD, "scanner": CONFIG_SCANNER}
              }

CLASS_PMT = pmtctrl.PMT

class TestStatic(unittest.TestCase):
    """
    Tests which don't need a component ready
    """
    @skip("simple")
    def test_scan(self):
        devices = CLASS.scan()
        self.assertGreater(len(devices), 0)

    def test_creation(self):
        """
        Doesn't even try to do anything, just create and delete components
        """
        dev = CLASS(**KWARGS)

        self.assertTrue(dev.selfTest(), "self test failed.")
        dev.terminate()

    def test_wrong_serial(self):
        """
        Check it correctly fails if the device with the given serial number is
        not a PMT Control.
        """
        # Look for a device with a serial number not starting with 37
        paths = glob.glob("/sys/bus/usb/devices/*/serial")
        for p in paths:
            try:
                f = open(p)
                snw = f.read().strip()
            except IOError:
                logging.debug("Failed to read %s, skipping device", p)
        else:
            self.skipTest("Failed to find any USB device with a serial number")

        kwargsw = dict(KWARGS)
        kwargsw["sn"] = snw
        with self.assertRaises(ValueError):
            dev = CLASS(**kwargsw)


class TestPMTControl(unittest.TestCase):
    """
    Tests which need a component ready
    """

    def setUp(self):
        self.dev = CLASS(**KWARGS)

    def tearDown(self):
        self.dev.terminate()

    def test_send_cmd(self):
        # Send proper command
        ans = self.dev._sendCommand("VOLT 0.7")
        self.assertEqual(ans, '')

        # Send wrong command
        with self.assertRaises(IOError):
            self.dev._sendCommand("VOLT??")

        # Set value out of range
        with self.assertRaises(IOError):
            self.dev._sendCommand("VOLT 8.4")

        # Send proper set and get command
        self.dev._sendCommand("VOLT 0.3")
        ans = self.dev._sendCommand("VOLT?")
        ans_f = float(ans)
        self.assertAlmostEqual(ans_f, 0.3)

    def test_pmtctrl_va(self):
        # Test gain
        gain = 0.6
        self.dev.gain.value = gain
        self.assertAlmostEqual(self.dev.gain.value, gain)

        # Test powerSupply
        powerSupply = True
        self.dev.powerSupply.value = powerSupply
        self.assertEqual(self.dev.powerSupply.value, powerSupply)

        # Test protection
        protection = True
        self.dev.protection.value = protection
        self.assertEqual(self.dev.protection.value, protection)

# @skip("simple")
class TestPMT(unittest.TestCase):
    """
    Test the PMT class
    """

    @classmethod
    def setUpClass(cls):
        cls.sem = semcomedi.SEMComedi(**CONFIG_SEM2)
        cls.control = CLASS_CTRL(**KWARGS_CTRL)

        for child in cls.sem.children.value:
            if child.name == CONFIG_SED["name"]:
                cls.sed = child
            elif child.name == CONFIG_BSD["name"]:
                cls.bsd = child
            elif child.name == CONFIG_SCANNER["name"]:
                cls.scanner = child
        cls.pmt = CLASS_PMT(name="test", role="detector",
                                 children={"detector": cls.bsd,
                                           "pmt-control": cls.control})

    @classmethod
    def tearUpClass(cls):
        cls.pmt.terminate()
        cls.sem.terminate()
        cls.control.terminate()

    def tearDown(self):
        self.logger.removeHandler(self.handler)
        self.handler.close()

    def setUp(self):
        # We will need to catch some log messages
        self.handler = h = TestHandler(Matcher())
        self.logger = l = logging.getLogger()
        l.addHandler(h)
        # reset resolution and dwellTime
        self.scanner.resolution.value = (256, 200)
        self.size = self.scanner.resolution.value
        self.scanner.dwellTime.value = self.scanner.dwellTime.range[0]
        self.acq_dates = (set(), set())  # 2 sets of dates, one for each receiver
        # save basic VA
        self._orig_gain = self.pmt.gain.value
        self._orig_powerSupply = self.pmt.powerSupply.value

    def tearUp(self):
        pass

    def test_simple_acquisition(self):
        self.is_received = threading.Event()
        # Protection should be on before start acquisition
        self.assertEqual(self.control.protection.value, True)
        self.assertEqual(self.pmt.data.active, False)
        self.pmt.data.subscribe(self.receive_image)
        # Protection should be off upon acquisition start
        self.assertEqual(self.control.protection.value, False)
        self.assertEqual(self.pmt.data.active, True)
        self.is_received.wait()
        # Protection should be reset after acquisition is done
        self.assertEqual(self.control.protection.value, True)

    def test_wrong_acquisition(self):
        self.is_received = threading.Event()
        # Protection should be on before start acquisition
        self.assertEqual(self.control.protection.value, True)
        self.assertEqual(self.pmt.data.active, False)
        h = self.handler
        self.pmt.data.subscribe(self.receive_image)
        self.assertEqual(self.pmt.data.active, True)
        # Turn protection on and catch the warning message
        self.control._sendCommand("PROT 0")
        self.is_received.wait()
        self.assertTrue(h.matches(message="PMT protection was triggered during acquisition."))
        # Protection should be reset after acquisition is done
        self.assertEqual(self.control.protection.value, True)

    def test_gain_decrease_acquisition(self):
        self.is_received = threading.Event()
        self.pmt.gain.value = 1
        # Protection should be on before start acquisition
        self.assertEqual(self.control.protection.value, True)
        self.assertEqual(self.pmt.data.active, False)
        h = self.handler
        self.pmt.data.subscribe(self.receive_image)
        self.assertEqual(self.pmt.data.active, True)
        # Turn protection on and then decrease the gain, so the protection is
        # expected to be reset
        self.control._sendCommand("PROT 0")
        self.pmt.gain.value = 0.5
        self.is_received.wait()
        self.assertFalse(h.matches(message="PMT protection was triggered during acquisition."))
        # Protection should be reset after acquisition is done
        self.assertEqual(self.control.protection.value, True)

    def receive_image(self, dataflow, image):
        """
        callback for df
        """
        self.image = image
        self.assertEqual(image.shape, self.size[-1:-3:-1])
        self.assertIn(model.MD_DWELL_TIME, image.metadata)
        self.acq_dates[0].add(image.metadata[model.MD_ACQ_DATE])

        dataflow.unsubscribe(self.receive_image)
        self.assertEqual(self.pmt.data.active, False)
        self.is_received.set()


class TestHandler(BufferingHandler):
    def __init__(self, matcher):
        # BufferingHandler takes a "capacity" argument
        # so as to know when to flush. As we're overriding
        # shouldFlush anyway, we can set a capacity of zero.
        # You can call flush() manually to clear out the
        # buffer.
        BufferingHandler.__init__(self, 0)
        self.matcher = matcher

    def shouldFlush(self):
        return False

    def emit(self, record):
        self.buffer.append(record.__dict__)

    def matches(self, **kwargs):
        """
        Look for a saved dict whose keys/values match the supplied arguments.
        """
        result = False
        for d in self.buffer:
            if self.matcher.matches(d, **kwargs):
                result = True
                break
        return result

class Matcher(object):

    _partial_matches = ('msg', 'message')

    def matches(self, d, **kwargs):
        """
        Try to match a single dict with the supplied arguments.

        Keys whose values are strings and which are in self._partial_matches
        will be checked for partial (i.e. substring) matches. You can extend
        this scheme to (for example) do regular expression matching, etc.
        """
        result = True
        for k in kwargs:
            v = kwargs[k]
            dv = d.get(k)
            if not self.match_value(k, dv, v):
                result = False
                break
        return result

    def match_value(self, k, dv, v):
        """
        Try to match a single stored value (dv) with a supplied value (v).
        """
        if type(v) != type(dv):
            result = False
        elif type(dv) is not str or k not in self._partial_matches:
            result = (v == dv)
        else:
            result = dv.find(v) >= 0
        return result

if __name__ == "__main__":
    unittest.main()
