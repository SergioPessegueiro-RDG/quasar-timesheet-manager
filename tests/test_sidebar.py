"""Pure helpers for the QDM sidebar (no window needed)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import Activity
from app.sidebar import qdm_matches, qdm_haystack, qdm_combo_rows


class TestQdmMatches(unittest.TestCase):
    def setUp(self):
        self.act = Activity(1, "Journey Call - PNG Fix", jira_key="QDM-1842")

    def test_empty_query_matches_everything(self):
        self.assertTrue(qdm_matches(self.act, ""))
        self.assertTrue(qdm_matches(self.act, "   "))

    def test_name_substring(self):
        self.assertTrue(qdm_matches(self.act, "png"))
        self.assertTrue(qdm_matches(self.act, "JOURNEY"))
        self.assertFalse(qdm_matches(self.act, "railcard"))

    def test_full_jira_key(self):
        self.assertTrue(qdm_matches(self.act, "qdm-1842"))
        self.assertTrue(qdm_matches(self.act, "QDM-1842"))

    def test_number_only(self):
        self.assertTrue(qdm_matches(self.act, "1842"))
        self.assertTrue(qdm_matches(self.act, "184"))
        self.assertFalse(qdm_matches(self.act, "9999"))

    def test_haystack_contains_name_and_number(self):
        hay = qdm_haystack(self.act)
        self.assertIn("png", hay)
        self.assertIn("qdm-1842", hay)
        self.assertIn("1842", hay)

    def test_combo_rows_include_key_label(self):
        names, hays, labels = qdm_combo_rows([self.act])
        self.assertEqual(names, ["Journey Call - PNG Fix"])
        self.assertEqual(labels, ["QDM-1842  Journey Call - PNG Fix"])
        self.assertIn("1842", hays[0])


class TestSearchFilterInPlace(unittest.TestCase):
    def test_typing_does_not_rebuild_rows(self):
        import tempfile
        import tkinter as tk
        from app.db import Database
        from app.sidebar import Sidebar

        root = tk.Tk()
        root.withdraw()
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        db = Database(handle.name)
        try:
            sb = Sidebar(
                root, db, on_change=lambda: None,
                open_activity_panel=lambda **_k: None,
                open_project_panel=lambda **_k: None)
            before = [id(item["widget"]) for item in sb._filter_items
                      if item["kind"] == "activity"]
            self.assertTrue(before)
            sb._search_placeholder_on = False
            sb.search_var.set("sprint")
            sb._flush_search()
            after = [id(item["widget"]) for item in sb._filter_items
                     if item["kind"] == "activity"]
            self.assertEqual(before, after)
            visible = [item for item in sb._filter_items
                       if item["kind"] == "activity" and item.get("_vis")]
            self.assertTrue(visible)
            self.assertTrue(all(item.get("_match") for item in visible))
            root.update_idletasks()
            self.assertGreater(sb.search_entry.winfo_reqheight(), 8)
            self.assertGreater(sb.search_entry.winfo_height(), 0)
        finally:
            db.close()
            root.destroy()
            os.unlink(handle.name)

    def test_collapsed_project_skips_activity_widgets(self):
        import tempfile
        import tkinter as tk
        from app.db import Database
        from app.models import Activity, Project
        from app.sidebar import Sidebar

        root = tk.Tk()
        root.withdraw()
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        db = Database(handle.name)
        try:
            pid = db.add_project(Project(None, "Collapsed Group", "#4C6EF5"))
            db.add_activity(Activity(None, "Hidden QDM", None, 30, project_id=pid))
            db.set_project_collapsed(pid, True)
            sb = Sidebar(
                root, db, on_change=lambda: None,
                open_activity_panel=lambda **_k: None,
                open_project_panel=lambda **_k: None)
            hays = [item.get("haystack", "") for item in sb._filter_items
                    if item["kind"] == "activity"]
            self.assertFalse(any("hidden qdm" in (h or "") for h in hays))
            self.assertTrue(any(
                item["kind"] == "project" and item.get("project_id") == pid
                for item in sb._filter_items))
        finally:
            db.close()
            root.destroy()
            os.unlink(handle.name)


if __name__ == "__main__":
    unittest.main()
