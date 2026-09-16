"""
Headless smoke test for the Summary tab, driven under Xvfb.

Seeds a handful of TimeEntry rows across the current week and the previous
month, then drives the real SummaryPanel (via MainWindow, exactly as a user
would reach it through the notebook) to check: the QDM (bar chart) and
Project (pie chart) breakdowns' totals/percentages for the week view (both always visible side
by side now, rather than one you toggled between), switching to month view
and back, Prev/Next/Today navigation, the empty-state message for a period
with nothing logged, and that switching onto the Summary tab (via
<<NotebookTabChanged>>) refreshes its numbers to reflect an entry added on
another tab in the meantime.
"""
import os
import sys
import tempfile
from datetime import timedelta

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


def row_texts(rows_frame):
    """Every legend row's rendered text, as a list -- one joined string
    per row (swatch has no text, so only the name/hours labels contribute)."""
    return [
        " ".join(c.cget("text") for c in row.winfo_children() if hasattr(c, "cget") and "text" in c.keys())
        for row in rows_frame.winfo_children()
    ]


win = MainWindow()
win.update()
db = win.db
summary = win.summary_panel

proj = db.get_project(db.add_project(Project(None, "Client Alpha Work", "#4C6EF5")))
deep_work = db.get_activity(db.add_activity(Activity(None, "Deep Work", "PROJ-1", 60, project_id=proj.id)))
meetings = db.get_activity(db.add_activity(Activity(None, "Meetings", "PROJ-2", 60, project_id=proj.id)))

week_start = win.calendar.week_start  # a Monday, per CalendarGrid's own convention

print("--- Seeding entries across the current week ---")
db.add_time_entry(TimeEntry(None, deep_work.id, deep_work.name, deep_work.jira_key, deep_work.color,
                             week_start.isoformat(), "09:00", "12:00", ""))  # 3h Monday
db.add_time_entry(TimeEntry(None, deep_work.id, deep_work.name, deep_work.jira_key, deep_work.color,
                             (week_start + timedelta(days=1)).isoformat(), "09:00", "10:00", ""))  # 1h Tuesday
db.add_time_entry(TimeEntry(None, meetings.id, meetings.name, meetings.jira_key, meetings.color,
                             (week_start + timedelta(days=2)).isoformat(), "13:00", "14:00", ""))  # 1h Wednesday

print("\n--- Switching to the Summary tab shows this week's totals in both breakdowns ---")
win.notebook.select(win.summary_tab)
win.update()
check("_active_calendar() is None while Summary tab is active", win._active_calendar() is None)
check("Summary panel defaults to week mode", summary.mode == "week")

check("QDM total label reflects 5 hours across 2 QDMs",
      summary._qdm["total_label"].cget("text") == "Total: 5.0h across 2 QDMs")
check("Project total label rolls both QDMs up into 1 project at 5.0h",
      summary._project["total_label"].cget("text") == "Total: 5.0h across 1 project")

# Deep Work (4h/80%) should be listed above Meetings (1h/20%) in the QDM
# column -- rows sorted by descending minutes.
qdm_rows = row_texts(summary._qdm["rows_frame"])
check("Two QDM rows are rendered", len(qdm_rows) == 2)
if len(qdm_rows) == 2:
    check("Deep Work (the larger total) is listed first",
          "Deep Work" in qdm_rows[0] and "4.0h" in qdm_rows[0] and "80%" in qdm_rows[0])
    check("Meetings is listed second with 1.0h (20%)",
          "Meetings" in qdm_rows[1] and "1.0h" in qdm_rows[1] and "20%" in qdm_rows[1])

# The Project column rolls Deep Work + Meetings up into a single
# "Client Alpha Work" row -- always shown alongside the QDM column now,
# with no toggle needed to see it.
project_rows = row_texts(summary._project["rows_frame"])
check("Grouping by project collapses the two QDMs into a single 'Client Alpha Work' row",
      len(project_rows) == 1)
if project_rows:
    check("The single project row is named after the Project, totals both QDMs' hours",
          "Client Alpha Work" in project_rows[0] and "5.0h" in project_rows[0] and "100%" in project_rows[0])

print("\n--- Switching to Month mode aggregates the whole month ---")
summary._set_mode("month")
win.update()
check("Mode switched to month", summary.mode == "month")
check("Month view still totals 5.0h across 2 QDMs (same entries, wider window)",
      summary._qdm["total_label"].cget("text") == "Total: 5.0h across 2 QDMs")

print("\n--- Prev/Next navigation moves the anchor and Today returns ---")
today_text = summary.period_label.cget("text")
summary._prev()
win.update()
prev_text = summary.period_label.cget("text")
check("Prev month changed the period label", prev_text != today_text)
check("Prev month with no entries shows the empty state in both columns",
      summary._qdm["total_label"].cget("text") == "Total: 0.0h across 0 QDMs"
      and summary._project["total_label"].cget("text") == "Total: 0.0h across 0 projects")
summary._today()
win.update()
check("Today() returns to the original month label", summary.period_label.cget("text") == today_text)

summary._set_mode("week")
win.update()
summary._prev()
win.update()
check("Prev week (no entries) shows the empty-state message in the QDM column",
      any(w.cget("text") == "No time logged in this period."
          for w in summary._qdm["rows_frame"].winfo_children() if hasattr(w, "cget") and "text" in w.keys()))
summary._today()
win.update()

print("\n--- Adding an entry on the Timesheet tab, then switching to Summary, refreshes totals ---")
win.notebook.select(win.timesheet_tab)
win.update()
db.add_time_entry(TimeEntry(None, meetings.id, meetings.name, meetings.jira_key, meetings.color,
                             (week_start + timedelta(days=3)).isoformat(), "10:00", "11:00", ""))  # +1h Meetings
win.notebook.select(win.summary_tab)
win.update()
check("Switching onto the Summary tab auto-refreshed to include the new entry (6.0h total)",
      summary._qdm["total_label"].cget("text") == "Total: 6.0h across 2 QDMs")

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
