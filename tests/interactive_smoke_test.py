"""
Headless interactive smoke test, driven under Xvfb.

Exercises the real Tkinter event handlers (drag-create, resize, move,
quick-assign, right-click delete, activity/project CRUD, export) by
constructing synthetic event objects and calling the bound handler methods
directly -- this covers the actual production code paths without needing a
real mouse/X11 input driver.
"""
import os
import sys
import tempfile
import tkinter as tk
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Isolate DB in a temp HOME so this never touches a real user's data.
tmp_home = tempfile.mkdtemp()
os.environ["HOME"] = tmp_home

import tkinter.messagebox as messagebox  # noqa: E402

# Silence all blocking dialogs during the automated run.
messagebox.askyesno = lambda *a, **k: True
messagebox.askyesnocancel = lambda *a, **k: True  # "Yes" -> keep entries
messagebox.showinfo = lambda *a, **k: None
messagebox.showwarning = lambda *a, **k: print("  [warning dialog]:", a, k)
messagebox.showerror = lambda *a, **k: print("  [error dialog]:", a, k)

from app.main_window import MainWindow  # noqa: E402
from app import config as cfg  # noqa: E402
from app.models import Activity, Project, TimeEntry  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def fake_event(x, y, state=0):
    # state=0 matches a real click event with no modifier keys held --
    # CalendarGrid._on_button1/_on_hover check event.state for Ctrl
    # (CONTROL_STATE_MASK) to detect the Ctrl+click-duplicate shortcut.
    return types.SimpleNamespace(x=x, y=y, x_root=x, y_root=y, state=state)


def y_for_minute(minute):
    # `cal` is assigned below, once the window exists; Python resolves this
    # global at call time, not def time, and every call happens after that.
    # Reads the grid's *live* (dynamically resized) geometry rather than the
    # static config.py defaults, since the calendar now stretches to fill
    # its window and those can differ once the window is realized.
    return cal.header_height + minute * cal.px_per_min


def x_for_day(day_idx):
    return cal.gutter_width + day_idx * cal.day_width + 10


win = MainWindow()
win.update()
cal = win.calendar
sb = win.sidebar
db = win.db

print("\n--- Initial state ---")
activities = db.list_activities()
check("Fresh database has no placeholder QDMs", len(activities) == 0)

# The rest of this script exercises real activity/calendar paths, so add
# a handful of local test QDMs (not the old first-run demo seed).
test_project_id = db.add_project(Project(None, "Test Work", cfg.DEFAULT_PROJECT_COLORS[0]))
client_id = db.add_project(Project(None, "Client Alpha", cfg.DEFAULT_PROJECT_COLORS[3]))
for name, jira_key, dur, project_id in (
        ("Sprint Planning", None, 60, test_project_id),
        ("Team Standup", None, 15, test_project_id),
        ("Code Review", None, 30, test_project_id),
        ("Development", "QDM-100", 120, client_id),
):
    db.add_activity(Activity(None, name, jira_key, dur, project_id=project_id))
sb.refresh()
win.update()
activities = db.list_activities()
check("Test activities are available for the rest of this script", len(activities) == 4)
check("Calendar starts with 0 entries this week", len(cal.entries_by_id) == 0)

print("\n--- Quick-assign (arm + click empty slot) ---")
sprint_planning = next(a for a in activities if a.name == "Sprint Planning")
sb._arm(sprint_planning)
check("Activity armed", sb.armed_activity_id == sprint_planning.id)
check("Calendar hint mentions activity name", "Sprint Planning" in cal.hint_label.cget("text"))

# Monday (day 0), 9:00am slot -- minute 0 in grid-local minutes.
ev = fake_event(x_for_day(0), y_for_minute(0))
cal._on_button1(ev)
cal._on_release(ev)  # plain click, no movement
win.update()

