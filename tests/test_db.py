"""Standalone tests for app.db -- no Tkinter required."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config
from app.db import Database
from app.models import Activity, Project, TemplateEntry, TimeEntry


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.dbpath = os.path.join(self.tmpdir, "test.db")
        self.db = Database(self.dbpath)

    def tearDown(self):
        self.db.close()

    def test_seed_defaults(self):
        activities = self.db.list_activities()
        self.assertEqual(len(activities), 4)
        names = {a.name for a in activities}
        self.assertIn("Sprint Planning", names)

        projects = self.db.list_projects()
        project_names = {p.name for p in projects}
        self.assertIn("General", project_names)
        self.assertIn("Client Alpha", project_names)

        # Every seeded activity belongs to a real project and inherits its
        # color -- there's no "ungrouped" state.
        for a in activities:
            self.assertIsNotNone(a.project_id)
            self.assertIsNotNone(a.color)

    def test_list_known_jira_projects_always_includes_the_fixed_default(self):
        # A fresh database has no time blocks yet, but the fixed default
        # is still what every export uses implicitly (see
        # app/config.py's DEFAULT_JIRA_PROJECT) -- so it's always offered
        # even before anything has explicitly used it.
        self.assertEqual(self.db.list_known_jira_projects(), [config.DEFAULT_JIRA_PROJECT])

    def test_list_known_jira_projects_includes_projects_actually_used(self):
        act = self.db.list_activities()[0]
        self.db.add_time_entry(TimeEntry(
            None, act.id, act.name, act.jira_key, act.color,
            "2026-08-24", "09:00", "10:00", "", jira_project="Other Client Project"))
        self.db.add_template_entry(TemplateEntry(
            None, act.id, act.name, act.jira_key, act.color,
            0, "09:00", "10:00", "", jira_project="Third Project"))
        # A blank/whitespace-only value shouldn't pollute the list.
        self.db.add_time_entry(TimeEntry(
            None, act.id, act.name, act.jira_key, act.color,
            "2026-08-25", "09:00", "10:00", "", jira_project="   "))

        projects = self.db.list_known_jira_projects()
        self.assertEqual(projects[0], config.DEFAULT_JIRA_PROJECT)
        self.assertEqual(set(projects),
                          {config.DEFAULT_JIRA_PROJECT, "Other Client Project", "Third Project"})

    def test_add_project_with_default_color_picks_an_unused_color(self):
        # Used by the Add QDM tab's inline "+ New Project..." flow (see
        # ActivityPanel.create_project in app/panels.py), which only asks
        # for a name -- a color is auto-picked so the user isn't stopped
        # to choose one.
        used = {p.color for p in self.db.list_projects()}
        self.assertTrue(used, "seed data should already have used colors")

        project = self.db.add_project_with_default_color("New Client")
        self.assertIsNotNone(project.id)
        self.assertEqual(project.name, "New Client")
        self.assertNotIn(project.color, used)
        self.assertIn(project.color, config.DEFAULT_PROJECT_COLORS)

        stored = self.db.get_project(project.id)
        self.assertEqual(stored.name, "New Client")
        self.assertEqual(stored.color, project.color)

    def test_add_project_with_default_color_falls_back_once_colors_run_out(self):
        # Use up every color in the palette first.
        for i, color in enumerate(config.DEFAULT_PROJECT_COLORS):
            self.db.add_project(Project(None, f"Filler {i}", color))

        project = self.db.add_project_with_default_color("Overflow Client")
        self.assertEqual(project.color, config.DEFAULT_PROJECT_COLORS[0])

    def test_add_update_delete_project(self):
        pid = self.db.add_project(Project(None, "Design Team", "#123456"))
        proj = self.db.get_project(pid)
        self.assertEqual(proj.name, "Design Team")
        self.assertEqual(proj.color, "#123456")

        proj.name = "Design Team v2"
        proj.color = "#654321"
        self.db.update_project(proj)
        updated = self.db.get_project(pid)
        self.assertEqual(updated.name, "Design Team v2")
        self.assertEqual(updated.color, "#654321")

        # Deleting a project with no activities in it is just a plain delete.
        self.db.delete_project(pid)
        self.assertIsNone(self.db.get_project(pid))

    def test_add_update_delete_activity(self):
        pid = self.db.add_project(Project(None, "Client B", "#111111"))
        aid = self.db.add_activity(Activity(None, "Design Review", "PROJ-9", 45, project_id=pid))
        act = self.db.get_activity(aid)
        self.assertEqual(act.name, "Design Review")
        self.assertEqual(act.jira_key, "PROJ-9")
        self.assertEqual(act.color, "#111111")  # inherited from its project

        act.name = "Design Review v2"
        self.db.update_activity(act)
        updated = self.db.get_activity(aid)
        self.assertEqual(updated.name, "Design Review v2")

        act.jira_status = "Closed"
        act.jira_status_category = "done"
        self.db.update_activity(act)
        closed = self.db.get_activity(aid)
        self.assertEqual(closed.jira_status, "Closed")
        self.assertEqual(closed.jira_status_category, "done")
        self.assertTrue(closed.is_closed())

        self.db.delete_activity(aid)
        self.assertIsNone(self.db.get_activity(aid))

    def test_activity_color_follows_its_project_not_itself(self):
        pid = self.db.add_project(Project(None, "Color Test", "#ABCDEF"))
        aid = self.db.add_activity(Activity(None, "Task A", project_id=pid))
        self.assertEqual(self.db.get_activity(aid).color, "#ABCDEF")

        # Recoloring the project immediately recolors every activity in it
        # (read fresh from the DB -- Activity.color is a read-only join).
        proj = self.db.get_project(pid)
        proj.color = "#112233"
        self.db.update_project(proj)
        self.assertEqual(self.db.get_activity(aid).color, "#112233")

    def test_time_entry_crud_and_snapshot(self):
        pid = self.db.add_project(Project(None, "Meetings", "#ABCDEF"))
        aid = self.db.add_activity(Activity(None, "Standup", None, 15, project_id=pid))
        act = self.db.get_activity(aid)
        eid = self.db.add_time_entry(TimeEntry(
            None, aid, act.name, act.jira_key, act.color,
            "2026-08-24", "09:00", "09:15", "daily sync"
        ))
        entry = self.db.get_time_entry(eid)
        self.assertEqual(entry.activity_name, "Standup")
        self.assertEqual(entry.duration_minutes(), 15)

        # Renaming the activity should cascade into existing entries.
        act.name = "Daily Standup"
        act.jira_key = "PROJ-1"
        self.db.update_activity(act)
        entry2 = self.db.get_time_entry(eid)
        self.assertEqual(entry2.activity_name, "Daily Standup")
        self.assertEqual(entry2.jira_key, "PROJ-1")

        self.db.delete_time_entry(eid)
        self.assertIsNone(self.db.get_time_entry(eid))

    def test_delete_activity_keeps_entries_by_default(self):
        pid = self.db.add_project(Project(None, "Temp Project", "#000000"))
        aid = self.db.add_activity(Activity(None, "Temp Activity", project_id=pid))
        eid = self.db.add_time_entry(TimeEntry(
            None, aid, "Temp Activity", None, "#000000",
            "2026-08-25", "10:00", "10:30", ""
        ))
        self.db.delete_activity(aid, delete_entries=False)
        entry = self.db.get_time_entry(eid)
        self.assertIsNotNone(entry)
        self.assertIsNone(entry.activity_id)
        self.assertEqual(entry.activity_name, "Temp Activity")  # snapshot preserved

    def test_delete_activity_cascade(self):
        pid = self.db.add_project(Project(None, "Temp2", "#000000"))
        aid = self.db.add_activity(Activity(None, "Temp2 Activity", project_id=pid))
        eid = self.db.add_time_entry(TimeEntry(
            None, aid, "Temp2 Activity", None, "#000000", "2026-08-25", "10:00", "10:30", ""
        ))
        self.db.delete_activity(aid, delete_entries=True)
        self.assertIsNone(self.db.get_time_entry(eid))

    def test_list_time_entries_for_week(self):
        pid = self.db.add_project(Project(None, "X Project", "#111111"))
        aid = self.db.add_activity(Activity(None, "X", project_id=pid))
        self.db.add_time_entry(TimeEntry(None, aid, "X", None, "#111111", "2026-08-24", "09:00", "10:00", ""))
        self.db.add_time_entry(TimeEntry(None, aid, "X", None, "#111111", "2026-08-25", "11:00", "12:00", ""))
        self.db.add_time_entry(TimeEntry(None, aid, "X", None, "#111111", "2026-09-01", "09:00", "10:00", ""))
        week_dates = ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28"]
        entries = self.db.list_time_entries_for_week(week_dates)
        self.assertEqual(len(entries), 2)

    def test_overlap_detection(self):
        pid = self.db.add_project(Project(None, "X Project", "#111111"))
        aid = self.db.add_activity(Activity(None, "X", project_id=pid))
        self.db.add_time_entry(TimeEntry(None, aid, "X", None, "#111111", "2026-08-24", "09:00", "10:00", ""))
        self.assertTrue(self.db.entries_overlap("2026-08-24", "09:30", "10:30"))
        self.assertTrue(self.db.entries_overlap("2026-08-24", "08:30", "09:30"))
        self.assertFalse(self.db.entries_overlap("2026-08-24", "10:00", "11:00"))  # touching, not overlapping
        self.assertFalse(self.db.entries_overlap("2026-08-24", "08:00", "09:00"))

    def test_settings(self):
        self.assertIsNone(self.db.get_setting("jira_display_name"))
        self.db.set_setting("jira_display_name", "Alex Rae")
        self.assertEqual(self.db.get_setting("jira_display_name"), "Alex Rae")
        self.db.set_setting("jira_display_name", "Alex R.")
        self.assertEqual(self.db.get_setting("jira_display_name"), "Alex R.")

    def test_activity_and_entry_jira_project_issue_type_persist(self):
        pid = self.db.add_project(Project(None, "Photocard", "#123456"))
        aid = self.db.add_activity(Activity(
            None, "Photocard test", "QDM-5455", 60, project_id=pid,
            jira_project="Quasar Delivery Management", issue_type="Sub-task",
        ))
        act = self.db.get_activity(aid)
        self.assertEqual(act.jira_project, "Quasar Delivery Management")
        self.assertEqual(act.issue_type, "Sub-task")

        eid = self.db.add_time_entry(TimeEntry(
            None, aid, act.name, act.jira_key, act.color,
            "2026-07-24", "09:00", "10:00", "",
            jira_project=act.jira_project, issue_type=act.issue_type,
        ))
        entry = self.db.get_time_entry(eid)
        self.assertEqual(entry.jira_project, "Quasar Delivery Management")
        self.assertEqual(entry.issue_type, "Sub-task")
        self.assertIsNone(entry.jira_worklog_id)

        self.db.set_time_entry_worklog_id(eid, "555")
        self.assertEqual(self.db.get_time_entry(eid).jira_worklog_id, "555")

        # Renaming the activity's Jira project/issue type should cascade,
        # same as name/jira_key/color already do.
        act.jira_project = "Renamed Project"
        act.issue_type = "Story"
        self.db.update_activity(act)
        entry2 = self.db.get_time_entry(eid)
        self.assertEqual(entry2.jira_project, "Renamed Project")
        self.assertEqual(entry2.issue_type, "Story")

    def test_template_entry_crud_and_snapshot(self):
        pid = self.db.add_project(Project(None, "Sync Project", "#ABCDEF"))
        aid = self.db.add_activity(Activity(None, "Weekly Sync", "PROJ-5", 30, project_id=pid))
        act = self.db.get_activity(aid)
        tid = self.db.add_template_entry(TemplateEntry(
            None, aid, act.name, act.jira_key, act.color,
            0, "09:00", "09:30", "recurring check-in",
        ))
        t = self.db.get_template_entry(tid)
        self.assertEqual(t.activity_name, "Weekly Sync")
        self.assertEqual(t.day_of_week, 0)
        self.assertEqual(t.duration_minutes(), 30)

        # Renaming the activity should cascade into template entries too.
        act.name = "Weekly Team Sync"
        self.db.update_activity(act)
        t2 = self.db.get_template_entry(tid)
        self.assertEqual(t2.activity_name, "Weekly Team Sync")

        entries = self.db.list_template_entries()
        self.assertEqual(len(entries), 1)

        self.db.delete_template_entry(tid)
        self.assertIsNone(self.db.get_template_entry(tid))

    def test_template_entries_overlap_detection(self):
        pid = self.db.add_project(Project(None, "X Project", "#111111"))
        aid = self.db.add_activity(Activity(None, "X", project_id=pid))
        self.db.add_template_entry(TemplateEntry(None, aid, "X", None, "#111111", 2, "09:00", "10:00", ""))
        self.assertTrue(self.db.template_entries_overlap(2, "09:30", "10:30"))
        self.assertFalse(self.db.template_entries_overlap(2, "10:00", "11:00"))  # touching, not overlapping
        self.assertFalse(self.db.template_entries_overlap(3, "09:30", "10:30"))  # different weekday

    def test_delete_activity_cascades_to_template_entries(self):
        pid = self.db.add_project(Project(None, "Temp3 Project", "#000000"))
        aid = self.db.add_activity(Activity(None, "Temp3", project_id=pid))
        tid = self.db.add_template_entry(TemplateEntry(None, aid, "Temp3", None, "#000000", 1, "10:00", "10:30", ""))
        self.db.delete_activity(aid, delete_entries=True)
        self.assertIsNone(self.db.get_template_entry(tid))

        aid2 = self.db.add_activity(Activity(None, "Temp4", project_id=pid))
        tid2 = self.db.add_template_entry(TemplateEntry(None, aid2, "Temp4", None, "#000000", 1, "10:00", "10:30", ""))
        self.db.delete_activity(aid2, delete_entries=False)
        kept = self.db.get_template_entry(tid2)
        self.assertIsNotNone(kept)
        self.assertIsNone(kept.activity_id)

    def test_apply_template_to_week(self):
        pid = self.db.add_project(Project(None, "Standup Project", "#ABCDEF"))
        aid = self.db.add_activity(Activity(None, "Standup", None, 15, project_id=pid))
        act = self.db.get_activity(aid)
        # Monday recurring standup.
        self.db.add_template_entry(TemplateEntry(
            None, aid, act.name, act.jira_key, act.color, 0, "09:00", "09:15", ""))
        # Wednesday recurring 1:1.
        self.db.add_template_entry(TemplateEntry(
            None, aid, act.name, act.jira_key, act.color, 2, "14:00", "14:30", ""))

        week_dates = ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28"]
        created, skipped = self.db.apply_template_to_week(week_dates)
        self.assertEqual(created, 2)
        self.assertEqual(skipped, [])

        entries = self.db.list_time_entries_for_week(week_dates)
        self.assertEqual(len(entries), 2)
        by_date = {e.date: e for e in entries}
        self.assertEqual(by_date["2026-08-24"].start_time, "09:00")
        self.assertEqual(by_date["2026-08-26"].start_time, "14:00")

        # Applying again should skip both -- the slots are now occupied.
        created2, skipped2 = self.db.apply_template_to_week(week_dates)
        self.assertEqual(created2, 0)
        self.assertEqual(len(skipped2), 2)
        # No duplicate rows were created.
        self.assertEqual(len(self.db.list_time_entries_for_week(week_dates)), 2)

    def test_project_crud_and_collapse_toggle(self):
        pid = self.db.add_project(Project(None, "Client A", "#4C6EF5"))
        proj = self.db.get_project(pid)
        self.assertEqual(proj.name, "Client A")
        self.assertFalse(proj.collapsed)

        proj.name = "Client A (renamed)"
        self.db.update_project(proj)
        renamed = self.db.get_project(pid)
        self.assertEqual(renamed.name, "Client A (renamed)")

        self.db.set_project_collapsed(pid, True)
        collapsed = self.db.get_project(pid)
        self.assertTrue(collapsed.collapsed)
        self.db.set_project_collapsed(pid, False)
        self.assertFalse(self.db.get_project(pid).collapsed)

    def test_activity_project_id_persists(self):
        pid = self.db.add_project(Project(None, "Internal", "#4C6EF5"))
        aid = self.db.add_activity(Activity(None, "Standup", None, 15, project_id=pid))
        act = self.db.get_activity(aid)
        self.assertEqual(act.project_id, pid)

        pid2 = self.db.add_project(Project(None, "External", "#12B886"))
        act.project_id = pid2
        self.db.update_activity(act)
        self.assertEqual(self.db.get_activity(aid).project_id, pid2)
        # Moving to a different project immediately recolors it.
        self.assertEqual(self.db.get_activity(aid).color, "#12B886")

    def test_ensure_activities_have_projects_reassigns_orphans_on_launch(self):
        """Every Activity must belong to a Project -- if one somehow ends up
        without one (project_id NULL, e.g. from an older/bypassed code
        path), the next time the app opens the database it gets moved into
        the catch-all "General" project rather than staying ungrouped."""
        with self.db._cursor() as cur:
            cur.execute(
                "INSERT INTO activities (name, jira_key, default_duration_minutes, "
                "archived, created_at, project_id) VALUES (?, ?, ?, ?, ?, NULL)",
                ("Orphaned", None, None, 0, "2026-01-01T00:00:00"),
            )
            orphan_id = cur.lastrowid

        # Re-running the startup migration chain (as a fresh app launch
        # would) should reassign it.
        self.db._ensure_activities_have_projects()
        act = self.db.get_activity(orphan_id)
        self.assertIsNotNone(act.project_id)
        general = self.db.get_project(act.project_id)
        self.assertEqual(general.name, "General")

    def test_delete_project_moves_activities_to_general_by_default(self):
        pid = self.db.add_project(Project(None, "Temp Project", "#4C6EF5"))
        aid = self.db.add_activity(Activity(None, "Grouped", project_id=pid))
        self.db.delete_project(pid, delete_activities=False)
        self.assertIsNone(self.db.get_project(pid))
        act = self.db.get_activity(aid)
        self.assertIsNotNone(act)
        general = self.db.get_project(act.project_id)
        self.assertEqual(general.name, "General")
        self.assertEqual(act.color, general.color)

    def test_delete_project_can_also_delete_its_activities(self):
        pid = self.db.add_project(Project(None, "Temp Project 2", "#4C6EF5"))
        aid = self.db.add_activity(Activity(None, "Grouped2", project_id=pid))
        eid = self.db.add_time_entry(TimeEntry(
            None, aid, "Grouped2", None, "#4C6EF5", "2026-08-24", "09:00", "09:30", ""))
        self.db.delete_project(pid, delete_activities=True)
        self.assertIsNone(self.db.get_project(pid))
        self.assertIsNone(self.db.get_activity(aid))
        # Deleting the activity this way keeps its existing time blocks
        # (same "keep entries" behavior as a normal activity delete).
        entry = self.db.get_time_entry(eid)
        self.assertIsNotNone(entry)
        self.assertIsNone(entry.activity_id)

    def test_deleting_the_general_project_creates_a_fresh_one(self):
        """Deleting a project literally named "General" (while keeping its
        activities) must not reassign them right back into the row that's
        about to be deleted -- get_or_create_general_project's
        exclude_project_id guards exactly this."""
        general_id, _ = self.db.get_or_create_general_project()
        aid = self.db.add_activity(Activity(None, "In General", project_id=general_id))
        self.db.delete_project(general_id, delete_activities=False)
        self.assertIsNone(self.db.get_project(general_id))
        act = self.db.get_activity(aid)
        self.assertIsNotNone(act.project_id)
        self.assertNotEqual(act.project_id, general_id)
        fresh_general = self.db.get_project(act.project_id)
        self.assertEqual(fresh_general.name, "General")

    def test_schema_migration_adds_columns_to_pre_existing_db(self):
        """A database created before Jira Project/Issue Type/project_id
        existed should be upgraded in place (columns added, no data lost)
        the next time the app opens it, instead of crashing. This
        simulates a database already on today's table names (activities/
        projects) but missing the later-added columns."""
        import sqlite3

        old_path = os.path.join(self.tmpdir, "old.db")
        conn = sqlite3.connect(old_path)
        conn.executescript("""
            CREATE TABLE projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                collapsed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                jira_key TEXT,
                default_duration_minutes INTEGER,
                color TEXT NOT NULL DEFAULT '#4C6EF5',
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE time_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                activity_id INTEGER,
                activity_name TEXT NOT NULL,
                jira_key TEXT,
                color TEXT NOT NULL DEFAULT '#4C6EF5',
                date TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        """)
        conn.execute(
            "INSERT INTO projects (id, name, created_at) VALUES (1, 'Legacy Project', '2026-01-01T00:00:00')"
        )
        conn.execute(
            "INSERT INTO activities (name, jira_key, default_duration_minutes, color, archived, created_at) "
            "VALUES ('Legacy Activity', 'PROJ-1', 30, '#4C6EF5', 0, '2026-01-01T00:00:00')"
        )
        conn.commit()
        conn.close()

        migrated = Database(old_path)
        try:
            activities = migrated.list_activities()
            self.assertEqual(len(activities), 1)
            act = activities[0]
            self.assertEqual(act.name, "Legacy Activity")
            self.assertIsNone(act.jira_project)
            self.assertIsNone(act.issue_type)
            self.assertIsNone(act.jira_status)
            self.assertIsNone(act.jira_status_category)
            # The orphaned activity (no project_id column existed yet) gets
            # swept into "General" by _ensure_activities_have_projects.
            self.assertIsNotNone(act.project_id)

            projects = migrated.list_projects()
            project_names = {p.name for p in projects}
            self.assertIn("Legacy Project", project_names)
            self.assertIn("General", project_names)

            act.jira_project = "New Project"
            migrated.update_activity(act)
            reloaded = migrated.get_activity(act.id)
            self.assertEqual(reloaded.jira_project, "New Project")
        finally:
            migrated.close()

    def test_legacy_step1_naming_migrates_all_the_way_to_current_schema(self):
        """A database from the very first schema (before any rename at
        all -- leaf items called "activities" grouped into
        "activity_folders") should upgrade all the way to today's
        Activity/Project naming (leaf items = activities again, groups =
        projects, color on the project) in one open, via both migrations
        chained back to back."""
        import sqlite3

        old_path = os.path.join(self.tmpdir, "step1.db")
        conn = sqlite3.connect(old_path)
        conn.executescript("""
            CREATE TABLE activity_folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                collapsed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                jira_key TEXT,
                default_duration_minutes INTEGER,
                color TEXT NOT NULL DEFAULT '#4C6EF5',
                archived INTEGER NOT NULL DEFAULT 0,
                project TEXT,
                issue_type TEXT,
                folder_id INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE time_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                activity_id INTEGER,
                activity_name TEXT NOT NULL,
                jira_key TEXT,
                color TEXT NOT NULL DEFAULT '#4C6EF5',
                date TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                project TEXT,
                issue_type TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        """)
        conn.execute(
            "INSERT INTO activity_folders (id, name) VALUES (1, 'Client Work')"
        )
        conn.execute(
            "INSERT INTO activities (id, name, jira_key, default_duration_minutes, color, "
            "archived, project, issue_type, folder_id, created_at) VALUES "
            "(1, 'Legacy Leaf', 'PROJ-1', 30, '#4C6EF5', 0, 'Quasar Delivery Management', "
            "'Sub-task', 1, '2026-01-01T00:00:00')"
        )
        conn.execute(
            "INSERT INTO time_entries (activity_id, activity_name, jira_key, color, date, "
            "start_time, end_time, notes, project, issue_type, created_at, updated_at) VALUES "
            "(1, 'Legacy Leaf', 'PROJ-1', '#4C6EF5', '2026-08-20', '09:00', '10:00', "
            "'notes here', 'Quasar Delivery Management', 'Sub-task', "
            "'2026-08-20T09:00:00', '2026-08-20T09:00:00')"
        )
        conn.commit()
        conn.close()

        migrated = Database(old_path)
        try:
            projects = migrated.list_projects()
            self.assertEqual(len(projects), 1)
            self.assertEqual(projects[0].name, "Client Work")
            # The old per-leaf-item color was backfilled onto the project.
            self.assertEqual(projects[0].color, "#4C6EF5")

            activities = migrated.list_activities()
            self.assertEqual(len(activities), 1)
            act = activities[0]
            self.assertEqual(act.name, "Legacy Leaf")
            self.assertEqual(act.jira_project, "Quasar Delivery Management")
            self.assertEqual(act.issue_type, "Sub-task")
            self.assertEqual(act.project_id, projects[0].id)

            entries = migrated.list_time_entries_for_week(["2026-08-20"])
            self.assertEqual(len(entries), 1)
            entry = entries[0]
            self.assertEqual(entry.activity_id, act.id)
            self.assertEqual(entry.activity_name, "Legacy Leaf")
            self.assertEqual(entry.jira_project, "Quasar Delivery Management")
            self.assertEqual(entry.notes, "notes here")

            # The renamed FK (activity_id -> activities(id)) still nulls out
            # on delete rather than erroring or orphaning silently.
            migrated.delete_activity(act.id, delete_entries=False)
            reloaded_entry = migrated.get_time_entry(entry.id)
            self.assertIsNotNone(reloaded_entry)
            self.assertIsNone(reloaded_entry.activity_id)
        finally:
            migrated.close()

    def test_step2_project_naming_migrates_to_activity_project(self):
        """A database from the intermediate naming (this app's FIRST rename
        -- leaf items already called "projects", grouped into
        "project_folders", no per-project color yet) should upgrade to
        today's naming: leaf items become "activities" again, folders
        become "projects", and color moves onto the project."""
        import sqlite3

        old_path = os.path.join(self.tmpdir, "step2.db")
        conn = sqlite3.connect(old_path)
        conn.executescript("""
            CREATE TABLE project_folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                collapsed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                jira_key TEXT,
                default_duration_minutes INTEGER,
                color TEXT NOT NULL DEFAULT '#4C6EF5',
                archived INTEGER NOT NULL DEFAULT 0,
                jira_project TEXT,
                issue_type TEXT,
                folder_id INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE time_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                project_name TEXT NOT NULL,
                jira_key TEXT,
                color TEXT NOT NULL DEFAULT '#4C6EF5',
                date TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                jira_project TEXT,
                issue_type TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        """)
        conn.execute("INSERT INTO project_folders (id, name) VALUES (1, 'Client Work')")
        conn.execute(
            "INSERT INTO projects (id, name, jira_key, default_duration_minutes, color, "
            "archived, jira_project, issue_type, folder_id, created_at) VALUES "
            "(1, 'Old Leaf', 'PROJ-2', 20, '#F76707', 0, 'Quasar Delivery Management', "
            "'Task', 1, '2026-01-01T00:00:00')"
        )
        conn.execute(
            "INSERT INTO time_entries (project_id, project_name, jira_key, color, date, "
            "start_time, end_time, notes, jira_project, issue_type, created_at, updated_at) VALUES "
            "(1, 'Old Leaf', 'PROJ-2', '#F76707', '2026-08-21', '13:00', '14:00', "
            "'from step 2', 'Quasar Delivery Management', 'Task', "
            "'2026-08-21T13:00:00', '2026-08-21T13:00:00')"
        )
        conn.commit()
        conn.close()

        migrated = Database(old_path)
        try:
            projects = migrated.list_projects()
            self.assertEqual(len(projects), 1)
            self.assertEqual(projects[0].name, "Client Work")
            self.assertEqual(projects[0].color, "#F76707")  # backfilled from the old leaf item

            activities = migrated.list_activities()
            self.assertEqual(len(activities), 1)
            act = activities[0]
            self.assertEqual(act.name, "Old Leaf")
            self.assertEqual(act.project_id, projects[0].id)
            self.assertEqual(act.jira_project, "Quasar Delivery Management")

            entries = migrated.list_time_entries_for_week(["2026-08-21"])
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].activity_name, "Old Leaf")
            self.assertEqual(entries[0].notes, "from step 2")
        finally:
            migrated.close()

    def test_backup_to_writes_a_restorable_snapshot(self):
        pid = self.db.add_project(Project(None, "Deep Work Project", "#4C6EF5"))
        aid = self.db.add_activity(Activity(None, "Deep Work", "PROJ-1", 60, project_id=pid))
        act = self.db.get_activity(aid)
        self.db.add_time_entry(TimeEntry(
            None, act.id, act.name, act.jira_key, act.color,
            "2026-08-24", "09:00", "10:00", "backup me"))
        self.db.set_setting("jira_display_name", "Alex")

        backup_path = os.path.join(self.tmpdir, "backup.db")
        self.db.backup_to(backup_path)
        self.assertTrue(os.path.exists(backup_path))

        # The backup is a fully independent, ordinary Free Timesheet
        # database file -- openable on its own, not just via restore_from.
        opened = Database(backup_path)
        try:
            names = {a.name for a in opened.list_activities()}
            self.assertIn("Deep Work", names)
            entries = opened.list_time_entries_for_week(["2026-08-24"])
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].notes, "backup me")
            self.assertEqual(opened.get_setting("jira_display_name"), "Alex")
        finally:
            opened.close()

    def test_restore_from_replaces_current_data(self):
        # What's in self.db before restoring should NOT survive -- restore
        # replaces everything, it doesn't merge.
        original_pid = self.db.add_project(Project(None, "Old Project", "#123456"))
        original_aid = self.db.add_activity(Activity(None, "Old Activity", "OLD-1", 30, project_id=original_pid))
        self.db.add_time_entry(TimeEntry(
            None, original_aid, "Old Activity", "OLD-1", "#123456",
            "2026-08-20", "09:00", "10:00", "should be gone after restore"))

        # A separate, independent database to restore from.
        backup_path = os.path.join(self.tmpdir, "restore_source.db")
        source = Database(backup_path)
        try:
            source_pid = source.add_project(Project(None, "Restored Project", "#4C6EF5"))
            source_aid = source.add_activity(Activity(None, "Restored Activity", "NEW-1", 45, project_id=source_pid))
            source.add_time_entry(TimeEntry(
                None, source_aid, "Restored Activity", "NEW-1", "#4C6EF5",
                "2026-08-21", "13:00", "14:00", "came from the backup"))
            source.set_setting("jira_display_name", "Restored Name")
        finally:
            source.close()

        self.db.restore_from(backup_path)

        names = {a.name for a in self.db.list_activities()}
        self.assertNotIn("Old Activity", names)
        self.assertIn("Restored Activity", names)

        entries = self.db.list_time_entries_for_week(["2026-08-20", "2026-08-21"])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].notes, "came from the backup")
        self.assertEqual(self.db.get_setting("jira_display_name"), "Restored Name")

    def test_restore_from_migrates_a_legacy_activity_named_backup(self):
        """Restoring from a backup taken by a pre-rename version of the app
        (still using the very first "Activity"/"Activity Folder" table/
        column names) should upgrade it in place the same way opening such
        a file directly does, not leave it half-migrated or fail outright."""
        import sqlite3

        legacy_path = os.path.join(self.tmpdir, "legacy_backup.db")
        conn = sqlite3.connect(legacy_path)
        conn.executescript("""
            CREATE TABLE activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                jira_key TEXT,
                default_duration_minutes INTEGER,
                color TEXT NOT NULL DEFAULT '#4C6EF5',
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE time_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                activity_id INTEGER,
                activity_name TEXT NOT NULL,
                jira_key TEXT,
                color TEXT NOT NULL DEFAULT '#4C6EF5',
                date TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        """)
        conn.execute(
            "INSERT INTO activities (id, name, jira_key, default_duration_minutes, color, "
            "archived, created_at) VALUES (1, 'Legacy Activity', 'PROJ-1', 30, '#4C6EF5', 0, "
            "'2026-01-01T00:00:00')"
        )
        conn.execute(
            "INSERT INTO time_entries (activity_id, activity_name, jira_key, color, date, "
            "start_time, end_time, notes, created_at, updated_at) VALUES "
            "(1, 'Legacy Activity', 'PROJ-1', '#4C6EF5', '2026-08-20', '09:00', '10:00', "
            "'old notes', '2026-08-20T09:00:00', '2026-08-20T09:00:00')"
        )
        conn.commit()
        conn.close()

        self.db.restore_from(legacy_path)

        activities = self.db.list_activities()
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0].name, "Legacy Activity")
        # No project_id column existed in that legacy file at all -- the
        # restored activity should have been swept into "General".
        self.assertIsNotNone(activities[0].project_id)

        entries = self.db.list_time_entries_for_week(["2026-08-20"])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].activity_name, "Legacy Activity")
        self.assertEqual(entries[0].notes, "old notes")

    def test_restore_from_rejects_an_unrelated_sqlite_file(self):
        import sqlite3

        unrelated_path = os.path.join(self.tmpdir, "unrelated.db")
        conn = sqlite3.connect(unrelated_path)
        conn.execute("CREATE TABLE totally_unrelated_thing (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        with self.assertRaises(ValueError):
            self.db.restore_from(unrelated_path)

        # The rejection should happen before anything is touched -- the
        # seeded defaults from setUp() are still there.
        self.assertEqual(len(self.db.list_activities()), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
