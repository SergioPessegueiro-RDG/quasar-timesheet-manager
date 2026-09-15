"""
Headless smoke test for the theme system, driven under Xvfb.

Covers: selecting a theme actually flips app.theme's live palette, the
choice is persisted to (and read back from) the settings table, plain tk
widgets that don't auto-repaint on a ttk style change (frames/labels/
canvases/the notes Text box) get rebuilt with the new colors, in-memory UI
state (current week, unsaved-to-disk-none-the-less entries) survives the
rebuild that a theme change triggers, and a legacy "light"/"dark" (or old
seven-palette) setting value is mapped onto a sensible modern theme
instead of crashing.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tmp_home = tempfile.mkdtemp()
os.environ["HOME"] = tmp_home

from app.main_window import MainWindow  # noqa: E402
from app import theme  # noqa: E402
from app.models import TimeEntry  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


print("--- Default state ---")
win = MainWindow()
win.update()

check("Starts on the default theme", theme.get_theme_id() == theme.DEFAULT_THEME_ID)
check("Root bg matches the default theme's APP_BG",
      win.cget("bg") == theme.get_theme(theme.DEFAULT_THEME_ID)["palette"]["APP_BG"])
check("No saved theme_mode setting yet", win.db.get_setting("theme_mode") is None)

print("\n--- State survives a theme-change-triggered rebuild ---")
# Move to a non-default week and add an entry, to verify UI state survives
# the destroy-and-rebuild that happens when the theme changes.
win.calendar._next_week()
win.update()
week_before = win.calendar.week_start
db = win.db
act = db.list_activities()[0]
db.add_time_entry(TimeEntry(None, act.id, act.name, act.jira_key, act.color,
                             win.calendar.day_date(0).isoformat(), "09:00", "10:00", "theme test"))
win.calendar.refresh()
win.update()
check("Seeded a non-default week with an entry", len(win.calendar.entries_by_id) == 1)

print("\n--- Switch to Bubblegum Pop ---")
win._select_theme("bubblegum_pop")
win.update()

check("theme.get_theme_id() flips to bubblegum_pop", theme.get_theme_id() == "bubblegum_pop")
check("Setting persisted to db", win.db.get_setting("theme_mode") == "bubblegum_pop")
check("Root bg updates to Bubblegum Pop's APP_BG",
      win.cget("bg") == theme.get_theme("bubblegum_pop")["palette"]["APP_BG"])
check("Week selection survived the rebuild", win.calendar.week_start == week_before)
check("Entries survived the rebuild (re-read from DB)", len(win.calendar.entries_by_id) == 1)

# Regression check: the Notes box is a raw tk.Text (not ttk), which does
# NOT auto-repaint on a style change -- it must be explicitly colored when
# (re)created, or it silently stays hardcoded white forever. The time-block
# editor is now an embedded "Time Block" tab (not a pop-up window) -- see
# app/timeblock_panel.py.
win.calendar._open_entry_dialog(new=True, day_idx=1,
                                 start_hhmm="09:00", end_hhmm="10:00")
win.update()
check("Time Block tab opened", str(win.notebook.tab(win.timeblock_panel, "state")) == "normal")
check("Notes box (tk.Text) is themed for Bubblegum Pop, not left hardcoded white",
      win.timeblock_panel.notes_text.cget("bg") == theme.get_theme("bubblegum_pop")["palette"]["FIELD_BG"])
win.timeblock_panel._cancel()
win.update()

print("\n--- Settings panel's theme picker reflects and can change the active theme ---")
win._open_settings_dialog()
win.update()
check("Settings tab opened", str(win.notebook.tab(win.settings_tab, "state")) == "normal")
check("Theme picker shows the currently-active theme selected",
      win.settings_panel.theme_var.get() == "bubblegum_pop")
check("System + curated palettes plus Custom are offered",
      set(win.settings_panel.theme_swatch_canvases.keys()) == set(theme.THEME_ORDER) | {theme.CUSTOM_THEME_ID})
win.settings_panel._select_theme("sandstone")
win.update()
check("Clicking a swatch updates the panel's pending selection (not yet applied)",
      win.settings_panel.theme_var.get() == "sandstone")
check("...but the live app theme hasn't changed yet (Save wasn't clicked)",
      theme.get_theme_id() == "bubblegum_pop")
win.settings_panel._save()
win.update()
check("Saving applies the newly-picked theme", theme.get_theme_id() == "sandstone")
check("Saving persists the newly-picked theme", win.db.get_setting("theme_mode") == "sandstone")

print("\n--- Back to the default theme ---")
win._select_theme(theme.DEFAULT_THEME_ID)
win.update()
check("theme.get_theme_id() flips back to the default", theme.get_theme_id() == theme.DEFAULT_THEME_ID)
check("Setting persisted back to the default", win.db.get_setting("theme_mode") == theme.DEFAULT_THEME_ID)
check("Root bg back to the default theme's APP_BG",
      win.cget("bg") == theme.get_theme(theme.DEFAULT_THEME_ID)["palette"]["APP_BG"])

win.destroy()

print("\n--- A fresh MainWindow reads back a persisted preference ---")
theme.set_theme(theme.DEFAULT_THEME_ID)  # reset module state between MainWindow instances in this script
win2 = MainWindow()
win2.update()
win2.db.set_setting("theme_mode", "magenta_pulse")
win2.destroy()

theme.set_theme(theme.DEFAULT_THEME_ID)
win3 = MainWindow()
win3.update()
check("A fresh MainWindow reads back a persisted 'magenta_pulse' preference on launch",
      theme.get_theme_id() == "magenta_pulse")
win3.destroy()

print("\n--- Legacy light/dark settings values (pre-theme-picker) still work ---")
theme.set_theme(theme.DEFAULT_THEME_ID)
win4 = MainWindow()
win4.update()
win4.db.set_setting("theme_mode", "dark")  # what old versions of the app stored
win4.destroy()

theme.set_theme(theme.DEFAULT_THEME_ID)
win5 = MainWindow()
win5.update()
check("A legacy 'dark' setting maps onto a real theme instead of crashing",
      theme.get_theme_id() in theme.THEMES)
check("...specifically the closest modern equivalent (stormy_morning)",
      theme.get_theme_id() == "stormy_morning")
win5.destroy()

print("\n============================")
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
