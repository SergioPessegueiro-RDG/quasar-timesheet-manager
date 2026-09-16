"""
Headless smoke test (Xvfb) for double-clicking a time block to jump
straight to its Edit tab.

Exercises the real handler methods directly with synthetic event objects
(same technique as tests/keyboard_undo_smoke_test.py) rather than real X11
input, since that's what actually reaches Tk's dispatcher in this
environment.

The double-click case specifically mirrors Tk's real event sequence for a
double-click -- <Button-1>/<ButtonRelease-1> fire for BOTH presses that
make up a double-click (that's what a plain single click already does),
and <Double-Button-1> additionally fires only for the second press (Tk
fires only the most-specific matching pattern for a given event -- see the
CONTROL_STATE_MASK comment in app/calendar_view.py for the same rule
applied to Ctrl+click). So this drives all three: first press/release
(ordinary click, selects the block), then the second press's
double-click, then that second press's release too, confirming it's a
safe no-op rather than something left over from the first click's
_drag_state interfering.
"""
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tmp_home = tempfile.mkdtemp()
os.environ["HOME"] = tmp_home

from app.main_window import MainWindow  # noqa: E402
from app.models import Activity, Project, TimeEntry  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def fake_event(x, y, state=0):
    return types.SimpleNamespace(x=x, y=y, x_root=x, y_root=y, state=state)


def y_for_minute(minute):
    return cal.header_height + minute * cal.px_per_min


def x_for_day(day_idx):
    return cal.gutter_width + day_idx * cal.day_width + 10


def double_click(x, y, state=0):
    cal._on_button1(fake_event(x, y, state))
    cal._on_release(fake_event(x, y, state))
    cal._on_double_click(fake_event(x, y, state))
    cal._on_release(fake_event(x, y, state))


win = MainWindow()
win.update()
cal = win.calendar
sb = win.sidebar
db = win.db

proj = db.get_project(db.add_project(Project(None, "Client Alpha", "#4C6EF5")))
act = db.get_activity(db.add_activity(Activity(None, "Deep Work", "PROJ-1", 60, project_id=proj.id)))
sb.refresh()
cal.refresh()
win.update()

print("--- Double-clicking an existing block opens its Edit tab ---")
entry_id = db.add_time_entry(TimeEntry(
    None, act.id, act.name, act.jira_key, act.color,
    cal.day_date(1).isoformat(), "10:00", "11:00", ""))
cal.refresh()
win.update()

# 10:00-11:00 with a 9am grid start is minutes 60-120 -- 75 lands solidly
# inside the block, same convention keyboard_undo_smoke_test.py uses.
double_click(x_for_day(1), y_for_minute(75))
win.update()

check("Time Block tab becomes visible", str(win.notebook.tab(win.timeblock_panel, "state")) == "normal")
check("Time Block tab is selected", win.notebook.select() == str(win.timeblock_panel))
check("Opened in Edit mode, not New", win.timeblock_panel.heading.cget("text") == "Edit Time Block")
check("Loaded with the double-clicked block's own activity",
      win.timeblock_panel.activity_var.get() == "Deep Work")
check("The block is also selected on the calendar underneath",
      cal.selected_entry_id == entry_id)
check("No stray second block was created by the two presses",
      len(cal.entries_by_id) == 1)

win.timeblock_panel._cancel()
win.update()
check("Time Block tab hides again after Cancel",
      str(win.notebook.tab(win.timeblock_panel, "state")) == "hidden")

print("\n--- Double-clicking empty calendar space does nothing extra ---")
# Empty-space double-click: the FIRST press/release of it is an ordinary
# empty-space click, which now deselects (or no-ops) instead of opening
# a blank New Time Block. What this section actually checks is that
# _on_double_click's own contribution stays a no-op on top of that (no
# crash, no dialog, no phantom entry), since _entry_id_at finds no block
# at an empty spot.
win.timeblock_panel._cancel()
win.update()
entries_before = len(cal.entries_by_id)
double_click(x_for_day(3), y_for_minute(300))
win.update()
check("Empty-space double-click does not open Time Block",
      str(win.notebook.tab(win.timeblock_panel, "state")) == "hidden")
check("Nothing is created without a drag",
      len(cal.entries_by_id) == entries_before)
win.timeblock_panel._cancel()
win.update()

print("\n--- A plain single click still just selects (no dialog) ---")
cal._on_button1(fake_event(x_for_day(1), y_for_minute(75)))
cal._on_release(fake_event(x_for_day(1), y_for_minute(75)))
win.update()
check("Plain click selects without opening the Time Block tab",
      cal.selected_entry_id == entry_id
      and str(win.notebook.tab(win.timeblock_panel, "state")) == "hidden")

print("\n============================")
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
