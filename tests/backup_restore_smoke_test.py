"""
Headless smoke test for the "Backup" tab (File -> Backup & Restore…), driven
under Xvfb.

The actual file-picking is native OS dialogs (tkinter.filedialog), which
can't be driven headlessly -- exactly like colorchooser.askcolor elsewhere
in this app, these are monkeypatched to return a fixed path instead of
opening a real dialog (see tests/interactive_smoke_test.py for the same
pattern applied to other native dialogs). What's actually under test is
everything after the path is chosen: BackupPanel wiring its buttons to
MainWindow's callbacks, MainWindow calling Database.backup_to/restore_from,
the timer-running guard, the confirmation prompt, and the full window
rebuild that reflects restored data (including a changed theme and changed
settings) everywhere -- sidebar, calendar, timer bar, and the Summary tab.
"""
import os
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tmp_home = tempfile.mkdtemp()
os.environ["HOME"] = tmp_home

import tkinter.filedialog as filedialog  # noqa: E402
import tkinter.messagebox as messagebox  # noqa: E402

confirm_answer = {"value": True}
messagebox.askyesno = lambda *a, **k: confirm_answer["value"]
messagebox.showinfo = lambda *a, **k: None
messagebox.showwarning = lambda *a, **k: None

from app import theme  # noqa: E402
from app.db import Database  # noqa: E402
from app.main_window import MainWindow  # noqa: E402
from app.models import Activity, Project, TimeEntry  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


win = MainWindow()
win.update()
db = win.db

client_alpha = db.get_project(db.add_project(Project(None, "Client Alpha", "#4C6EF5")))
deep_work = db.get_activity(db.add_activity(Activity(None, "Deep Work", "PROJ-1", 60, project_id=client_alpha.id)))
db.add_time_entry(TimeEntry(None, deep_work.id, deep_work.name, deep_work.jira_key, deep_work.color,
                             win.calendar.day_date(1).isoformat(), "09:00", "10:00", "pre-backup"))
db.set_setting("jira_display_name", "Alex")
win._on_sidebar_change()  # also refreshes the Timer bar's activity list
win.update()

backup_path = os.path.join(tmp_home, "manual-backup.db")

print("--- Back Up Data writes a snapshot to the chosen path ---")
filedialog.asksaveasfilename = lambda *a, **k: backup_path
win._open_settings_dialog()
win.update()
win._open_backup_dialog()
win.update()
win.backup_panel._backup()
win.update()
check("Backup file was created", os.path.exists(backup_path))

verify_db = Database(backup_path)
try:
    names = {a.name for a in verify_db.list_activities()}
    check("Backup contains the seeded activity", "Deep Work" in names)
    check("Backup preserved the display name setting",
          verify_db.get_setting("jira_display_name") == "Alex")
finally:
    verify_db.close()
win.backup_panel.on_close()
win.update()

print("\n--- Restore is refused while a timer is running ---")
win.timer_bar.activity_var.set("Deep Work")
win.timer_bar._start()
win.update()
check("Timer is running", win.timer_bar.is_running())
win._restore_data(backup_path)
win.update()
check("Restore did nothing while the timer runs (activity still armed/running)",
      win.timer_bar.is_running())
win.timer_bar._stop()
win.update()
check("Timer stopped cleanly", not win.timer_bar.is_running())

print("\n--- Restoring from a backup replaces data and rebuilds the whole window ---")
# Build a second, independent database with different data/settings/theme
# to restore from -- proves restore_from *replaces* rather than merges, and
# that the post-restore rebuild picks up a changed theme too.
other_path = os.path.join(tmp_home, "other-backup.db")
other_db = Database(other_path)
try:
    other_project = other_db.get_project(other_db.add_project(Project(None, "Other Client", "#12B886")))
    other_activity = other_db.get_activity(other_db.add_activity(
        Activity(None, "Client Sync", "PROJ-9", 30, project_id=other_project.id)))
    # A Wednesday of the CURRENT week (not a fixed date), so the Summary
    # tab's default week-view actually includes it below regardless of
    # which day this test happens to run on.
    this_week_wed = (date.today() - timedelta(days=date.today().weekday()) + timedelta(days=2)).isoformat()
    other_db.add_time_entry(TimeEntry(
        None, other_activity.id, other_activity.name, other_activity.jira_key, other_activity.color,
        this_week_wed, "13:00", "13:30", "from the other backup"))
    other_db.set_setting("jira_display_name", "Someone Else")
    other_db.set_setting("theme_mode", "sandstone")
finally:
    other_db.close()

filedialog.askopenfilename = lambda *a, **k: other_path
confirm_answer["value"] = True
win._open_backup_dialog()
win.update()
win.backup_panel._restore()
win.update()
win.update()  # a second pump for the after(0, ...)-deferred rebuild

check("Window rebuilt with a fresh notebook after restore", hasattr(win, "notebook"))
activity_names = {a.name for a in win.db.list_activities()}
check("Old activity is gone after restore", "Deep Work" not in activity_names)
check("Restored activity is present", "Client Sync" in activity_names)
check("Restored display name setting took effect",
      win.db.get_setting("jira_display_name") == "Someone Else")
check("Restored theme was applied", theme.get_theme_id() == "sandstone")

win.notebook.select(win.summary_tab)
win.update()
# The Summary tab is now two side-by-side pie-chart breakdowns (Project,
# QDM/Activity) instead of one togglable list -- "Client Sync" is an
# activity name, so it shows up in the QDM column's legend rows.
row_text = " ".join(
    " ".join(c.cget("text") for c in row.winfo_children() if hasattr(c, "cget") and "text" in c.keys())
    for row in win.summary_panel._qdm["rows_frame"].winfo_children())
check("Summary tab reflects the restored data after switching to it", "Client Sync" in row_text)

print("\n--- Restore declining the confirmation prompt leaves data untouched ---")
confirm_answer["value"] = False
win.notebook.select(win.timesheet_tab)
win.update()
win._open_backup_dialog()
win.update()
win.backup_panel._restore()
win.update()
check("Declining the confirmation kept the current data",
      "Client Sync" in {a.name for a in win.db.list_activities()})
win.backup_panel.on_close()
win.update()

print("\n--- Restoring an unrelated/invalid file shows a warning, doesn't crash ---")
bogus_path = os.path.join(tmp_home, "not_a_backup.txt")
with open(bogus_path, "w") as f:
    f.write("this is not a sqlite database")
confirm_answer["value"] = True
win._restore_data(bogus_path)  # calling the MainWindow method directly, bypassing the file dialog
win.update()
check("Invalid file did not corrupt the current database",
      "Client Sync" in {a.name for a in win.db.list_activities()})

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
