"""Summary pie labels: thin wedges stay unlabeled so names don't overlap."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.summary_panel import slice_label_kind, wrap_slice_name


class TestSliceLabelKind(unittest.TestCase):
    def test_six_percent_wedge_is_legend_only(self):
        self.assertEqual(slice_label_kind(360 * 0.06), "none")

    def test_medium_wedge_gets_just_the_percent(self):
        self.assertEqual(slice_label_kind(32), "pct")

    def test_large_wedge_keeps_the_name(self):
        self.assertEqual(slice_label_kind(360 * 0.24), "name")
        self.assertEqual(slice_label_kind(360 * 0.65), "name")


class TestWrapSliceName(unittest.TestCase):
    def test_short_name_is_unchanged(self):
        self.assertEqual(wrap_slice_name("Railcards"), "Railcards")

    def test_long_name_stops_at_two_lines(self):
        text = wrap_slice_name(
            "QUASAR Improvement - Railcard App Automation", 16, 2)
        self.assertLessEqual(len(text.split("\n")), 2)

    def test_empty_name_is_empty(self):
        self.assertEqual(wrap_slice_name(""), "")
        self.assertEqual(wrap_slice_name("   "), "")


class TestDrawPieSkipsThinSlices(unittest.TestCase):
    def test_thin_wedges_are_not_named_on_the_ring(self):
        import tkinter as tk
        from app.summary_panel import SummaryPanel

        root = tk.Tk()
        try:
            canvas = tk.Canvas(root, width=400, height=400, bg="#FFFFFF", highlightthickness=0)
            canvas.pack()
            root.update_idletasks()
            root.withdraw()
            panel = SummaryPanel.__new__(SummaryPanel)
            panel.family = "TkDefaultFont"
            rows = [
                {"name": "Large Project", "color": "#4C6EF5", "minutes": 390},
                {"name": "Medium Project", "color": "#12B886", "minutes": 144},
                {"name": "Thin A - Salesforce Platform Release Winter 27",
                 "color": "#FAB005", "minutes": 36},
                {"name": "Thin B - DPRC Eligibility", "color": "#FD7E14", "minutes": 30},
            ]
            panel._draw_pie(canvas, rows, 600)
            texts = [
                canvas.itemcget(item, "text")
                for item in canvas.find_all()
                if canvas.type(item) == "text"
            ]
            blob = "\n".join(texts)
            self.assertIn("65%", blob)
            self.assertIn("24%", blob)
            self.assertNotIn("Winter 27", blob)
            self.assertNotIn("DPRC", blob)
            self.assertNotIn("6%", blob)
        finally:
            root.destroy()

    def test_default_canvas_size_does_not_crash(self):
        import tkinter as tk
        from app.summary_panel import SummaryPanel

        root = tk.Tk()
        root.withdraw()
        try:
            canvas = tk.Canvas(root, bg="#FFFFFF", highlightthickness=0)
            panel = SummaryPanel.__new__(SummaryPanel)
            panel.family = "TkDefaultFont"
            panel._draw_pie(
                canvas, [{"name": "One", "color": "#4C6EF5", "minutes": 60}], 60)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
