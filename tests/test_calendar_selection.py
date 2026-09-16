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


if __name__ == "__main__":
    unittest.main()
