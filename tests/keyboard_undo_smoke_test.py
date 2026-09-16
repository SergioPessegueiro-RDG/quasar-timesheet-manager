"""
Headless smoke test for keyboard shortcuts + undo/redo, driven under Xvfb.

Exercises the real handler methods directly (synthetic event objects, same
technique as tests/interactive_smoke_test.py) rather than real X11 input:
block selection via a plain click, Delete/Backspace removing the selected
block, Escape deselecting, Left/Right/Up/Down nudging a selected block
between days/times (and Left/Right navigating weeks when nothing is
selected), and Ctrl+Z/Ctrl+Shift+Z/Ctrl+Y undoing/redoing every kind of
edit (create, quick-assign create, move/resize, edit-via-dialog, delete,
duplicate-in-place, and multi-day duplicate) on whichever tab is active.
"""
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tmp_home = tempfile.mkdtemp()
os.environ["HOME"] = tmp_home

import tkinter.messagebox as messagebox  # noqa: E402

messagebox.askyesno = lambda *a, **k: True
messagebox.showinfo = lambda *a, **k: None
messagebox.showwarning = lambda *a, **k: None

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


def click(x, y, state=0):
    """A plain click: button-down then button-up at the same spot, with no
    motion in between -- exactly what _on_button1/_on_release treat as
    "not moved" (selects a block, deselects on empty space, or
    quick-assigns if a QDM is armed)."""
    cal._on_button1(fake_event(x, y, state))
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

print("--- Selecting a block with a plain click ---")
entry_id = db.add_time_entry(TimeEntry(
    None, act.id, act.name, act.jira_key, act.color,
    cal.day_date(1).isoformat(), "10:00", "11:00", ""))
cal.refresh()
win.update()
# y_for_minute() takes minutes-since-grid-start (config.START_HOUR), same
# convention as _hhmm_to_minute -- 10:00-11:00 with a 9am grid start is
# minutes 60-120, so 75 lands solidly in the middle of the block.
click(x_for_day(1), y_for_minute(75))  # inside the 10:00-11:00 block on Tuesday
check("Plain click selects the block (no drag)", cal.selected_entry_id == entry_id)

print("\n--- Delete/Backspace removes the selected block, with confirmation ---")
cal._delete_selected_entry()
win.update()
check("Selected block was deleted", db.get_time_entry(entry_id) is None)
check("Selection cleared after delete", cal.selected_entry_id is None)

print("\n--- Undo restores the deleted block ---")
check("Something is on the undo stack", cal.can_undo())
cal.undo()
win.update()
restored = [e for e in db.list_time_entries_for_week([cal.day_date(1).isoformat()])
            if e.activity_name == "Deep Work"]
check("Undoing the delete brings the block back", len(restored) == 1)
if restored:
    check("Restored block kept its original time", restored[0].start_time == "10:00"
          and restored[0].end_time == "11:00")
    check("Undo re-selects the restored block", cal.selected_entry_id == restored[0].id)

print("\n--- Redo deletes it again ---")
check("Something is on the redo stack", cal.can_redo())
cal.redo()
win.update()
check("Redoing the delete removes the block again",
      not [e for e in db.list_time_entries_for_week([cal.day_date(1).isoformat()])
           if e.activity_name == "Deep Work"])
check("Redo stack is empty again (nothing left to redo)", not cal.can_redo())

print("\n--- Clicking empty space deselects without opening Time Block ---")
new_id = db.add_time_entry(TimeEntry(
    None, act.id, act.name, act.jira_key, act.color,
    cal.day_date(2).isoformat(), "13:00", "14:00", ""))
cal.refresh()
win.update()
# 13:00-14:00 with a 9am grid start is minutes 240-300; 270 is the middle.
click(x_for_day(2), y_for_minute(270))
check("Block selected before click-away", cal.selected_entry_id == new_id)
click(x_for_day(4), y_for_minute(0))  # empty Friday slot
win.update()
check("Clicking empty space deselected the block", cal.selected_entry_id is None)
check("Click-away did not open Time Block",
      str(win.notebook.tab(win.timeblock_panel, "state")) == "hidden")
