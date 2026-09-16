"""Work-hour window vs after-hours timer logs — no window needed."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime

from app.calendar_view import (
    display_hours_for_entries, _hhmm_to_minute, _minutes_total, _now_line_delay_ms,
)
from app.calendar_feed import CalendarEvent
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

    def test_imported_meeting_outside_work_hours_widens_the_grid(self):
        meeting = CalendarEvent("u", "Early standup", "2026-09-16", "08:00", "08:30")
        start, end = display_hours_for_entries([meeting], 9, 17)
        self.assertEqual(start, 8)
        self.assertEqual(end, 17)


class TestNowLineDelay(unittest.TestCase):
    def test_waits_until_the_next_minute(self):
        now = datetime(2026, 9, 16, 12, 15, 0, 0)
        self.assertEqual(_now_line_delay_ms(now), 60_000)

    def test_one_second_before_the_minute(self):
        now = datetime(2026, 9, 16, 12, 15, 59, 0)
        self.assertEqual(_now_line_delay_ms(now), 1_000)

    def test_floor_near_the_minute_boundary(self):
        now = datetime(2026, 9, 16, 12, 15, 59, 900_000)
        self.assertEqual(_now_line_delay_ms(now), 250)


if __name__ == "__main__":
    unittest.main()
