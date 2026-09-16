"""
Headless smoke test (Xvfb) for the Timer button: pick an activity, Start,
Stop -- and it should log a time block for today, rounded to the nearest
15 minutes, refresh the Timesheet tab, and survive a theme-toggle rebuild
without losing whatever's currently running.

The timer's own elapsed-time clock (app.timer_bar's `datetime.now()`) is
replaced with a fully deterministic fake clock we advance by hand, rather
than backdating against the real wall clock -- that would make the test's
pass/fail depend on what real hour of the day it happens to run at (e.g.
"simulate a start at 06:00" only produces positive elapsed time if it's
run after 6am), which is exactly the kind of flakiness a regression test
shouldn't have.
"""
import os
import sys
import tempfile
from datetime import date, datetime, time as dtime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tmp_home = tempfile.mkdtemp()
os.environ["HOME"] = tmp_home

import tkinter.messagebox as messagebox  # noqa: E402

warnings = []
messagebox.askyesno = lambda *a, **k: True
messagebox.askyesnocancel = lambda *a, **k: True
messagebox.showinfo = lambda *a, **k: None
messagebox.showwarning = lambda *a, **k: warnings.append((a, k))
messagebox.showerror = lambda *a, **k: print("  [error dialog]:", a, k)

import app.timer_bar as timer_bar_module  # noqa: E402


class _Clock:
    # Pinned to today's real date (so it still lines up with what
    # MainWindow._go_today jumps to, and with date.today()-based queries
    # below) but at a fixed, safely-mid-day time -- not the actual current
    # wall-clock time. The scenarios below drift this forward by a few
    # hours in total; anchoring to real datetime.now() instead would make
    # the test's pass/fail depend on what real hour it happens to run at
    # (a run starting late at night could drift across midnight).
    now = datetime.combine(date.today(), dtime(9, 0))


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _Clock.now


timer_bar_module.datetime = _FrozenDatetime


def advance(minutes):
    _Clock.now = _Clock.now + timedelta(minutes=minutes)


def hhmm(dt):
    return dt.strftime("%H:%M")


from app.main_window import MainWindow  # noqa: E402
from app.models import Activity, Project, TimeEntry  # noqa: E402
from app import theme  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


win = MainWindow()
win.update()

db = win.db
proj = db.get_project(db.add_project(Project(None, "Client Alpha", "#4C6EF5")))
act_a = db.get_activity(db.add_activity(Activity(None, "Deep Work", "PROJ-1", 30, project_id=proj.id)))
act_b = db.get_activity(db.add_activity(Activity(None, "Standup", "PROJ-2", 15, project_id=proj.id)))
win.timer_bar.refresh_activities()


def entries_today():
    return db.list_time_entries_for_week([date.today().isoformat()])


def time_block_open():
    try:
        return str(win.notebook.tab(win.notebook.select(), "text")) == "Time Block"
    except Exception:
        return False


def save_timer_description(text="from the timer"):
    panel = win.timeblock_panel
    panel.notes_text.delete("1.0", "end")
    panel.notes_text.insert("1.0", text)
    panel._save()
    win.update()


print("--- Timer bar exists and its activity picker is populated ---")
check("MainWindow has a timer_bar", hasattr(win, "timer_bar"))
tb = win.timer_bar
check("Timer starts idle", not tb.is_running())
check("Picker shows Select a QDM until one is chosen",
      tb.activity_var.get() == "Select a QDM…")
check("Start Timer is disabled until a QDM is chosen",
      tb.toggle_btn.cget("state") == "disabled")
values = list(tb.activity_combo.cget("values"))
check("Activity picker lists both seeded activities", "Deep Work" in values and "Standup" in values)

print("\n--- Starting without picking an activity warns and doesn't start ---")
tb.activity_var.set("")
check("Start stays disabled with an empty picker",
      tb.toggle_btn.cget("state") == "disabled")
warnings.clear()
tb._start()
check("A warning was shown", len(warnings) == 1)
check("Timer did not start", not tb.is_running())

print("\n--- Start -> Stop asks for a description before logging ---")
tb.activity_var.set("Deep Work")
tb.activity_combo.set("Deep Work")
check("Start enables once a QDM is chosen",
      tb.toggle_btn.cget("state") == "normal")