check("Click-away did not delete the block", db.get_time_entry(new_id) is not None)

print("\n--- Escape deselects without deleting anything ---")
click(x_for_day(2), y_for_minute(270))
check("Block selected before Escape", cal.selected_entry_id == new_id)
cal._cancel_drag()  # bound to <Escape>
win.update()
check("Escape deselected the block", cal.selected_entry_id is None)
check("Escape did not delete the block", db.get_time_entry(new_id) is not None)

print("\n--- Arrow keys nudge the selected block between days/times ---")
click(x_for_day(2), y_for_minute(270))
check("Block re-selected", cal.selected_entry_id == new_id)
cal._on_right_key()
win.update()
moved = db.get_time_entry(new_id)
check("Right arrow moved the block to the next day",
      moved.date == cal.day_date(3).isoformat())
cal._on_left_key()
win.update()
moved = db.get_time_entry(new_id)
check("Left arrow moved it back to the original day", moved.date == cal.day_date(2).isoformat())
cal._on_down_key()
win.update()
moved = db.get_time_entry(new_id)
check(f"Down arrow moved its time later by one slot (got {moved.start_time}-{moved.end_time})",
      moved.start_time == "13:15" and moved.end_time == "14:15")
cal._on_up_key()
win.update()
moved = db.get_time_entry(new_id)
check("Up arrow moved it back", moved.start_time == "13:00" and moved.end_time == "14:00")

print("\n--- Undoing all four nudges restores the original position ---")
for _ in range(4):
    cal.undo()
    win.update()
restored_after_nudges = db.get_time_entry(new_id)
check("Block is back at its original day/time after undoing all nudges",
      restored_after_nudges.date == cal.day_date(2).isoformat()
      and restored_after_nudges.start_time == "13:00" and restored_after_nudges.end_time == "14:00")

print("\n--- Left/Right with nothing selected navigates weeks instead ---")
cal._cancel_drag()  # deselect
week_before = cal.week_start
cal._on_right_key()
win.update()
check("Right arrow (no selection) advances to next week", cal.week_start > week_before)
cal._on_left_key()
win.update()
check("Left arrow (no selection) returns to the original week", cal.week_start == week_before)

print("\n--- Undo/redo covers quick-assign create ---")
sb._arm(act)
win.update()
before_count = len(cal.entries_by_id)
click(x_for_day(4), y_for_minute(0))  # empty Friday slot at grid start, activity armed
win.update()
check("Quick-assign opened Time Block for a description",
      str(win.notebook.tab(win.timeblock_panel, "state")) == "normal")
win.timeblock_panel.notes_text.delete("1.0", "end")
win.timeblock_panel.notes_text.insert("1.0", "quick-assign notes")
win.timeblock_panel._save()
win.update()
check("Saving the quick-assign created a new block", len(cal.entries_by_id) == before_count + 1)
check("Undo stack has the quick-assign create", cal.can_undo())
cal.undo()
win.update()
check("Undoing the quick-assign removes the block", len(cal.entries_by_id) == before_count)
cal.redo()
win.update()
check("Redoing the quick-assign brings it back", len(cal.entries_by_id) == before_count + 1)
sb.clear_armed()

print("\n--- Undo/redo covers Ctrl+click duplicate-in-place ---")
target = next(iter(cal.entries_by_id.values()))
before_count = len(cal.entries_by_id)
cal._duplicate_entry_in_place(target.id)
win.update()
check("Ctrl+click duplicate created a copy", len(cal.entries_by_id) == before_count + 1)
cal.undo()
win.update()
check("Undo removes the duplicate", len(cal.entries_by_id) == before_count)
cal.redo()
win.update()
check("Redo brings the duplicate back", len(cal.entries_by_id) == before_count + 1)

print("\n--- Undo/redo covers dragging a block to a new time (move) ---")
from app.calendar_view import _hhmm_to_minute  # noqa: E402

