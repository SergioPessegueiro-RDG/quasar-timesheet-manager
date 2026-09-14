"""Word-wrap for calendar block titles/notes — no Tk window needed."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.calendar_view import wrap_block_text
from app.models import TimeEntry
from app.calendar_view import CalendarGrid


class TestWrapBlockText(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(wrap_block_text("", 20), [])
        self.assertEqual(wrap_block_text("   ", 20), [])

    def test_fits_on_one_line(self):
        self.assertEqual(wrap_block_text("Focus Block", 20), ["Focus Block"])

    def test_wraps_on_word_boundaries(self):
        self.assertEqual(
            wrap_block_text("QUASAR Continuous Improvement Health Dashboard", 20),
            ["QUASAR Continuous", "Improvement Health", "Dashboard"],
        )

    def test_splits_a_token_longer_than_the_line(self):
        self.assertEqual(wrap_block_text("abcdefghij", 4), ["abcd", "efgh", "ij"])


class TestEntryTextLinesWrap(unittest.TestCase):
    def test_tall_block_wraps_name_and_keeps_notes(self):
        # CalendarGrid._entry_text_lines doesn't need a live canvas — it
        # only reads the entry and the pixel box.
        class _Fake:
            pass
        fake = _Fake()
        fake._entry_text_lines = CalendarGrid._entry_text_lines.__get__(fake, CalendarGrid)
        entry = TimeEntry(
            1, None, "QUASAR Continuous Improvement Health Dashboard", None,
            "#22C55E", "2026-09-14", "09:00", "11:00",
            "Worked on automating the health dashboard export",
        )
        lines = fake._entry_text_lines(entry, 0, 0, 200, 160)
        texts = [t for t, *_ in lines]
        joined = " ".join(texts)
        self.assertIn("QUASAR Continuous", joined)
        self.assertIn("Improvement", joined)
        self.assertIn("Worked on automating", joined)
        self.assertTrue(any("09:00" in t for t in texts))


if __name__ == "__main__":
    unittest.main()
