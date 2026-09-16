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


    def test_clipped_notes_end_with_ellipsis(self):
        class _Fake:
            pass
        fake = _Fake()
        fake._entry_text_lines = CalendarGrid._entry_text_lines.__get__(fake, CalendarGrid)
        notes = (
            "Discussion w/ Raman for ongoing issues. "
            "Actioned requests by Mark from FRT. "
            "Stand ups & Salesforce Prod issue. "
            "(Not an issue on our side but w/ Salesforce itself)"
        )
        entry = TimeEntry(
            1, None, "Railcards - Project - Marketing Database Rebuild",
            "QDM-5992", "#22C55E", "2026-09-16", "08:00", "10:00", notes,
        )
        lines = fake._entry_text_lines(entry, 0, 0, 160, 72)
        texts = [t for t, *_ in lines]
        self.assertTrue(any(t.endswith("…") for t in texts), texts)
        joined = " ".join(texts)
        self.assertNotIn("Salesforce itself", joined)


class TestOverlayTextLinesWrap(unittest.TestCase):
    def test_tall_meeting_wraps_title_instead_of_ellipsis(self):
        from app.calendar_feed import CalendarEvent
        class _Fake:
            pass
        fake = _Fake()
        fake._overlay_text_lines = CalendarGrid._overlay_text_lines.__get__(fake, CalendarGrid)
        event = CalendarEvent(
            "u", "Daily Catch up - SM Leads", "2026-09-17", "08:00", "09:00",
            location="Microsoft Teams Meeting",
        )
        # Narrow hour-tall block: used to ellipsis to one line.
        lines = fake._overlay_text_lines(event, 0, 0, 100, 120)
        texts = [t for t, *_ in lines]
        joined = " ".join(texts)
        self.assertGreaterEqual(len(texts), 2)
        self.assertIn("Daily Catch", joined)
        self.assertIn("SM Leads", joined)
        self.assertNotIn("…", joined)
        self.assertTrue(any("08:00" in t for t in texts))
        self.assertIn("Microsoft Teams", joined)


if __name__ == "__main__":
    unittest.main()