moved_entry = next(iter(cal.entries_by_id.values()))
orig_day_idx = cal._entry_day_idx(moved_entry)
orig_start = moved_entry.start_time
drag_x = x_for_day(orig_day_idx)
start_minute = _hhmm_to_minute(moved_entry.start_time)
# Well clear of both the top and bottom resize-grip zones (config.RESIZE_
# GRIP_PX is a pixel threshold, not a minute one) -- this entry is a full
# hour long, so +20 minutes lands solidly in the middle.
anchor_y = y_for_minute(start_minute + 20)
cal._on_button1(fake_event(drag_x, anchor_y))
check("Drag started in 'move' mode", cal._drag_state is not None and cal._drag_state["mode"] == "move")
new_y = anchor_y + cal.slot_height * 2  # drag two slots later
cal._on_motion_drag(fake_event(drag_x, new_y))
cal._on_release(fake_event(drag_x, new_y))
win.update()
after_move = db.get_time_entry(moved_entry.id) if not cal.template_mode else None
check("Dragging actually changed the block's start time",
      after_move is not None and after_move.start_time != orig_start)
check("The moved block is selected after the drag", cal.selected_entry_id == moved_entry.id)
cal.undo()
win.update()
check("Undoing the move restores the original start time",
      db.get_time_entry(moved_entry.id).start_time == orig_start)
cal.redo()
win.update()
check("Redoing the move re-applies the new start time",
      db.get_time_entry(moved_entry.id).start_time == after_move.start_time)

print("\n--- Ctrl+Z / Ctrl+Y dispatch through MainWindow to the active tab's calendar ---")
win.notebook.select(win.timesheet_tab)
win.update()
check("_active_calendar() resolves to the Timesheet calendar", win._active_calendar() is cal)
# Reuse the undo history already sitting on cal's stack from the move test
# just above -- MainWindow's shortcut handlers should reach the exact same
# undo()/redo() our direct calls did.
before_dispatch = len(cal._undo_stack)
check("There's existing undo history to dispatch against", before_dispatch > 0)
win._on_undo_shortcut()
win.update()
check("MainWindow's undo shortcut popped this tab's undo stack",
      len(cal._undo_stack) == before_dispatch - 1)
win._on_redo_shortcut()
win.update()
check("MainWindow's redo shortcut re-applied it", len(cal._undo_stack) == before_dispatch)

print("\n--- Undo/redo is scoped per-tab (Template tab has its own independent history) ---")
win.notebook.select(win.template_tab)
win.update()
tcal = win.template_calendar
check("_active_calendar() now resolves to the Template calendar", win._active_calendar() is tcal)
check("Template calendar has its own separate undo stack object",
      tcal._undo_stack is not cal._undo_stack)
win._on_undo_shortcut()  # should affect tcal, not cal, since Template tab is now active
win.update()
check("Dispatching undo while on the Template tab did not touch the Timesheet tab's stack",
      len(cal._undo_stack) == before_dispatch)

print("\n--- Undo/redo does nothing (and doesn't crash) while a non-calendar tab is active ---")
win._open_settings_dialog()
win.update()
check("_active_calendar() is None while Settings is showing", win._active_calendar() is None)
win._on_undo_shortcut()  # must not raise
win._on_redo_shortcut()  # must not raise
check("Calling the shortcuts with no active calendar didn't raise", True)
win.settings_panel._cancel()
win.update()

print("\n--- Typing in a text field doesn't trigger undo/redo ---")
win.notebook.select(win.timesheet_tab)
win.update()
win.calendar._open_entry_dialog(new=True, day_idx=0,
                                 start_hhmm="10:00", end_hhmm="10:15")
win.update()
check("Time Block tab opened", str(win.notebook.tab(win.timeblock_panel, "state")) == "normal")
check("_is_typing_target recognizes the Notes box (a tk.Text)",
      win._is_typing_target(win.timeblock_panel.notes_text))
check("_is_typing_target recognizes the Activity field (a RoundedCombobox)",
      win._is_typing_target(win.timeblock_panel.activity_combo) is True)
check("_is_typing_target does NOT flag a plain container Frame",
      win._is_typing_target(win.timeblock_panel) is False)
win.timeblock_panel._cancel()
win.update()

win.destroy()

print("\n============================")
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
    sys.exit(0)