entries = db.list_time_entries_for_week([cal.day_date(i).isoformat() for i in range(5)])
check("Quick-assign created exactly 1 entry", len(entries) == 1)
if entries:
    e = entries[0]
    check("Quick-assigned entry has correct activity", e.activity_name == "Sprint Planning")
    check("Quick-assigned entry starts at 09:00", e.start_time == "09:00")
    check("Quick-assigned entry duration matches default (60min)", e.duration_minutes() == 60)

print("\n--- Creating a block with times that overlap an existing one is now ALLOWED ---")
# Drag-create has to start on genuinely empty canvas -- starting the drag
# on top of the already-rendered 09:00-10:00 block would be interpreted as
# clicking *that* block (move/resize), not creating a new one underneath
# it. So this drags out a block in clearly empty space (14:00-15:00) and
# then, in the dialog, explicitly retimes it to 09:30-10:30 -- which does
# overlap -- to prove the SAVE path itself no longer rejects that.
empty_start_ev = fake_event(x_for_day(0), y_for_minute(300))  # Monday, 14:00
cal._on_button1(empty_start_ev)
empty_move_ev = fake_event(x_for_day(0), y_for_minute(360))  # drag to 15:00
cal._on_motion_drag(empty_move_ev)
cal._on_release(empty_move_ev)
win.update()
overlap_panel = win.timeblock_panel
check("Dialog opened for the empty-space drag-create",
      str(win.notebook.tab(overlap_panel, "state")) == "normal")
overlap_panel.activity_var.set("Sprint Planning")
overlap_panel._on_activity_changed()
overlap_panel.start_var.set("09:30")
overlap_panel.start_combo.set("09:30")
overlap_panel.end_var.set("10:30")
overlap_panel.end_combo.set("10:30")
overlap_panel._save()
win.update()

entries_after = db.list_time_entries_for_week([cal.day_date(i).isoformat() for i in range(5)])
mon_entries_after = [e for e in entries_after if e.date == cal.day_date(0).isoformat()]
check("The retimed-to-overlap block was still saved (2 entries on Monday now)",
      len(mon_entries_after) == 2)

layout = cal._layout_day_entries(mon_entries_after)
cols = {layout[e.id] for e in mon_entries_after}
check("Both overlapping entries were assigned 2 side-by-side columns (not stacked/hidden)",
      {c[1] for c in cols} == {2} and {c[0] for c in cols} == {0, 1})

# Clean up the overlapping entry this check added, so the rest of this
# test file's assumptions about "exactly 1 entry on Monday" still hold.
for e in mon_entries_after:
    if e.id != entries[0].id:
        db.delete_time_entry(e.id)
cal.refresh()
win.update()

sb.clear_armed()
check("Activity un-armed after cancel", sb.armed_activity_id is None)

print("\n--- Edit panel on an EXISTING entry pre-fills the readonly comboboxes ---")
# Regression check: a readonly combobox's displayed text must reflect
# an early textvariable.set() (this silently rendered blank in testing
# until the form was reordered to populate values before focus is set).
# Assert against the widgets' own .get(), not just the backing StringVars,
# since that's what actually broke.
#
# The time-block editor is an embedded "Time Block" tab (not a pop-up
# window) -- see app/timeblock_panel.py for why.
mon_entry_for_edit = entries[0]
cal._edit_entry(mon_entry_for_edit)
win.update()
check("Time Block tab becomes visible for editing",
      str(win.notebook.tab(win.timeblock_panel, "state")) == "normal")
check("Time Block tab is the one selected", win.notebook.select() == str(win.timeblock_panel))
panel = win.timeblock_panel
check("Edit panel's Activity combobox display shows the entry's activity",
      panel.activity_combo.get() == "Sprint Planning")
check("Edit panel's Day combobox display is populated (not blank)",
      panel.day_combo.get() != "")
check("Edit panel's Start combobox display shows 09:00",
      panel.start_combo.get() == "09:00")
check("Edit panel's End combobox display shows 10:00",
      panel.end_combo.get() == "10:00")
