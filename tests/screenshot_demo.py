"""Populate a demo week and keep the window open briefly for a screenshot."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["HOME"] = tempfile.mkdtemp()

from app.main_window import MainWindow
from app.models import Activity, Project, TimeEntry
from app import config

win = MainWindow()
win.update()
cal = win.calendar
db = win.db
general_id = db.add_project(Project(None, "Demo", config.DEFAULT_PROJECT_COLORS[0]))
client_id = db.add_project(Project(None, "Client Alpha", config.DEFAULT_PROJECT_COLORS[3]))
for name, jira_key, dur, project_id in (
        ("Team Standup", None, 15, general_id),
        ("Sprint Planning", None, 60, general_id),
        ("Code Review", None, 30, general_id),
        ("Development", "QDM-100", 120, client_id),
):
    db.add_activity(Activity(None, name, jira_key, dur, project_id=project_id))
activities = db.list_activities()
by_name = {a.name: a for a in activities}

demo = [
    ("Team Standup", 0, "09:00", "09:15", ""),
    ("Sprint Planning", 0, "09:30", "10:30", "Plan sprint 24 backlog + capacity"),
    ("Development", 0, "11:00", "13:00", "Implement CSV export edge cases"),
    ("Code Review", 1, "10:00", "10:30", "Review PR #482 (billing service)"),
    ("Development", 1, "13:00", "16:00", "Bug bash + fix flaky test suite"),
    ("Team Standup", 2, "09:00", "09:15", ""),
    ("Development", 3, "09:30", "12:00", "Pairing with Sam on auth refactor"),
    ("Code Review", 4, "14:00", "15:00", "Final review before release cut"),
]
for name, day_idx, start, end, notes in demo:
    a = by_name[name]
    db.add_time_entry(TimeEntry(None, a.id, a.name, a.jira_key, a.color,
                                 cal.day_date(day_idx).isoformat(), start, end, notes))

cal.refresh()
win.update()
win.geometry("1240x780+0+0")
win.update()

with open("/tmp/screenshot_ready", "w") as f:
    f.write("ready")

win.after(6000, win.destroy)
win.mainloop()
