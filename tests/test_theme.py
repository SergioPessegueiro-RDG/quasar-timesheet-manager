"""Theme helpers that don't need a window: palette mix, SDF coverage."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import theme


class TestRoundedRectSdf(unittest.TestCase):
    def test_center_is_inside(self):
        dist = theme._sdf_rounded_rect(10, 10, 0, 0, 20, 20, 4)
        self.assertLess(dist, 0)

    def test_outside_corner_is_positive(self):
        dist = theme._sdf_rounded_rect(0, 0, 2, 2, 20, 20, 4)
        self.assertGreater(dist, 0)

    def test_coverage_ramps_across_the_edge(self):
        self.assertEqual(theme._coverage(1.0), 0.0)
        self.assertEqual(theme._coverage(-1.0), 1.0)
        mid = theme._coverage(0.0)
        self.assertGreater(mid, 0.0)
        self.assertLess(mid, 1.0)

    def test_per_corner_backgrounds_paint_cutouts(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            img = theme._make_aa_image(
                40, 40, 12, "#112233", "#000000", "", 0.0,
                backgrounds=("#FF0000", "#00FF00", "#0000FF", "#FFFF00"))

            def rgb(px):
                if isinstance(px, str):
                    parts = px.replace(",", " ").split()
                    return tuple(int(float(p)) for p in parts[:3])
                return tuple(int(v) for v in px[:3])

            self.assertEqual(rgb(img.get(0, 0)), (255, 0, 0))
            self.assertEqual(rgb(img.get(39, 0)), (0, 255, 0))
            self.assertEqual(rgb(img.get(0, 39)), (0, 0, 255))
            self.assertEqual(rgb(img.get(39, 39)), (255, 255, 0))
            self.assertEqual(rgb(img.get(20, 20)), (0x11, 0x22, 0x33))
        finally:
            root.destroy()

    def test_outline_sits_outside_the_fill(self):
        fill = "#4C6EF5"
        outline = "#111111"
        bg = "#FFFFFF"
        centre = theme._aa_pixel(10, 10, (0.5, 0.5, 19.5, 19.5), 6, fill, bg, outline, 1.0)
        self.assertEqual(centre, fill)

    def test_mix_is_used_for_theme_colors(self):
        previous = theme.get_theme_id()
        try:
            theme.set_theme("ink_wash")
            self.assertTrue(theme.ACCENT.startswith("#"))
            theme.set_theme("stormy_morning")
            self.assertTrue(theme.ACCENT.startswith("#"))
        finally:
            theme.set_theme(previous)


class TestPlaceAndAtmosphereThemes(unittest.TestCase):
    def test_white_and_dark_mode_are_the_system_pair(self):
        self.assertEqual(theme.THEMES["white"]["category"], "System")
        self.assertEqual(theme.THEMES["dark_mode"]["category"], "System")
        self.assertEqual(theme.THEMES["white"]["palette"]["PANEL_BG"], "#FFFFFF")
        self.assertTrue(theme._is_dark(theme.THEMES["dark_mode"]["palette"]["PANEL_BG"]))
        self.assertEqual(theme.THEME_ORDER[:3], ["white", "dark_mode", "system"])

    def test_new_scenic_ids_are_in_the_picker(self):
        for theme_id in ("cairo_sun", "fuji_night", "harbor_dusk",
                         "silver_grain", "pack_ice", "orchard_bloom"):
            self.assertIn(theme_id, theme.THEMES)
            self.assertIn(theme.THEMES[theme_id]["category"], ("Places", "Atmosphere"))
            self.assertTrue(theme.THEMES[theme_id]["palette"]["ACCENT"].startswith("#"))
            self.assertIn(theme_id, theme.THEME_ORDER)

    def test_glass_themes_drop_window_alpha(self):
        previous = theme.get_glass_alpha()
        try:
            theme.set_glass_alpha(0.95)
            self.assertEqual(theme.window_alpha("white"), 1.0)
            self.assertAlmostEqual(theme.window_alpha("picom"), 0.95)
            self.assertEqual(theme.clamp_glass_alpha(0.2), theme.WINDOW_ALPHA_MIN)
            self.assertEqual(theme.clamp_glass_alpha(1.5), theme.WINDOW_ALPHA_MAX)
            self.assertEqual(theme.THEMES["picom"]["category"], "Glass")
            self.assertTrue(theme.is_glass_theme("hypr"))
            self.assertFalse(theme.is_glass_theme("white"))
        finally:
            theme.set_glass_alpha(previous)


class TestWindowChrome(unittest.TestCase):
    def test_apply_window_chrome_sets_flag(self):
        import sys
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            theme.apply_window_chrome(root)
            if sys.platform == "darwin":
                self.assertIsInstance(getattr(root, "_themed_titlebar", None), bool)
            else:
                self.assertFalse(root._themed_titlebar)
        finally:
            root.destroy()


class TestRoundedWidgets(unittest.TestCase):
    def test_card_inset_covers_the_corner_radius(self):
        import tkinter as tk
        from app.widgets import CARD_RADIUS, RoundedCard, RoundedCombobox

        root = tk.Tk()
        root.withdraw()
        try:
            card = RoundedCard(root, radius=CARD_RADIUS, outline=False)
            self.assertGreaterEqual(card._inset, CARD_RADIUS)
            flush = RoundedCard(root, radius=CARD_RADIUS, outline=False, pad=0)
            self.assertEqual(flush._inset, 0)

            pill = RoundedCard(root, radius=10, pad=10, outline=False, shrink=True)
            tk.Label(pill.body, text="search").pack()
            pill.pack()
            root.update_idletasks()
            self.assertGreater(pill.winfo_reqheight(), 20)

            var = tk.StringVar()
            combo = RoundedCombobox(root, textvariable=var, values=["A", "B"], width=8)
            combo.set("A")
            self.assertEqual(combo.get(), "A")
            self.assertEqual(combo.current(), 0)
            combo.current(1)
            self.assertEqual(combo.get(), "B")
            combo.config(values=["X", "Y", "Z"])
            self.assertEqual(list(combo.cget("values")), ["X", "Y", "Z"])
            combo.config(state="disabled")
            self.assertEqual(str(combo.cget("state")), "disabled")

            painted = RoundedCard(
                root, bg="#ABCDEF", radius=8, outline=False, outer_bg="#654321")
            self.assertEqual(str(painted.cget("bg")).upper(), "#654321")
            self.assertEqual(str(painted._canvas.cget("bg")).upper(), "#654321")

            from app.widgets import _hex_bg_at
            self.assertEqual(_hex_bg_at(root, 0, 0, "#3AAFA9").upper(), "#3AAFA9")
        finally:
            root.destroy()


class TestDayTotalHours(unittest.TestCase):
    def test_zero_is_plain_0h(self):
        from app.calendar_view import format_day_total_hours
        self.assertEqual(format_day_total_hours(0), "0h")
        self.assertEqual(format_day_total_hours(0.0), "0h")

    def test_nonzero_keeps_one_decimal(self):
        from app.calendar_view import format_day_total_hours
        self.assertEqual(format_day_total_hours(30), "0.5h")
        self.assertEqual(format_day_total_hours(60), "1.0h")
        self.assertEqual(format_day_total_hours(90), "1.5h")
