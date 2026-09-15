"""Work-hour window vs after-hours timer logs — no window needed."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.calendar_view import display_hours_for_entries, _hhmm_to_minute, _minutes_total
from app.models import TimeEntry


def _entry(start, end):
    return TimeEntry(None, 1, "Night", None, "#000", "2026-09-15", start, end)


class TestDisplayHoursForEntries(unittest.TestCase):
    def test_in_range_blocks_leave_settings_hours_alone(self):
        start, end = display_hours_for_entries([_entry("10:00", "11:00")], 9, 17)
        self.assertEqual((start, end), (9, 17))

    def test_evening_timer_log_widens_past_end_hour(self):
        entry = _entry("19:00", "19:15")
        # On a 9–5 grid this lands past the last row (invisible).
        self.assertGreaterEqual(_hhmm_to_minute(entry.start_time, 9), _minutes_total(9, 17))
        start, end = display_hours_for_entries([entry], 9, 17)
        self.assertEqual(start, 9)
        self.assertEqual(end, 20)
        start_min = _hhmm_to_minute(entry.start_time, start)
        end_min = _hhmm_to_minute(entry.end_time, start)
        self.assertGreater(end_min, start_min)
        self.assertLess(end_min, _minutes_total(start, end))

    def test_early_block_pulls_the_grid_start_earlier(self):
        start, end = display_hours_for_entries([_entry("07:30", "08:00")], 9, 17)
        self.assertEqual(start, 7)
        self.assertEqual(end, 17)

    def test_overnight_block_extends_to_midnight(self):
        start, end = display_hours_for_entries([_entry("16:00", "02:00")], 9, 17)
        self.assertEqual(start, 9)
        self.assertEqual(end, 24)


if __name__ == "__main__":
    unittest.main()
