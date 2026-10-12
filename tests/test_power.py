"""Battery behaviour: what counts as on battery, and when scans wait."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BATT = """Now drawing from 'Battery Power'
 -InternalBattery-0 (id=1234)	76%; discharging; 5:12 remaining present: true
"""
AC = """Now drawing from 'AC Power'
 -InternalBattery-0 (id=1234)	100%; charged; 0:00 remaining present: true
"""
G_LOW = """System-wide power settings:
Currently in use:
 standby              1
 lowpowermode         1
 sleep                1
"""
G_NORMAL = G_LOW.replace("lowpowermode         1", "lowpowermode         0")


class PowerTests(unittest.TestCase):
    def test_parse(self):
        from reclaim import power
        self.assertTrue(power.parse_on_battery(BATT))
        self.assertFalse(power.parse_on_battery(AC))
        self.assertFalse(power.parse_on_battery(""))          # desktop Macs: no battery
        self.assertTrue(power.parse_low_power(G_LOW))
        self.assertFalse(power.parse_low_power(G_NORMAL))

    def test_qos_is_harmless_off_mac(self):
        from reclaim import power
        if sys.platform != "darwin":
            self.assertFalse(power.set_thread_qos(power.QOS_BACKGROUND))


class ScanDeferralTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.environ["HOME"] = self.home
        for mod in list(sys.modules):
            if mod.startswith("reclaim"):
                del sys.modules[mod]
        from reclaim import scan
        from reclaim.daemon import Daemon
        self.scan = scan
        self.d = Daemon({"scan_root": self.home, "auto_update": "off", "notify": False})
        self.scans = []
        self.d.scan_now = lambda: self.scans.append(1)
        self.d.maybe_auto_clean = lambda free: None

    def tearDown(self):
        self.d.con.close()
        shutil.rmtree(self.home, ignore_errors=True)

    def free(self, gb):
        self.scan.disk_usage = lambda root=None: (gb * 10**9, 500 * 10**9)

    def test_due_scan_waits_on_battery_when_space_is_fine(self):
        self.free(80)
        self.d.defer = lambda: True
        self.d.tick()
        self.assertEqual(self.scans, [])

    def test_due_scan_runs_on_mains(self):
        self.free(80)
        self.d.defer = lambda: False
        self.d.tick()
        self.assertEqual(self.scans, [1])

    def test_low_space_scans_even_on_battery(self):
        self.free(8)
        self.d.defer = lambda: True
        self.d.tick()
        self.assertEqual(self.scans, [1])

    def test_setting_can_allow_battery_scans(self):
        self.free(80)
        self.d.defer = lambda: True
        self.d.cfg["scan_on_battery"] = True
        self.d.tick()
        self.assertEqual(self.scans, [1])


if __name__ == "__main__":
    unittest.main()