panel._cancel()
win.update()
check("Time Block tab hides again after Cancel",
      str(win.notebook.tab(win.timeblock_panel, "state")) == "hidden")
check("Cancel returns focus to the Timesheet tab", win.notebook.index(win.notebook.select()) == 0)

print("\n--- Drag-to-create opens dialog, save creates entry ---")
opened_dialogs = []
orig_dialog_init = None
import app.calendar_view as cvmod

original_open = cal._open_entry_dialog


def spy_open_entry_dialog(*args, **kwargs):
    opened_dialogs.append((args, kwargs))
    return original_open(*args, **kwargs)


cal._open_entry_dialog = spy_open_entry_dialog

start_ev = fake_event(x_for_day(1), y_for_minute(120))  # Tuesday, 11:00
cal._on_button1(start_ev)
move_ev = fake_event(x_for_day(1), y_for_minute(240))  # drag down to 13:00 (past threshold)
cal._on_motion_drag(move_ev)
cal._on_release(move_ev)
win.update()

check("Drag-create opened the entry panel", len(opened_dialogs) == 1)

# Drive the embedded Time Block tab like a user would (it should already be
# showing and selected -- see app/timeblock_panel.py).
check("Time Block tab is visible after drag-create",
      str(win.notebook.tab(win.timeblock_panel, "state")) == "normal")
panel = win.timeblock_panel
check("Panel pre-filled start time", panel.start_var.get() == "11:00")
check("Panel pre-filled end time", panel.end_var.get() == "13:00")
panel.activity_var.set("Code Review")
panel._on_activity_changed()
panel._save()
win.update()
check("Time Block tab hides again after Save",
      str(win.notebook.tab(win.timeblock_panel, "state")) == "hidden")

entries_tue = db.list_time_entries_for_week([cal.day_date(i).isoformat() for i in range(5)])
tue_entry = next((e for e in entries_tue if e.date == cal.day_date(1).isoformat()), None)
check("Drag-created entry saved to DB", tue_entry is not None)
if tue_entry:
    check("Drag-created entry has chosen activity", tue_entry.activity_name == "Code Review")
    check("Drag-created entry spans 11:00-13:00", (tue_entry.start_time, tue_entry.end_time) == ("11:00", "13:00"))

print("\n--- Resize an entry by dragging its bottom edge ---")
mon_entry = next(e for e in db.list_time_entries_for_week(
    [cal.day_date(i).isoformat() for i in range(5)]) if e.date == cal.day_date(0).isoformat())
cal.refresh()
win.update()
_, y0, _, y1 = cal._entry_geometry(mon_entry, 0)
grip_ev = fake_event(x_for_day(0), y1 - 1)  # near bottom edge
region = cal._hit_region(mon_entry.id, y1 - 1)
check("Hit-test detects bottom resize grip", region == "resize-bottom")

cal._on_button1(grip_ev)
resize_ev = fake_event(x_for_day(0), y_for_minute(150))  # extend to 11:30
cal._on_motion_drag(resize_ev)
cal._on_release(resize_ev)
win.update()

resized = db.get_time_entry(mon_entry.id)
check("Resize updated end_time", resized.end_time == "11:30")
check("Resize kept start_time", resized.start_time == "09:00")

print("\n--- Move an entry to a different day by dragging its middle ---")
cal.refresh()
win.update()
_, y0, _, y1 = cal._entry_geometry(resized, 0)
mid_y = (y0 + y1) / 2
move_start = fake_event(x_for_day(0), mid_y)
region2 = cal._hit_region(resized.id, mid_y)
check("Hit-test detects move region in the middle", region2 == "move")

cal._on_button1(move_start)
move_target = fake_event(x_for_day(3), mid_y)  # drag to Thursday, same time-of-day
cal._on_motion_drag(move_target)
cal._on_release(move_target)
win.update()