t0 = _Clock.now
tb._start()
check("Timer is running after Start", tb.is_running())
check("Activity picker is locked while running",
      str(tb.activity_combo.cget("state")) == "disabled")
check("Button now reads Stop Timer", tb.toggle_btn.cget("text") == "Stop Timer")
check("start_dt was captured at Start time", tb.start_dt == t0)

advance(47)  # round_duration_minutes(47) -> 45
before = len(entries_today())
tb._stop()
win.update()

check("Timer is idle again after Stop", not tb.is_running())
check("Activity picker is unlocked again", str(tb.activity_combo.cget("state")) == "readonly")
check("Time Block tab opened for a description", time_block_open())
check("No time entry is saved until description is confirmed",
      len(entries_today()) == before)
panel = win.timeblock_panel
check("Description is required on the Time Block tab", panel._require_notes)
panel._save()
win.update()
check("Save without a description is blocked", len(entries_today()) == before)
check("The form asks for a description",
      "description" in panel.error_label.cget("text").lower())
save_timer_description("hooked up the API")
after = entries_today()
check("Exactly one new time entry was logged", len(after) == before + 1)
logged = next(e for e in after if e.start_time == hhmm(t0))
check(f"Logged entry is for the right activity (got {logged.activity_name!r})",
      logged.activity_name == "Deep Work")
check(f"~47 elapsed minutes rounded to 45 (got {logged.start_time}-{logged.end_time})",
      logged.end_time == hhmm(t0 + timedelta(minutes=45)))
check("The description was stored on the block", logged.notes == "hooked up the API")
check("Status label shows what was logged", "Logged 45 min to Deep Work" in tb.status_label.cget("text"))
today_week_start = date.today() - timedelta(days=date.today().weekday())
check("Timer bar jumped the Timesheet calendar to today's week",
      win.calendar.week_start == today_week_start)
check("Timesheet tab is selected after logging", win.notebook.index(win.notebook.select()) == 0)

print("\n--- Truly zero elapsed time (Stop at the exact instant of Start) logs nothing ---")
advance(60)
tb.activity_var.set("Standup")
tb.activity_combo.set("Standup")
tb._start()
before = len(entries_today())
tb._stop()  # no advance() at all -- exactly 0 elapsed
win.update()
check("A genuinely 0-minute timer logs no entry", len(entries_today()) == before)
check("Status label says nothing was logged", "nothing logged" in tb.status_label.cget("text"))

print("\n--- Cancel on the description prompt discards the time ---")
advance(60)
tb.activity_var.set("Standup")
tb.activity_combo.set("Standup")
tb._start()
advance(15)
before = len(entries_today())
tb._stop()
win.update()
check("Cancel path still opens Time Block first", time_block_open())
win.timeblock_panel._cancel()
win.update()
check("Cancel does not log a time entry", len(entries_today()) == before)
check("Cancel status says nothing was logged",
      "nothing logged" in tb.status_label.cget("text"))

print("\n--- A timer that ran only a moment still logs the 15-minute floor ---")
advance(60)  # a clean gap before the next block
t1 = _Clock.now
tb.activity_var.set("Standup")
tb.activity_combo.set("Standup")
tb._start()
advance(1)  # a brief but nonzero moment -- still real elapsed time, unlike a literal 0
before = len(entries_today())
tb._stop()
win.update()
save_timer_description("standup notes")
after = entries_today()
check("A near-instant timer still logs one entry (15-minute floor)", len(after) == before + 1)
floor_entry = next(e for e in after if e.start_time == hhmm(t1))
check(f"Floor entry is a 15-minute block (got {floor_entry.start_time}-{floor_entry.end_time})",
      floor_entry.end_time == hhmm(t1 + timedelta(minutes=15)))

print("\n--- Stopping into an existing block logs it anyway, no prompt (overlaps render side by side) ---")
# Overlapping an existing block used to ask "log it anyway?" via
# messagebox.askyesno; that prompt was removed once the calendar started
# rendering overlapping blocks side by side instead of rejecting them (see
# CalendarGrid._layout_day_entries). Prove the timer now just logs straight
# through an overlap, and that askyesno is never even consulted for it.
advance(60)
t2 = _Clock.now
conflict_start = hhmm(t2 + timedelta(minutes=20))
conflict_end = hhmm(t2 + timedelta(minutes=40))
db.add_time_entry(TimeEntry(None, act_b.id, act_b.name, act_b.jira_key, act_b.color,
                             date.today().isoformat(), conflict_start, conflict_end, ""))

