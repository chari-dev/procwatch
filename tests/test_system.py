import unittest
from procwatch import system

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                    16902.
Pages active:                                 262520.
Pages inactive:                               261465.
Pages speculative:                               803.
Pages wired down:                             177599.
Pages occupied by compressor:                 292893.
"""

SWAP = "vm.swapusage: total = 2048.00M  used = 1243.06M  free = 804.94M  (encrypted)"


class TestSystem(unittest.TestCase):
    def test_vm_stat_uses_the_declared_page_size(self):
        used_kb, comp_kb = system.parse_vm_stat(VM_STAT)
        # active + wired, at 16 KB per page
        self.assertEqual(used_kb, (262520 + 177599) * 16)
        self.assertEqual(comp_kb, 292893 * 16)

    def test_swapusage_reports_used_megabytes_as_kb(self):
        self.assertEqual(system.parse_swapusage(SWAP), int(1243.06 * 1024))

    def test_read_returns_plausible_live_values(self):
        r = system.read()
        self.assertGreaterEqual(r.load1, 0)
        self.assertGreater(r.mem_used_kb, 0)
        self.assertGreater(r.disk_free_kb, 0)

    def test_a_missing_vm_stat_field_raises_rather_than_reporting_zero(self):
        # Silently returning 0 would write a plausible wrong number into a
        # history nobody re-checks. A raise becomes a skipped tick instead.
        without_wired = "\n".join(
            line for line in VM_STAT.splitlines() if "wired down" not in line)
        with self.assertRaises(system.SystemReadError):
            system.parse_vm_stat(without_wired)

    def test_unparseable_swapusage_raises(self):
        with self.assertRaises(system.SystemReadError):
            system.parse_swapusage("vm.swapusage: nonsense")

    def test_a_failing_subprocess_raises(self):
        from unittest import mock
        failed = mock.Mock(returncode=1, stdout="", stderr="vm_stat: boom")
        with mock.patch("procwatch.system.subprocess.run", return_value=failed):
            with self.assertRaises(system.SystemReadError):
                system.read()

    def test_machine_info_parses_each_probe(self):
        from unittest import mock

        def fake_run(args):
            if args[0] == "sw_vers":
                return "27.0\n"
            joined = " ".join(args)
            if "brand_string" in joined:
                return "Apple M3\n"
            if "hw.memsize" in joined:
                return "17179869184\n"
            return "{ sec = 1784956655, usec = 150878 } Fri Jul 24 2026\n"

        with mock.patch("procwatch.system._run", side_effect=fake_run), \
             mock.patch("procwatch.system.os.uname") as mock_uname, \
             mock.patch("procwatch.system.os.cpu_count", return_value=8):
            mock_uname.return_value = mock.Mock(nodename="host.local")
            info = system.machine_info()
        self.assertEqual(info["hostname"], "host")
        self.assertEqual(info["os_version"], "27.0")
        self.assertEqual(info["chip"], "Apple M3")
        self.assertEqual(info["cores"], 8)
        self.assertEqual(info["mem_total_kb"], 17179869184 // 1024)
        self.assertEqual(info["boot_ts"], 1784956655)

    def test_free_bytes_measures_the_database_directory(self):
        import os as _os
        from unittest import mock

        # Get a real statvfs object to return
        real_stat = _os.statvfs(_os.path.expanduser("~"))

        with mock.patch("procwatch.system.os.statvfs") as mock_statvfs:
            mock_statvfs.return_value = real_stat
            system.free_bytes()
            # Verify that statvfs was called with the DB directory
            mock_statvfs.assert_called_once_with(_os.path.dirname(system.config.DB_PATH))


if __name__ == "__main__":
    unittest.main()