moved = db.get_time_entry(resized.id)
check("Move changed the entry's date to Thursday", moved.date == cal.day_date(3).isoformat())
check("Move preserved duration", moved.duration_minutes() == resized.duration_minutes())

print("\n--- Right-click delete ---")
before_count = len(db.list_time_entries_for_week([cal.day_date(i).isoformat() for i in range(5)]))
cal._delete_entry(moved)
win.update()
after_count = len(db.list_time_entries_for_week([cal.day_date(i).isoformat() for i in range(5)]))
check("Right-click delete removed the entry", after_count == before_count - 1)

print("\n--- Duplicate a block to other weekdays via right-click ---")
# The Duplicate dialog is now an embedded "Duplicate" tab, not a pop-up --
# see app/panels.py.
source_entry = next(e for e in db.list_time_entries_for_week(
    [cal.day_date(i).isoformat() for i in range(5)]) if e.date == cal.day_date(1).isoformat())
cal._open_duplicate_dialog(source_entry)
win.update()
check("Duplicate tab becomes visible",
      str(win.notebook.tab(win.duplicate_panel, "state")) == "normal")
dpanel = win.duplicate_panel
check("Source day is not offered as a duplicate target",
      1 not in dpanel.day_vars)
# Check Wednesday and Thursday (day indices 2 and 3).
dpanel.day_vars[2].set(True)
dpanel.day_vars[3].set(True)
dpanel._duplicate()
win.update()
check("Duplicate tab hides again after duplicating",
      str(win.notebook.tab(win.duplicate_panel, "state")) == "hidden")

week_dates_now = [cal.day_date(i).isoformat() for i in range(5)]
all_entries_now = db.list_time_entries_for_week(week_dates_now)
wed_dup = [e for e in all_entries_now if e.date == cal.day_date(2).isoformat()
           and e.activity_name == source_entry.activity_name]
thu_dup = [e for e in all_entries_now if e.date == cal.day_date(3).isoformat()
           and e.activity_name == source_entry.activity_name]
check("Duplicate created a copy on Wednesday", len(wed_dup) == 1)
check("Duplicate created a copy on Thursday", len(thu_dup) == 1)
if wed_dup:
    check("Duplicated copy keeps the same time range",
          (wed_dup[0].start_time, wed_dup[0].end_time) == (source_entry.start_time, source_entry.end_time))
    check("Duplicated copy has its own id (independent row)", wed_dup[0].id != source_entry.id)

print("\n--- Duplicating onto a day that already has the block now OVERLAPS it (side by side) ---")
before_wed_count = len(wed_dup)
cal._open_duplicate_dialog(source_entry)
win.update()
dpanel2 = win.duplicate_panel
dpanel2.day_vars[2].set(True)  # Wednesday (day index 2) again -> now overlaps on purpose
dpanel2._duplicate()
win.update()
wed_after = [e for e in db.list_time_entries_for_week(week_dates_now)
             if e.date == cal.day_date(2).isoformat() and e.activity_name == source_entry.activity_name]
check("Duplicate onto an already-occupied day DOES create a second, overlapping copy",
      len(wed_after) == before_wed_count + 1)
wed_layout = cal._layout_day_entries(wed_after)
check("The two overlapping Wednesday copies got separate side-by-side columns",
      {wed_layout[e.id] for e in wed_after} == {(0, 2), (1, 2)})

