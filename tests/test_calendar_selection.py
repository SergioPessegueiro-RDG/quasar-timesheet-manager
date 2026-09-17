"""Click-away deselect vs drag-to-create on the weekly grid."""
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config
from app.calendar_view import CalendarGrid
from app.db import Database
from app.models import Activity, TimeEntry


class TestClickAwayDeselects(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.db_path = handle.name
        self.db = Database(self.db_path)
        self.opened = []
        self.cal = CalendarGrid(
            self.root, self.db,
            get_armed_activity=lambda: None,
            clear_armed_activity=lambda: None,
            open_time_block=lambda **kwargs: self.opened.append(kwargs),
        )
        self.cal.gutter_width = 40
        self.cal.day_width = 100
        self.cal.header_height = 24
        self.cal.px_per_min = 1.0
        self.cal._set_live_block = lambda *_a, **_k: None
        act_id = self.db.add_activity(Activity(None, "Deep Work", "QDM-1", color="#4C6EF5"))
        self.act = self.db.get_activity(act_id)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        os.unlink(self.db_path)

    def _event(self, day_idx, minute, dy=0):
        x = self.cal.gutter_width + day_idx * self.cal.day_width + 10
        y = self.cal.header_height + minute * self.cal.px_per_min + dy
        return types.SimpleNamespace(x=x, y=y, x_root=x, y_root=y, state=0)

    def _click(self, day_idx, minute):
        ev = self._event(day_idx, minute)
        self.cal._on_button1(ev)
        self.cal._on_release(ev)

    def test_empty_click_clears_selection_without_opening_time_block(self):
        entry_id = self.db.add_time_entry(TimeEntry(
            None, self.act.id, self.act.name, self.act.jira_key, self.act.color,
            self.cal.day_date(0).isoformat(), "09:00", "10:00", ""))
        self.cal.refresh()
        self.cal.selected_entry_id = entry_id
        self._click(4, 40)
        self.assertIsNone(self.cal.selected_entry_id)
        self.assertEqual(self.opened, [])
        self.assertIsNotNone(self.db.get_time_entry(entry_id))

    def test_empty_click_with_nothing_selected_does_not_open_time_block(self):
        self._click(2, 60)
        self.assertEqual(self.opened, [])

    def test_dragging_empty_space_still_opens_time_block(self):
        start = self._event(2, 60)
        moved = self._event(2, 60, dy=config.DRAG_THRESHOLD_PX + 8)
        self.cal._on_button1(start)
        self.cal._on_motion_drag(moved)
        self.cal._on_release(moved)
        self.assertEqual(len(self.opened), 1)
        self.assertTrue(self.opened[0].get("is_new"))


class TestUnsentMarker(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.db_path = handle.name
        self.db = Database(self.db_path)
        self.cal = CalendarGrid(
            self.root, self.db,
            get_armed_activity=lambda: None,
            clear_armed_activity=lambda: None,
            open_time_block=lambda **_k: None,
        )
        self.cal.gutter_width = 40
        self.cal.day_width = 100
        self.cal.header_height = 24
        self.cal.px_per_min = 1.0
        act_id = self.db.add_activity(Activity(None, "Deep Work", "QDM-1", color="#4C6EF5"))
        self.act = self.db.get_activity(act_id)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        os.unlink(self.db_path)

    def test_new_block_draws_an_unsent_mark(self):
        self.db.add_time_entry(TimeEntry(
            None, self.act.id, self.act.name, self.act.jira_key, self.act.color,
            self.cal.day_date(0).isoformat(), "09:00", "10:00", "notes"))
        self.cal.refresh()
        items = self.cal.canvas.find_withtag("unsent")
        self.assertTrue(items)
        labels = [self.cal.canvas.itemcget(i, "text") for i in items
                  if self.cal.canvas.type(i) == "text"]
        self.assertTrue(any("synced" in t.lower() for t in labels), labels)
        self.db.add_time_entry(TimeEntry(
            None, self.act.id, self.act.name, self.act.jira_key, self.act.color,
            self.cal.day_date(0).isoformat(), "09:00", "10:00", "notes"))
        self.cal.refresh()
        self.assertTrue(self.cal.canvas.find_withtag("unsent"))

    def test_synced_block_has_no_unsent_mark(self):
        from app.jira_sync import worklog_fingerprint
        entry_id = self.db.add_time_entry(TimeEntry(
            None, self.act.id, self.act.name, self.act.jira_key, self.act.color,
            self.cal.day_date(0).isoformat(), "09:00", "10:00", "notes"))
        entry = self.db.get_time_entry(entry_id)
        self.db.mark_time_entry_worklog_synced(
            entry_id, "10001", "QDM-1", worklog_fingerprint(entry))
        self.cal.refresh()
        self.assertFalse(self.cal.canvas.find_withtag("unsent"))

    def test_short_unsent_block_does_not_overlap_the_title(self):
        # 15 minutes at 1.5 px/min is ~22px — below the stacked-caption
        # threshold, which is how a 15-minute slot looks at typical zoom.
        self.cal.px_per_min = 1.5
        self.cal.day_width = 180
        self.db.add_time_entry(TimeEntry(
            None, self.act.id, "India - database rebuild", self.act.jira_key,
            self.act.color, self.cal.day_date(0).isoformat(), "09:00", "09:15",
            "notes"))
        self.cal.refresh()
        unsent = [i for i in self.cal.canvas.find_withtag("unsent")
                  if self.cal.canvas.type(i) == "text"]
        titles = [i for i in self.cal.canvas.find_withtag("entry_text")
                  if self.cal.canvas.type(i) == "text"]
        self.assertTrue(unsent, "short unsent blocks still show a caption")
        self.assertTrue(titles, "short unsent blocks still show the QDM name")
        caption = self.cal.canvas.bbox(unsent[0])
        title = self.cal.canvas.bbox(titles[0])
        self.assertIsNotNone(caption)
        self.assertIsNotNone(title)
        # Same line, title on the left, caption on the right — boxes
        # must not occupy the same pixels.
        self.assertLessEqual(title[2], caption[0] + 1, (title, caption))
        self.assertTrue(
            any("synced" in self.cal.canvas.itemcget(i, "text").lower()
                for i in unsent))


class TestPlaceAnywhereOnGrid(unittest.TestCase):
    """Logged blocks can be created and moved before the current time."""

    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.db_path = handle.name
        self.db = Database(self.db_path)
        self.opened = []
        self.cal = CalendarGrid(
            self.root, self.db,
            get_armed_activity=lambda: None,
            clear_armed_activity=lambda: None,
            open_time_block=lambda **kwargs: self.opened.append(kwargs),
        )
        self.cal.gutter_width = 40
        self.cal.day_width = 100
        self.cal.header_height = 24
        self.cal.px_per_min = 1.0
        self.cal._set_live_block = lambda *_a, **_k: None
        act_id = self.db.add_activity(Activity(None, "Deep Work", "QDM-1", color="#4C6EF5"))
        self.act = self.db.get_activity(act_id)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        os.unlink(self.db_path)

    def _event(self, day_idx, minute, dy=0):
        x = self.cal.gutter_width + day_idx * self.cal.day_width + 10
        y = self.cal.header_height + minute * self.cal.px_per_min + dy
        return types.SimpleNamespace(x=x, y=y, x_root=x, y_root=y, state=0)

    def test_snapped_minute_at_grid_top_is_start_of_day_not_now(self):
        y = self.cal.header_height
        self.assertEqual(self.cal._snapped_minute_for_y(y), 0)
        self.assertEqual(self.cal._view_minute_to_hhmm(0), "09:00")

    def test_drag_create_opens_time_block_at_start_of_day(self):
        start = self._event(2, 0)
        moved = self._event(2, 30)
        self.cal._on_button1(start)
        self.cal._on_motion_drag(moved)
        self.cal._on_release(moved)
        self.assertEqual(len(self.opened), 1)
        self.assertEqual(self.opened[0].get("initial_start"), "09:00")
        self.assertEqual(self.opened[0].get("initial_end"), "09:30")

    def test_move_afternoon_block_to_start_of_day(self):
        entry_id = self.db.add_time_entry(TimeEntry(
            None, self.act.id, self.act.name, self.act.jira_key, self.act.color,
            self.cal.day_date(2).isoformat(), "16:00", "17:00", "notes"))
        self.cal.refresh()
        start_min = self.cal._view_hhmm_to_minute("16:00")
        self.cal._on_button1(self._event(2, start_min + 20))
        self.assertEqual(self.cal._drag_state["mode"], "move")
        self.cal._on_motion_drag(self._event(2, 0))
        self.cal._on_release(self._event(2, 0))
        entry = self.db.get_time_entry(entry_id)
        self.assertEqual(entry.start_time, "09:00")
        self.assertEqual(entry.end_time, "10:00")

    def test_nudge_up_reaches_start_of_day(self):
        entry_id = self.db.add_time_entry(TimeEntry(
            None, self.act.id, self.act.name, self.act.jira_key, self.act.color,
            self.cal.day_date(1).isoformat(), "09:15", "09:45", "notes"))
        self.cal.refresh()
        self.cal.selected_entry_id = entry_id
        self.cal._nudge_selected_entry(minute_delta=-config.SLOT_MINUTES)
        entry = self.db.get_time_entry(entry_id)
        self.assertEqual(entry.start_time, "09:00")
        self.assertEqual(entry.end_time, "09:30")


if __name__ == "__main__":
    unittest.main()