askyesno_calls = []
messagebox.askyesno = lambda *a, **k: askyesno_calls.append((a, k)) or True

tb.activity_var.set("Deep Work")
tb.activity_combo.set("Deep Work")
tb._start()
advance(30)  # t2 -> t2+30, which overlaps [t2+20, t2+40)
before = len(entries_today())
tb._stop()
win.update()
save_timer_description("overlap notes")
after = entries_today()
check("Stopping into an overlapping slot still logs a new entry", len(after) == before + 1)
check("No overlap prompt was shown (askyesno was never called)", len(askyesno_calls) == 0)
check("Status label shows it was logged normally", "Logged" in tb.status_label.cget("text"))

overlapping_today = [e for e in after
                      if e.start_time < conflict_end and conflict_start < e.end_time]
check("Both the conflicting entries are present and time-overlapping",
      len(overlapping_today) == 2)
day_idx = win.calendar._entry_day_idx(overlapping_today[0])
layout = win.calendar._layout_day_entries(
    [e for e in win.calendar.entries_by_id.values()
     if win.calendar._entry_day_idx(e) == day_idx])
cols = {layout[e.id] for e in overlapping_today}
check("The calendar lays the two overlapping entries out in separate side-by-side columns",
      {c[1] for c in cols} == {2} and {c[0] for c in cols} == {0, 1})

messagebox.askyesno = lambda *a, **k: True  # restore the default for the rest of the run

print("\n--- A running timer survives a theme-change rebuild ---")
advance(120)
t3 = _Clock.now
tb.activity_var.set("Standup")
tb.activity_combo.set("Standup")
tb._start()
advance(15)  # 15 minutes elapsed at the moment of the theme change

win._select_theme("sleek_indigo")
win.update()
check("timer_bar was rebuilt into a new widget instance", win.timer_bar is not tb)
tb2 = win.timer_bar
check("The new timer bar picked up the running state", tb2.is_running())
check("The running activity survived the rebuild", tb2.activity_var.get() == "Standup")
check("start_dt survived the rebuild unchanged", tb2.start_dt == t3)

advance(15)  # another 15 minutes after resuming -> 30 total
before = len(entries_today())
tb2._stop()
win.update()
save_timer_description("resumed timer")
after = entries_today()
check("Stopping the resumed timer logs correctly", len(after) == before + 1)
resumed_entry = next(e for e in after if e.start_time == hhmm(t3))
check(f"Elapsed time across the rebuild was preserved (got {resumed_entry.start_time}-{resumed_entry.end_time})",
      resumed_entry.end_time == hhmm(t3 + timedelta(minutes=30)))

win._select_theme(theme.DEFAULT_THEME_ID)  # back to the default theme for the rest of the run
win.update()
tb3 = win.timer_bar

print("\n--- Closing the app while the timer is running offers to log it first ---")
advance(120)
t4 = _Clock.now
tb3.activity_var.set("Deep Work")
tb3.activity_combo.set("Deep Work")
tb3._start()
before = len(entries_today())

closed = {"called": False}
real_destroy = win.destroy
real_db_close = win.db.close
win.destroy = lambda: closed.__setitem__("called", True)
win.db.close = lambda: None  # so this test can still query the db afterward

messagebox.askyesnocancel = lambda *a, **k: None  # Cancel: don't close
win._on_close()
check("Cancel on the close prompt leaves the app open", not closed["called"])
check("Cancel on the close prompt leaves the timer running", tb3.is_running())

advance(47)
messagebox.askyesnocancel = lambda *a, **k: True  # Yes: log it, then close
win._on_close()
check("Yes on the close prompt closes the app", closed["called"])
after = entries_today()
check("Yes on the close prompt logged the running timer first", len(after) == before + 1)
closing_entry = next(e for e in after if e.start_time == hhmm(t4))
check(f"The time logged on close matches what actually elapsed (got {closing_entry.end_time})",
      closing_entry.end_time == hhmm(t4 + timedelta(minutes=45)))

win.destroy = real_destroy
win.db.close = real_db_close
win.destroy()

print("\n============================")
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