print("\n--- Ctrl+click on a block duplicates it in place ---")
# By this point in the file, Monday's original block has long since been
# moved to Thursday (the "Move an entry to a different day" section above)
# and then deleted (the "Right-click delete" section) -- there's nothing
# left at Monday 09:00 to click. `source_entry` (Tuesday, still exactly one
# entry there since nothing else in this file touches Tuesday) is a block
# that's actually still on screen at this point, so use that instead.
cal.refresh()
win.update()
before_all_count = len(db.list_time_entries_for_week(week_dates_now))
sx0, sy0, sx1, sy1 = cal._entry_geometry(source_entry, 1)
click_x, click_y = (sx0 + sx1) / 2, (sy0 + sy1) / 2
found_id = cal._entry_id_at(click_x, click_y)
check("The synthetic click lands on the Tuesday source block", found_id == source_entry.id)
ctrl_ev = fake_event(click_x, click_y, state=0x4)  # Control held
cal._on_button1(ctrl_ev)
win.update()
after_ctrl_click = db.list_time_entries_for_week(week_dates_now)
check("Ctrl+click created exactly one duplicate (not a drag/resize)",
      len(after_ctrl_click) == before_all_count + 1)
check("Ctrl+click did not start a drag/resize", cal._drag_state is None)
tue_now = [e for e in after_ctrl_click if cal._entry_day_idx(e) == 1
           and e.activity_name == source_entry.activity_name]
check("Both the original and the Ctrl+click duplicate are on Tuesday, same time",
      len(tue_now) == 2 and len({(e.start_time, e.end_time) for e in tue_now}) == 1)

print("\n--- Notes are prioritized in the block's rendered text lines ---")
noted_entry = TimeEntry(999, None, "Focus Block", None, "#4C6EF5", cal.day_date(0).isoformat(),
                         "09:00", "10:00", "Reviewing PR #482 for the billing service")
lines = cal._entry_text_lines(noted_entry, 0, 0, 200, 90)
check("Rendered lines include the activity name", any("Focus Block" in t for t, *_ in lines))
check("Rendered lines include the notes text (for differentiating activities)",
      any("Reviewing PR" in t for t, *_ in lines))

tiny_lines = cal._entry_text_lines(noted_entry, 0, 0, 200, 20)
check("A very short block still renders at least the activity name",
      len(tiny_lines) >= 1 and "Focus Block" in tiny_lines[0][0])

print("\n--- Activity ('Add QDM' tab) CRUD via panel ---")
# Add/Edit Activity is now an embedded "Add QDM" tab, not a pop-up -- see
# app/panels.py.
sb._add_activity()
win.update()
check("Add QDM tab becomes visible",
      str(win.notebook.tab(win.activity_panel, "state")) == "normal")
apanel = win.activity_panel
check("Add QDM heading reads \"Add QDM\" when creating a new activity",
      apanel.heading.cget("text") == "Add QDM")
check("Jira Project/Issue Type fields were removed from the Add QDM tab (they're always the "
      "same fixed values now -- see app/config.py)",
      not hasattr(apanel, "jira_project_var") and not hasattr(apanel, "issue_type_var"))
apanel.name_var.set("Design Review")
# Jira Issue Key is a number-only field now -- "QDM-" is prepended
# automatically (see app/config.py's jira_key_from_number).
apanel.jira_key_number_var.set("42")
apanel.duration_var.set("45")
apanel._save()
win.update()
check("Add QDM tab hides again after Save",
      str(win.notebook.tab(win.activity_panel, "state")) == "hidden")

new_acts = db.list_activities()
check("New activity was added, with the QDM- prefix filled in automatically",
      any(a.name == "Design Review" and a.jira_key == "QDM-42" for a in new_acts))
check("New activity has no per-activity Jira Project/Issue Type (falls back to this app's fixed defaults)",
      any(a.name == "Design Review" and a.jira_project is None and a.issue_type is None for a in new_acts))
design_review = next(a for a in new_acts if a.name == "Design Review")

print("\n--- Project CRUD via panel ---")
# "+ Project" is an embedded "Project" tab, same pattern as Activity -- see
# app/panels.py's ProjectPanel.
sb._add_project()
win.update()
check("Project tab becomes visible",
      str(win.notebook.tab(win.project_panel, "state")) == "normal")
ppanel = win.project_panel
ppanel.name_var.set("Client A")
ppanel._save()
win.update()
check("Project tab hides again after Save",
      str(win.notebook.tab(win.project_panel, "state")) == "hidden")

