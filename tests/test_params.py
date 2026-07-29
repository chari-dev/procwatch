import unittest

from procwatch import server


class TestSeconds(unittest.TestCase):
    """Query-string timestamps.

    The dashboard computes a window by dividing a wheel delta by a pixel
    width, so the value it sends carries a fraction. Rejecting that as
    unparseable produced "invalid literal for int() with base 10" as plain
    text, which the page then tried to read as JSON -- so a scroll surfaced
    as "could not reach the recorder".
    """

    def test_a_whole_number_is_itself(self):
        self.assertEqual(server._seconds("1785290511"), 1785290511)

    def test_a_fractional_second_is_accepted(self):
        self.assertEqual(server._seconds("1785290511.153447"), 1785290511)

    def test_a_float_is_truncated_not_rounded(self):
        # Truncation keeps the answer inside the window that was asked for.
        self.assertEqual(server._seconds("1785290511.9"), 1785290511)

    def test_exponent_notation_is_accepted(self):
        self.assertEqual(server._seconds("1.7852905e9"), 1785290500)

    def test_a_negative_timestamp_survives(self):
        # Before 1970 is not useful, but it is a number and must not crash.
        self.assertEqual(server._seconds("-5"), -5)

    def test_text_is_still_refused(self):
        # A 400 is the right answer for a request that is genuinely malformed;
        # only numbers-with-fractions were being refused wrongly.
        for junk in ("NaN2", "abc", "", "12abc"):
            with self.assertRaises(ValueError, msg=junk):
                server._seconds(junk)


if __name__ == "__main__":
    unittest.main()
