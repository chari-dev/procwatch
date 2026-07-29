import unittest
from procwatch.identity import derive

WEBKIT = ("/System/Library/Frameworks/WebKit.framework/Versions/A/XPCServices/"
          "com.apple.WebKit.WebContent.xpc/Contents/MacOS/com.apple.WebKit.WebContent")


class TestIdentity(unittest.TestCase):
    def test_two_log_streams_with_different_predicates_stay_apart(self):
        a = derive("/usr/bin/log", '/usr/bin/log stream --predicate DUPLEX-TRACE')
        b = derive("/usr/bin/log", '/usr/bin/log stream --predicate OTHER-TRACE')
        self.assertEqual(a[0], "log")
        self.assertNotEqual(a, b)

    def test_renderers_differing_only_in_volatile_tokens_merge(self):
        a = derive(WEBKIT, WEBKIT + " --type=renderer --pid=4765")
        b = derive(WEBKIT, WEBKIT + " --type=renderer --pid=3974")
        self.assertEqual(a, b)

    def test_long_bundle_paths_do_not_collapse_distinct_processes(self):
        # The round-1 bug: a 120-char prefix truncation made these identical.
        base = "/Applications/Some Very Long Vendor Name.app/Contents/Frameworks/"
        one = base + "Helper A.app/Contents/MacOS/Helper A"
        two = base + "Helper B.app/Contents/MacOS/Helper B"
        self.assertGreater(len(base), 60)
        self.assertNotEqual(derive(one, one), derive(two, two))

    def test_exe_is_a_basename_not_a_path(self):
        exe, _ = derive(WEBKIT, WEBKIT)
        self.assertEqual(exe, "com.apple.WebKit.WebContent")

    def test_executable_paths_containing_spaces_split_correctly(self):
        comm = "/System/Applications/Utilities/Activity Monitor.app/Contents/MacOS/Activity Monitor"
        exe, args = derive(comm, comm + " -foo bar")
        self.assertEqual(exe, "Activity Monitor")
        self.assertEqual(args, "-foo bar")

    def test_absolute_paths_in_arguments_reduce_to_basenames(self):
        _, args = derive("/bin/sh", "/bin/sh /Users/you/Developer/site/run.sh")
        self.assertEqual(args, "run.sh")

    def test_uuids_and_temp_paths_are_masked(self):
        c = "/usr/bin/tool"
        a = derive(c, c + " --id 550e8400-e29b-41d4-a716-446655440000")
        b = derive(c, c + " --id 6ba7b810-9dad-11d1-80b4-00c04fd430c8")
        self.assertEqual(a, b)

    def test_args_sig_is_capped_after_normalisation_not_before(self):
        c = "/usr/bin/tool"
        # Delimited multi-digit tokens, matching the shape of real argv --
        # a single digit glued to a word char is deliberately NOT masked.
        long_args = " ".join("--pid=%d" % (1000 + i) for i in range(200))
        _, args = derive(c, c + " " + long_args)
        self.assertLessEqual(len(args), 100)
        self.assertTrue(args.startswith("--pid=N"))

    def test_digits_glued_inside_a_token_are_not_masked(self):
        c = "/usr/sbin/diskutil"
        two = derive(c, c + " info disk2")
        three = derive(c, c + " info disk3")
        self.assertNotEqual(two, three)

    def test_delimited_single_digits_are_masked(self):
        c = "/usr/bin/helper"
        seven = derive(c, c + " --client-id=7")
        twelve = derive(c, c + " --client-id=12")
        self.assertEqual(seven, twelve)

    def test_a_process_with_no_arguments_has_an_empty_signature(self):
        self.assertEqual(derive("/sbin/launchd", "/sbin/launchd"), ("launchd", ""))

    def test_command_not_starting_with_comm_falls_back_to_no_args(self):
        # ps occasionally reports a command that does not extend comm.
        exe, args = derive("/usr/bin/thing", "(thing)")
        self.assertEqual(exe, "thing")
        self.assertEqual(args, "")


if __name__ == "__main__":
    unittest.main()