all_projects = db.list_projects()
check("New project was added", any(p.name == "Client A" for p in all_projects))
client_a = next(p for p in all_projects if p.name == "Client A")
check("New project starts expanded (not collapsed)", client_a.collapsed is False)

print("\n--- Assigning an activity to a project via the Add QDM tab ---")
sb._edit_activity(design_review)
win.update()
apanel2 = win.activity_panel
check("Add QDM heading reads \"Edit QDM\" when editing an existing activity",
      apanel2.heading.cget("text") == "Edit QDM")
check("Edit QDM has a Jira Status dropdown so a ticket can be closed without logging time",
      hasattr(apanel2, "status_combo"))
check("Edit QDM status starts on don't-change",
      "Don't change" in (apanel2.status_var.get() or ""))
check("Add QDM tab's Project dropdown defaults to the activity's current project",
      apanel2.project_var.get() == db.get_project(design_review.project_id).name)
apanel2.project_var.set("Client A")
apanel2.project_combo.set("Client A")
apanel2._save()
win.update()

design_review_reloaded = db.get_activity(design_review.id)
check("Activity is now grouped into the Client A project",
      design_review_reloaded.project_id == client_a.id)

print("\n--- Creating a new project inline from the Add QDM tab ---")
sb._add_activity()
win.update()
apanel3 = win.activity_panel
check("New Project Name field starts hidden",
      not apanel3.new_project_label.winfo_ismapped())
apanel3.project_var.set(apanel3.project_combo["values"][-1])
apanel3._on_project_changed()
win.update()
check("Choosing \"+ New Project...\" reveals the New Project Name field",
      apanel3.new_project_label.winfo_ismapped())
apanel3.name_var.set("Inline Activity")
apanel3.jira_key_number_var.set("77")
apanel3.new_project_name_var.set("Inline Client")
apanel3._save()
win.update()

inline_project = next((p for p in db.list_projects() if p.name == "Inline Client"), None)
check("Selecting \"+ New Project...\" and saving created the new project",
      inline_project is not None)
inline_activity = next((a for a in db.list_activities() if a.name == "Inline Activity"), None)
check("The new activity was saved against the newly created project",
      inline_activity is not None and inline_project is not None
      and inline_activity.project_id == inline_project.id)


def sidebar_row_texts():
    """Flatten every text Label currently rendered in the sidebar's scrollable
    row list, in on-screen order, so project headers vs. activity rows can be
    told apart by content without depending on internal widget structure."""
    out = []

    def walk(widget):
        if isinstance(widget, tk.Label):
            out.append(widget.cget("text"))
        for child in widget.winfo_children():
            walk(child)

    for row in sb.list_frame.winfo_children():
        walk(row)
    return out

rendered = sidebar_row_texts()
check("Project header 'Client A' is rendered in the sidebar",
      any("Client A" in t for t in rendered))
check("Grouped activity 'Design Review' is rendered under its project",
      any(t == "Design Review" for t in rendered))
project_header_idx = next(i for i, t in enumerate(rendered) if "Client A" in t)
activity_idx = next(i for i, t in enumerate(rendered) if t == "Design Review")
check("The grouped activity is rendered after its project header (i.e. nested under it)",
      activity_idx > project_header_idx)

print("\n--- Collapsing a project hides its activities ---")
sb._toggle_project(client_a)
win.update()
check("Project is now marked collapsed in the DB",
      db.get_project(client_a.id).collapsed is True)
rendered_collapsed = sidebar_row_texts()
check("Collapsed project still shows its header",
      any("Client A" in t for t in rendered_collapsed))
check("Collapsed project hides its grouped activity",
      not any(t == "Design Review" for t in rendered_collapsed))

sb._toggle_project(db.get_project(client_a.id))
win.update()
check("Project is expanded again in the DB",
      db.get_project(client_a.id).collapsed is False)
