"""The notification queue: written by the recorder, collected by the menu
bar app, swept to osascript when nothing collects it."""
import unittest
from unittest import mock

from procwatch import alerts, db


class TestQueue(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_announced_notes_are_pending_in_order(self):
        alerts.announce(self.conn, "One", "first", target="why", now=100)
        alerts.announce(self.conn, "Two", "second", target="events", now=200)
        notes = alerts.pending(self.conn)
        self.assertEqual([(n["title"], n["target"]) for n in notes],
                         [("One", "why"), ("Two", "events")])

    def test_collecting_is_claiming(self):
        alerts.announce(self.conn, "One", "first", now=100)
        first = alerts.pending(self.conn, claim=True)
        self.assertEqual(len(first), 1)
        self.assertEqual(alerts.pending(self.conn), [])

    def test_looking_without_claiming_leaves_the_queue_alone(self):
        alerts.announce(self.conn, "One", "first", now=100)
        alerts.pending(self.conn)
        self.assertEqual(len(alerts.pending(self.conn)), 1)

    def test_stale_notes_fall_back_to_the_poster(self):
        alerts.announce(self.conn, "Old", "waited too long", now=100)
        alerts.announce(self.conn, "New", "still fresh", now=195)
        said = []
        sent = alerts.deliver_stale(self.conn, now=200, wait=90,
                                    poster=lambda t, b: said.append(t))
        self.assertEqual(sent, 1)
        self.assertEqual(said, ["Old"])
        # The fresh one is still waiting for the menu bar app.
        self.assertEqual([n["title"] for n in alerts.pending(self.conn)],
                         ["New"])

    def test_wait_zero_means_nothing_to_wait_for(self):
        alerts.announce(self.conn, "Now", "no bar app", now=200)
        said = []
        alerts.deliver_stale(self.conn, now=200, wait=0,
                             poster=lambda t, b: said.append(t))
        self.assertEqual(said, ["Now"])

    def test_a_claimed_note_is_never_posted_again(self):
        alerts.announce(self.conn, "One", "first", now=100)
        alerts.pending(self.conn, claim=True)
        said = []
        alerts.deliver_stale(self.conn, now=10**9, wait=0,
                             poster=lambda t, b: said.append(t))
        self.assertEqual(said, [])

    def test_ancient_notes_are_dropped_not_posted(self):
        alerts.announce(self.conn, "Ancient", "week-old news", now=100)
        alerts.deliver_stale(self.conn, now=100 + alerts.KEEP_NOTES + 1,
                             wait=10**8, poster=lambda t, b: None)
        self.assertEqual(alerts.pending(self.conn), [])


if __name__ == "__main__":
    unittest.main()
