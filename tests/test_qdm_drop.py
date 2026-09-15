"""QDM drag-from-sidebar onto a calendar day."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config
from app.calendar_view import CalendarGrid
from app.db import Database
from app.models import Activity


class TestQdmDropFollowsDay(unittest.TestCase):
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
        self.cal._set_live_block = lambda *_a, **_k: None
        self.act = Activity(1, "PNG Fix", jira_key="QDM-1842", color="#22C55E")

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

    def _state(self):
        return {
            "mode": "qdm-drop",
            "activity": self.act,
            "preview_rect": None,
            "preview_day_idx": None,
            "preview_start": None,
            "preview_end": None,
        }

    def _x_for_day(self, day_idx):
        return self.cal.gutter_width + day_idx * self.cal.day_width + 10

    def test_preview_follows_the_pointer_off_monday(self):
        state = self._state()
        y = self.cal.header_height + 40
        self.cal._update_qdm_drop_preview(self._x_for_day(0), y, state)
        self.assertEqual(state["preview_day_idx"], 0)
        self.cal._update_qdm_drop_preview(self._x_for_day(3), y, state)
        self.assertEqual(state["preview_day_idx"], 3)

    def test_vertical_move_does_not_lock_the_day(self):
        state = self._state()
        y = self.cal.header_height + 40
        self.cal._update_qdm_drop_preview(self._x_for_day(0), y, state)
        self.cal._update_qdm_drop_preview(self._x_for_day(0), y + 80, state)
        self.cal._update_qdm_drop_preview(self._x_for_day(3), y + 80, state)
        self.assertEqual(state["preview_day_idx"], 3)
        self.assertEqual(
            state["preview_end"] - state["preview_start"],
            config.QDM_DROP_MINUTES,
        )

    def test_leaving_the_grid_clears_the_slot(self):
        state = self._state()
        y = self.cal.header_height + 40
        self.cal._update_qdm_drop_preview(self._x_for_day(0), y, state)
        self.assertEqual(state["preview_day_idx"], 0)
        self.cal._clear_qdm_slot_preview(state)
        self.assertIsNone(state["preview_day_idx"])
        self.assertIsNone(state["preview_start"])

    def test_begin_drop_shows_a_ghost_card(self):
        self.cal.begin_qdm_drop(self.act)
        self.assertIsNotNone(self.cal._qdm_ghost)
        self.cal._unbind_qdm_drop()
        self.assertIsNone(self.cal._qdm_ghost)

    def test_ghost_is_a_grid_block_not_a_window(self):
        self.cal.begin_qdm_drop(self.act)
        ghost = self.cal._qdm_ghost
        self.assertIsInstance(ghost, dict)
        self.cal._place_qdm_ghost(80, 80)
        self.assertTrue(ghost["ids"])
        self.assertTrue(self.cal.canvas.find_withtag("qdm_ghost"))
        self.cal._unbind_qdm_drop()
        self.assertFalse(self.cal.canvas.find_withtag("qdm_ghost"))

    def test_today_column_blends_to_the_tint_not_plain_grid(self):
        from datetime import date
        from app import theme
        today = date.today()
        found_today = False
        for i in range(len(config.DAY_NAMES)):
            bg = self.cal._day_column_bg(i)
            if self.cal.day_date(i) == today:
                found_today = True
                self.assertEqual(bg, theme.TODAY_TINT)
            else:
                self.assertEqual(bg, theme.GRID_BG)
        if not found_today:
            self.assertEqual(self.cal._day_column_bg(0), theme.GRID_BG)

    def test_overflowing_grid_reserves_unmapped_scrollbar_width(self):
        from app.widgets import VectorScrollbar
        self.assertEqual(
            self.cal._vscroll_reserve_px(viewport_h=80, content_h=400),
            VectorScrollbar.WIDTH)
        self.assertEqual(
            self.cal._vscroll_reserve_px(viewport_h=800, content_h=400),
            0)


if __name__ == "__main__":
    unittest.main()