rendered_expanded = sidebar_row_texts()
check("Expanded project shows its grouped activity again",
      any(t == "Design Review" for t in rendered_expanded))

print("\n--- Renaming a project ---")
client_a_live = db.get_project(client_a.id)
sb._edit_project(client_a_live)
win.update()
ppanel2 = win.project_panel
check("Project tab pre-fills the existing name", ppanel2.name_var.get() == "Client A")
ppanel2.name_var.set("Client A (Renamed)")
ppanel2._save()
win.update()
check("Project rename persisted", db.get_project(client_a.id).name == "Client A (Renamed)")

print("\n--- Deleting a project moves its activities to General by default ---")
# messagebox.askyesnocancel is stubbed to always return True at the top of
# this script -- for project delete that maps to "Yes" = keep activities.
sb._delete_project(db.get_project(client_a.id))
win.update()
check("Project was deleted", db.get_project(client_a.id) is None)
design_review_after_delete = db.get_activity(design_review.id)
check("Its activity was kept, moved into the catch-all General project",
      design_review_after_delete is not None
      and db.get_project(design_review_after_delete.project_id).name == "General")

print("\n--- Deleting a project can also delete its activities ---")
pid2 = db.add_project(Project(None, "Temp Project", "#4C6EF5"))
temp_act_id = db.add_activity(Activity(None, "Temp Grouped Activity", project_id=pid2))
sb.refresh()
win.update()
orig_askyesnocancel = messagebox.askyesnocancel
messagebox.askyesnocancel = lambda *a, **k: False  # "No" = also delete its activities
try:
    sb._delete_project(db.get_project(pid2))
finally:
    messagebox.askyesnocancel = orig_askyesnocancel
win.update()
check("Project (delete-activities-too path) was deleted", db.get_project(pid2) is None)
check("Its activity was deleted too", db.get_activity(temp_act_id) is None)

print("\n--- Week navigation ---")
wk0 = cal.week_start
cal._next_week()
win.update()
check("Next-week button advances 7 days", (cal.week_start - wk0).days == 7)
cal._prev_week()
win.update()
check("Prev-week button returns to original week", cal.week_start == wk0)

print("\n--- Jira CSV export end-to-end through the UI path ---")
from app.export_csv import export_entries

db.set_setting("jira_display_name", "Alex Rae")
week_dates = [cal.day_date(i).isoformat() for i in range(5)]
db.add_time_entry(TimeEntry(
    None, sprint_planning.id, sprint_planning.name, sprint_planning.jira_key or "PROJ-1",
    sprint_planning.color, week_dates[0], "14:00", "15:00", "export test"
))
entries_for_export = db.list_time_entries_between(week_dates[0], week_dates[-1])
csv_path = os.path.join(tmp_home, "export_test.csv")
written, skipped = export_entries(entries_for_export, csv_path,
                                   db.get_setting("jira_display_name", ""))
check("CSV export wrote at least 1 row", written >= 1)
check("Exported CSV file exists", os.path.isfile(csv_path))
with open(csv_path) as f:
    content = f.read()
check("CSV header correct",
      content.splitlines()[0] == "Project,Issue Type,Key,Date Started,Display Name,"
                                  "Time Spent (h),Work Description")
check("CSV row uses this app's fixed default Project/Issue Type",
      "Quasar Delivery Management,Sub-task," in content)
print("  CSV content preview:\n" + "\n".join("    " + line for line in content.splitlines()))

print("\n--- Template tab: drag-create, quick-assign, edit, duplicate ---")
tcal = win.template_calendar
tsb = win.template_sidebar
check("Template tab starts empty", len(tcal.entries_by_id) == 0)
check("Template calendar is in template_mode", tcal.template_mode is True)


def tx_for_day(day_idx):
    return tcal.gutter_width + day_idx * tcal.day_width + 10


def ty_for_minute(minute):
    return tcal.header_height + minute * tcal.px_per_min


