"""The installer, read rather than run.

Running it would install onto the machine running the tests. What can be checked
without that is that it does the things the rest of the project promises: the
README, the website and the dashboard all tell people to type `procwatch`, and
for four releases nothing ever created that command.
"""
import os
import re
import subprocess
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(HERE, "install.sh")


def _script():
    with open(SCRIPT) as handle:
        return handle.read()


class TestShell(unittest.TestCase):
    def test_it_parses(self):
        # sh -n catches an unbalanced quote or a missing fi, which in an
        # installer is discovered by the person installing it.
        done = subprocess.run(["sh", "-n", SCRIPT], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)


class TestTheCommand(unittest.TestCase):
    """Everything tells people to type `procwatch`. Something has to create it."""

    def test_the_installer_creates_a_procwatch_command(self):
        script = _script()
        self.assertIn("link_command() {", script)
        self.assertRegex(script, r'ln -sf "\$TARGET" "\$dir/procwatch"')
        # Defined AND called. Asserting the name alone passes on a script that
        # defines the function and never runs it, which is the more likely
        # mistake of the two.
        self.assertRegex(script, r"(?m)^link_command\s*$")

    def test_it_only_uses_directories_already_on_the_path(self):
        # A command installed somewhere off PATH is the same as no command, and
        # it is worse, because the installer said it worked.
        script = _script()
        self.assertIn('case ":$PATH:" in *":$dir:"*', script)

    def test_the_user_owned_directory_is_tried_first(self):
        # Homebrew's bin is Homebrew's to manage; `brew doctor` complains about
        # foreign files in it, so it is the last resort rather than the first.
        #
        # Scoped to the loop inside link_command. There are two `for dir` loops
        # in the script and uninstall's comes first in the file, so an unscoped
        # search reads the wrong one and passes whatever the installer does.
        script = _script()
        body = script[script.index("link_command() {"):]
        order = re.search(r"for dir in ([^\n;]+); do", body).group(1)
        self.assertLess(order.index("$HOME/.local/bin"),
                        order.index("/opt/homebrew/bin"))

    def test_it_says_how_to_fix_a_path_that_has_nowhere_writable(self):
        self.assertIn("export PATH=", _script())

    def test_uninstall_removes_the_command(self):
        script = _script()
        self.assertIn('rm -f "$link"', script)

    def test_uninstall_only_removes_a_link_that_is_ours(self):
        # Somebody else's `procwatch` on the PATH is not ours to delete, and a
        # real file with that name certainly is not.
        script = _script()
        self.assertIn('[ -L "$link" ]', script)
        self.assertIn('[ "$(readlink "$link")" = "$TARGET" ]', script)


class TestWhatItTellsYou(unittest.TestCase):
    def test_the_closing_note_uses_the_command_it_just_made(self):
        # It used to print `python3 "/long/path/procwatch.py" open`, which is
        # both the wrong instruction and a confession that the command does not
        # exist.
        script = _script()
        tail = script[script.index("Done. The recorder is running"):]
        self.assertIn("procwatch why", tail)
        self.assertNotIn('python3 "$TARGET" open', tail)

    def test_every_command_it_advertises_is_a_real_subcommand(self):
        # The names the installer prints have to exist, or the first thing
        # somebody types after installing fails.
        import contextlib
        import io
        from procwatch import cli

        script = _script()
        tail = script[script.index("Done. The recorder is running"):]
        advertised = set(re.findall(r"\bprocwatch (\w+)", tail))
        self.assertTrue(advertised, "the installer advertises nothing")

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            with self.assertRaises(SystemExit):
                cli.main(["--help"])
        listed = re.search(r"\{([a-z,]+)\}", buffer.getvalue())
        self.assertIsNotNone(listed, buffer.getvalue()[:200])
        known = set(listed.group(1).split(","))
        self.assertEqual(sorted(advertised - known), [])


if __name__ == "__main__":
    unittest.main()
