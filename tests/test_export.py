import unittest

from procwatch import server


class TestCsvField(unittest.TestCase):
    """Process names are not safe CSV.

    "Arc Helper (Renderer)" is fine; a name with a comma in it silently shifts
    every column after it, which a spreadsheet shows as data rather than as
    damage.
    """

    def test_a_plain_name_is_unquoted(self):
        self.assertEqual(server._csv("Arc"), "Arc")

    def test_a_comma_forces_quoting(self):
        self.assertEqual(server._csv("Arc, Helper"), '"Arc, Helper"')

    def test_a_quote_is_doubled(self):
        self.assertEqual(server._csv('say "hi"'), '"say ""hi"""')

    def test_a_newline_forces_quoting(self):
        self.assertEqual(server._csv("two\nlines"), '"two\nlines"')

    def test_none_becomes_empty(self):
        self.assertEqual(server._csv(None), "")


if __name__ == "__main__":
    unittest.main()