# Quick-assign a recurring Monday standup via the Template tab's own sidebar.
standup = next(a for a in db.list_activities() if a.name == "Team Standup")
tsb._arm(standup)
qa_ev = fake_event(tx_for_day(0), ty_for_minute(0))
tcal._on_button1(qa_ev)
tcal._on_release(qa_ev)
win.update()
tsb.clear_armed()
win.update()

template_entries = db.list_template_entries()
check("Quick-assign created a recurring Monday template entry", len(template_entries) == 1)
if template_entries:
    te = template_entries[0]
    check("Template entry is on Monday (day_of_week 0)", te.day_of_week == 0)
    check("Template entry has correct activity", te.activity_name == "Team Standup")

print("\n--- Template tab: drag-create + edit via the shared Time Block tab ---")
t_start_ev = fake_event(tx_for_day(2), ty_for_minute(120))  # Wednesday, 11:00
tcal._on_button1(t_start_ev)
t_move_ev = fake_event(tx_for_day(2), ty_for_minute(180))  # drag to 12:00
tcal._on_motion_drag(t_move_ev)
tcal._on_release(t_move_ev)
win.update()
check("Time Block tab opened for the Template tab's drag-create",
      str(win.notebook.tab(win.timeblock_panel, "state")) == "normal")
tpanel = win.timeblock_panel
check("Panel's Day dropdown shows a plain weekday name (no date) in template mode",
      tpanel.day_var.get() == "Wednesday")
tpanel.activity_var.set("Code Review")
tpanel._on_activity_changed()
tpanel._save()
win.update()

template_entries = db.list_template_entries()
check("Drag-create added a second recurring template entry", len(template_entries) == 2)
wed_template = next((t for t in template_entries if t.day_of_week == 2), None)
check("New template entry is on Wednesday", wed_template is not None)
if wed_template:
    check("New template entry spans 11:00-12:00",
          (wed_template.start_time, wed_template.end_time) == ("11:00", "12:00"))

print("\n--- Template tab: duplicate a recurring block to another weekday ---")
tcal._open_duplicate_dialog(wed_template)
win.update()
check("Duplicate tab opened from the Template tab", win.notebook.select() == str(win.duplicate_panel))
tdpanel = win.duplicate_panel
check("Source weekday is not offered as a duplicate target", 2 not in tdpanel.day_vars)
tdpanel.day_vars[4].set(True)  # Friday
tdpanel._duplicate()
win.update()
template_entries = db.list_template_entries()
check("Duplicate added a Friday recurring copy", any(t.day_of_week == 4 for t in template_entries))
check("Template tab now has 3 recurring entries", len(template_entries) == 3)

print("\n--- Apply Template to This Week ---")
# The current week already has real blocks on some days from earlier in this
# script (Tuesday/Wednesday/Thursday), so this also exercises the
# overlap-skip path for whichever template day collides with them -- not
# just the happy path.
week_dates_for_apply = [cal.day_date(i).isoformat() for i in range(5)]
before_count = len(db.list_time_entries_for_week(week_dates_for_apply))

cal._apply_template()
win.update()
after_real_entries = db.list_time_entries_for_week(week_dates_for_apply)
check("Applying the template added at least one real entry to the current week",
      len(after_real_entries) > before_count)

mon_from_template = [e for e in after_real_entries if e.date == cal.day_date(0).isoformat()
                      and e.activity_name == "Team Standup"]
check("Applied Monday entry matches the template's activity and time", len(mon_from_template) == 1)
if mon_from_template:
    check("Applied entry keeps the template's start time", mon_from_template[0].start_time == "09:00")

print("\n--- Applying the template again skips already-occupied slots ---")
cal._apply_template()
win.update()
after_second_apply = db.list_time_entries_for_week(week_dates_for_apply)
check("Re-applying the template did not create duplicate entries (all slots already taken)",
      len(after_second_apply) == len(after_real_entries))

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
