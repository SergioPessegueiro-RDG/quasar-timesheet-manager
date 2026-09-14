"""
SQLite data-access layer for the Jira Timesheet app.

Everything is stored in a single local SQLite file (see config.DB_PATH).
No network access, no external services -- fully self-hosted.
"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import List, Optional, Tuple

from . import config
from .models import Activity, PendingWorklogDelete, Project, TemplateEntry, TimeEntry

# NOTE ON NAMING -- this schema has been renamed twice:
#   1. Originally "Activities" (leaf items) grouped into "Activity Folders".
#   2. Renamed so the leaf items became "Projects" (no folders yet).
#   3. Renamed again (this version) back to the original "Activity" for the
#      leaf items, with the *folder* becoming "Project" instead -- and a
#      time block's color now comes from its Activity's Project, not the
#      Activity itself (previously colors were per-Activity/-leaf-item).
# _migrate_legacy_activity_naming() below handles step 1->2 for a database
# from before either rename; _migrate_folder_rename_to_activity_project()
# handles step 2->3. Both run every time the app starts, and both are
# no-ops once a database is already on the current names, so upgrading
# from ANY older version just works without the user doing anything.
SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    color       TEXT NOT NULL DEFAULT '#4C6EF5',
    sort_order  INTEGER NOT NULL DEFAULT 0,
    collapsed   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activities (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    name                    TEXT NOT NULL,
    jira_key                TEXT,
    default_duration_minutes INTEGER,
    archived                INTEGER NOT NULL DEFAULT 0,
    created_at              TEXT NOT NULL,
    jira_project            TEXT,
    issue_type              TEXT,
    project_id              INTEGER,
    jira_status             TEXT,
    jira_status_category    TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS time_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id     INTEGER,
    activity_name   TEXT NOT NULL,
    jira_key        TEXT,
    color           TEXT NOT NULL DEFAULT '#4C6EF5',
    date            TEXT NOT NULL,        -- YYYY-MM-DD
    start_time      TEXT NOT NULL,        -- HH:MM (24h)
    end_time        TEXT NOT NULL,        -- HH:MM (24h)
    notes           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    jira_project    TEXT,
    issue_type      TEXT,
    jira_worklog_id TEXT,
    jira_worklog_issue TEXT,
    jira_worklog_fingerprint TEXT,
    FOREIGN KEY (activity_id) REFERENCES activities(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS pending_worklog_deletes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    jira_key        TEXT NOT NULL,
    jira_worklog_id TEXT NOT NULL UNIQUE,
    date            TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT
);

CREATE TABLE IF NOT EXISTS template_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id     INTEGER,
    activity_name   TEXT NOT NULL,
    jira_key        TEXT,
    color           TEXT NOT NULL DEFAULT '#4C6EF5',
    day_of_week     INTEGER NOT NULL,     -- 0=Monday .. 4=Friday
    start_time      TEXT NOT NULL,        -- HH:MM (24h)
    end_time        TEXT NOT NULL,        -- HH:MM (24h)
    notes           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    jira_project    TEXT,
    issue_type      TEXT,
    FOREIGN KEY (activity_id) REFERENCES activities(id) ON DELETE SET NULL
);
"""

# Columns added after the initial release, keyed by their FINAL (current)
# table/column names. CREATE TABLE IF NOT EXISTS above is a no-op on a
# database that already has these tables, so an existing database is
# migrated by adding any of these columns that are still missing --
# existing rows keep all their data, the new columns just come back blank
# (or their stated default) until filled in. This covers upgrading from a
# version that predates these columns entirely; see
# _migrate_legacy_activity_naming()/_migrate_folder_rename_to_activity_project()
# below for the separate migrations that rename *tables/columns* to today's
# names before this one adds any that are still missing under those names.
_MIGRATIONS = {
    "projects": [
        ("color", "TEXT NOT NULL DEFAULT '#4C6EF5'"),
    ],
    "activities": [
        ("jira_project", "TEXT"),
        ("issue_type", "TEXT"),
        ("project_id", "INTEGER"),
        ("jira_status", "TEXT"),
        ("jira_status_category", "TEXT"),
    ],
    "time_entries": [
        ("jira_project", "TEXT"),
        ("issue_type", "TEXT"),
        ("jira_worklog_id", "TEXT"),
        ("jira_worklog_issue", "TEXT"),
        ("jira_worklog_fingerprint", "TEXT"),
    ],
    "template_entries": [
        ("jira_project", "TEXT"),
        ("issue_type", "TEXT"),
    ],
}

# Table renames from the original pre-"Projects" naming (step 1 -> step 2
# above), applied only if the old table is present and the new one isn't
# (i.e. exactly once, the first time a database from that era is opened by
# a version of the app from after the FIRST rename).
_LEGACY_TABLE_RENAMES = [
    ("activities", "projects"),
    ("activity_folders", "project_folders"),
]

# Column renames within tables that already have their step-2 (intermediate)
# table name by the time this runs -- i.e. either they were just renamed
# above, or they were created fresh under that name and never had the old
# columns to begin with (in which case these are all no-ops).
_LEGACY_COLUMN_RENAMES = {
    "projects": [("project", "jira_project")],
    "time_entries": [
        ("activity_id", "project_id"),
        ("activity_name", "project_name"),
        ("project", "jira_project"),
    ],
    "template_entries": [
        ("activity_id", "project_id"),
        ("activity_name", "project_name"),
        ("project", "jira_project"),
    ],
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Database:
    """Thin wrapper around a single sqlite3 connection."""

    def __init__(self, path: str = config.DB_PATH):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _init_schema(self):
        # Order matters: rename any pre-existing tables/columns to today's
        # names *before* creating the (new-named) schema, so an upgrade
        # renames the user's existing data in place instead of the CREATE
        # TABLE IF NOT EXISTS below silently leaving it behind in an
        # orphaned old table while creating an empty new one next to it.
        self._migrate_legacy_activity_naming()
        self._migrate_folder_rename_to_activity_project()
        with self._cursor() as cur:
            cur.executescript(SCHEMA)
        self._migrate_schema()
        self._backfill_project_colors_from_legacy_activities()
        self._ensure_activities_have_projects()
        self._seed_defaults_if_empty()

    def _migrate_legacy_activity_naming(self):
        """Step 1 -> step 2: the original "Activities"/"Activity Folders"
        naming to the intermediate "Projects"/"Project Folders" naming."""
        with self._cursor() as cur:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            existing_tables = {row["name"] for row in cur.fetchall()}

            for old_name, new_name in _LEGACY_TABLE_RENAMES:
                if old_name in existing_tables and new_name not in existing_tables:
                    # SQLite (3.25.0+) automatically rewrites any foreign-key
                    # clauses in other tables that reference old_name, so
                    # time_entries/template_entries' FK definitions keep
                    # pointing at the right (renamed) table without needing
                    # anything else here.
                    cur.execute(f"ALTER TABLE {old_name} RENAME TO {new_name}")
                    existing_tables.discard(old_name)
                    existing_tables.add(new_name)

            for table, renames in _LEGACY_COLUMN_RENAMES.items():
                if table not in existing_tables:
                    continue
                cur.execute(f"PRAGMA table_info({table})")
                columns = {row["name"] for row in cur.fetchall()}
                for old_col, new_col in renames:
                    if old_col in columns and new_col not in columns:
                        # RENAME COLUMN likewise updates any FK/trigger/view
                        # definitions within the same table that reference
                        # the old column name.
                        cur.execute(f"ALTER TABLE {table} RENAME COLUMN {old_col} TO {new_col}")
                        columns.discard(old_col)
                        columns.add(new_col)

    def _migrate_folder_rename_to_activity_project(self):
        """Step 2 -> step 3: the intermediate "Projects"/"Project Folders"
        naming (leaf items called Projects, no per-item color yet moved to
        the folder) to today's "Activities"/"Projects" naming -- the leaf
        items go back to being called Activities, and what used to be
        Project Folders becomes Projects.

        Identified by "a `projects` table exists but `activities` doesn't"
        rather than by requiring `project_folders` to also be present --
        once a fresh/current database always creates both `activities` and
        `projects` together (see SCHEMA), the only way `projects` can exist
        without `activities` is if `projects` is still holding step-2 leaf
        data waiting to become `activities`. This also covers a database
        from before the folder feature even existed (no `project_folders`
        table at all, e.g. from before app/sidebar.py grew folders) -- its
        leaf table still gets renamed to `activities`; there's just no
        folder table left to become the new `projects`, so an empty one is
        created fresh by SCHEMA below instead."""
        with self._cursor() as cur:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            existing_tables = {row["name"] for row in cur.fetchall()}

            if "projects" in existing_tables and "activities" not in existing_tables:
                cur.execute("ALTER TABLE projects RENAME TO activities")
                existing_tables.discard("projects")
                existing_tables.add("activities")
                if "project_folders" in existing_tables:
                    cur.execute("ALTER TABLE project_folders RENAME TO projects")
                    existing_tables.discard("project_folders")
                    existing_tables.add("projects")

            if "activities" in existing_tables:
                cur.execute("PRAGMA table_info(activities)")
                columns = {row["name"] for row in cur.fetchall()}
                if "folder_id" in columns and "project_id" not in columns:
                    cur.execute("ALTER TABLE activities RENAME COLUMN folder_id TO project_id")

            for table in ("time_entries", "template_entries"):
                if table not in existing_tables:
                    continue
                cur.execute(f"PRAGMA table_info({table})")
                columns = {row["name"] for row in cur.fetchall()}
                if "project_id" in columns and "activity_id" not in columns:
                    cur.execute(f"ALTER TABLE {table} RENAME COLUMN project_id TO activity_id")
                if "project_name" in columns and "activity_name" not in columns:
                    cur.execute(f"ALTER TABLE {table} RENAME COLUMN project_name TO activity_name")

    def _migrate_schema(self):
        with self._cursor() as cur:
            for table, columns in _MIGRATIONS.items():
                cur.execute(f"PRAGMA table_info({table})")
                existing = {row["name"] for row in cur.fetchall()}
                for col_name, col_type in columns:
                    if col_name not in existing:
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")

    def _backfill_project_colors_from_legacy_activities(self):
        """One-time data migration (guarded by a settings flag, since
        `projects.color` always has SOME value after _migrate_schema adds
        it -- the flag is the only way to tell "just added, needs a real
        value" apart from "user genuinely picked this exact color"):
        upgrading from the step-2 naming means every existing Project just
        got the schema's flat default color, even though its Activities
        (nee "Projects") used to each have their own real one. Give each
        Project the color of the first Activity that was in it, so the
        upgrade doesn't flatten everything to one color, and recolor that
        Project's existing time blocks to match."""
        if self.get_setting("migrated_project_colors_from_legacy_activities") == "1":
            return
        with self._cursor() as cur:
            cur.execute("PRAGMA table_info(activities)")
            activity_columns = {row["name"] for row in cur.fetchall()}
            if "color" in activity_columns:
                cur.execute(
                    "SELECT id, project_id, color FROM activities "
                    "WHERE project_id IS NOT NULL ORDER BY id"
                )
                first_color_by_project = {}
                for row in cur.fetchall():
                    first_color_by_project.setdefault(row["project_id"], row["color"])
                for project_id, color in first_color_by_project.items():
                    cur.execute("UPDATE projects SET color=? WHERE id=?", (color, project_id))
                    cur.execute(
                        "UPDATE time_entries SET color=? "
                        "WHERE activity_id IN (SELECT id FROM activities WHERE project_id=?)",
                        (color, project_id),
                    )
                    cur.execute(
                        "UPDATE template_entries SET color=? "
                        "WHERE activity_id IN (SELECT id FROM activities WHERE project_id=?)",
                        (color, project_id),
                    )
        self.set_setting("migrated_project_colors_from_legacy_activities", "1")

    def _ensure_activities_have_projects(self):
        """Every Activity must belong to a Project (so it always has a real
        color) -- this is a general safety net, not just a one-time
        migration step, so it's cheap and safe to run on every launch: it's
        a no-op the instant there are no orphaned activities left, which is
        true for a normal fresh install immediately and true for an
        upgraded one after its first run."""
        with self._cursor() as cur:
            cur.execute("SELECT id FROM activities WHERE project_id IS NULL")
            orphan_ids = [row["id"] for row in cur.fetchall()]
        if not orphan_ids:
            return
        general_id, general_color = self.get_or_create_general_project()
        placeholders = ",".join("?" for _ in orphan_ids)
        with self._cursor() as cur:
            cur.execute(
                f"UPDATE activities SET project_id=? WHERE id IN ({placeholders})",
                [general_id, *orphan_ids],
            )
            cur.execute(
                f"UPDATE time_entries SET color=? WHERE activity_id IN ({placeholders})",
                [general_color, *orphan_ids],
            )
            cur.execute(
                f"UPDATE template_entries SET color=? WHERE activity_id IN ({placeholders})",
                [general_color, *orphan_ids],
            )

    def get_or_create_general_project(self, exclude_project_id: Optional[int] = None) -> Tuple[int, str]:
        """Finds (or creates) the catch-all "General" project that
        orphaned/unassigned Activities land in. `exclude_project_id` matters
        when this is called from delete_project() while deleting a project
        literally named "General" itself -- without it, activities being
        freed from that very row would get reassigned right back into the
        project that's about to be deleted."""
        with self._cursor() as cur:
            if exclude_project_id is not None:
                cur.execute("SELECT id, color FROM projects WHERE name='General' AND id<>?",
                            (exclude_project_id,))
            else:
                cur.execute("SELECT id, color FROM projects WHERE name='General'")
            row = cur.fetchone()
        if row:
            return row["id"], row["color"]
        # The last palette entry is a muted neutral gray -- fitting for a
        # catch-all bucket nobody explicitly created.
        color = config.DEFAULT_PROJECT_COLORS[-1]
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO projects (name, color, sort_order, collapsed, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("General", color, 0, 0, _now()),
            )
            lastrowid = cur.lastrowid
            assert lastrowid is not None
        return lastrowid, color

    @contextmanager
    def _cursor(self):
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    def close(self):
        self._conn.close()

    # ------------------------------------------------------------------
    # Backup / restore (see panels.SettingsPanel and MainWindow._backup_data/
    # _restore_data for the UI side)
    # ------------------------------------------------------------------
    # Tables from any era of this schema -- if a chosen file has none of
    # these, it isn't a Free Timesheet database at all (or is one for some
    # entirely different app that happens to also be SQLite), so
    # restore_from() refuses it up front rather than silently wiping the
    # real data with garbage.
    _RECOGNIZED_BACKUP_TABLES = {
        "projects", "activities", "project_folders", "time_entries",
        "template_entries", "settings",
    }

    def backup_to(self, dest_path: str):
        """Writes a complete, consistent snapshot of the whole database to
        dest_path. Uses SQLite's own online backup API (Connection.backup())
        rather than a plain file copy, so it stays correct regardless of
        journal mode and even while the app is mid-session with this
        connection open."""
        dest_conn = sqlite3.connect(dest_path)
        try:
            self._conn.backup(dest_conn)
        finally:
            dest_conn.close()

    def restore_from(self, src_path: str):
        """Replaces every table's contents with what's in src_path (a file
        produced by backup_to, from this or an earlier version of the app),
        using the same online backup API in reverse. Re-runs the normal
        startup migrations afterward, so a backup made by an older version
        (any prior naming scheme, or missing a column added since) is
        upgraded in place exactly like opening an old database file directly
        would be. Raises ValueError if src_path doesn't look like a Free
        Timesheet database at all."""
        src_conn = sqlite3.connect(src_path)
        try:
            src_conn.row_factory = sqlite3.Row
            cur = src_conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            src_tables = {row["name"] for row in cur.fetchall()}
            if not (src_tables & self._RECOGNIZED_BACKUP_TABLES):
                raise ValueError("This doesn't look like a Free Timesheet backup file.")
            src_conn.backup(self._conn)
        finally:
            src_conn.close()
        self._init_schema()

    # ------------------------------------------------------------------
    # Seed data (first run only)
    # ------------------------------------------------------------------
    def _seed_defaults_if_empty(self):
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM activities")
            count = cur.fetchone()["c"]
        if count > 0:
            return
        general_id = self.add_project(Project(None, "General", config.DEFAULT_PROJECT_COLORS[0]))
        client_id = self.add_project(Project(None, "Client Alpha", config.DEFAULT_PROJECT_COLORS[3]))
        defaults = [
            ("Sprint Planning", None, 60, general_id),
            ("Team Standup", None, 15, general_id),
            ("Code Review", None, 30, general_id),
            ("Development", "PROJ-100", 120, client_id),
        ]
        for name, jira_key, dur, project_id in defaults:
            self.add_activity(Activity(None, name, jira_key, dur, project_id=project_id))

    # ------------------------------------------------------------------
    # Projects (collapsible groups in the sidebar; every Activity belongs
    # to exactly one, and its color is what time blocks actually show)
    # ------------------------------------------------------------------
    def add_project(self, p: Project) -> int:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO projects (name, color, sort_order, collapsed, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (p.name, p.color, p.sort_order, int(p.collapsed), _now()),
            )
            lastrowid = cur.lastrowid
            assert lastrowid is not None
            return lastrowid

    def add_project_with_default_color(self, name: str) -> Project:
        """Create a Project without asking for a color -- used by the
        inline "+ New Project..." flow on the Add QDM tab (see
        ActivityPanel.create_project in app/panels.py), which only asks
        for a name. Picks the first color in config.DEFAULT_PROJECT_COLORS
        not already used by an existing Project, falling back to the first
        color if they're all taken."""
        used_colors = {p.color for p in self.list_projects()}
        color = next((c for c in config.DEFAULT_PROJECT_COLORS if c not in used_colors),
                     config.DEFAULT_PROJECT_COLORS[0])
        project = Project(None, name, color)
        project.id = self.add_project(project)
        return project

    def update_project(self, p: Project):
        if p.id is None:
            raise ValueError("Project.id is required for update")
        with self._cursor() as cur:
            cur.execute(
                "UPDATE projects SET name=?, color=?, sort_order=?, collapsed=? WHERE id=?",
                (p.name, p.color, p.sort_order, int(p.collapsed), p.id),
            )
            # Every Activity in this Project shows the Project's color, so
            # recoloring it here needs to recolor every existing time block
            # logged against any of those Activities too, immediately.
            cur.execute(
                "UPDATE time_entries SET color=? "
                "WHERE activity_id IN (SELECT id FROM activities WHERE project_id=?)",
                (p.color, p.id),
            )
            cur.execute(
                "UPDATE template_entries SET color=? "
                "WHERE activity_id IN (SELECT id FROM activities WHERE project_id=?)",
                (p.color, p.id),
            )

    def set_project_collapsed(self, project_id: int, collapsed: bool):
        """Lightweight update for just the collapse/expand toggle, so
        clicking a project header doesn't need to round-trip the whole
        Project object."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE projects SET collapsed=? WHERE id=?",
                (int(collapsed), project_id),
            )

    def collapse_all_projects(self):
        """Fold every project header closed. Used after a large Jira sync
        (and when the sidebar would otherwise try to draw hundreds of
        activity rows at once) so Tk isn't asked to build a widget per QDM."""
        with self._cursor() as cur:
            cur.execute("UPDATE projects SET collapsed=1")

    def delete_project(self, project_id: int, delete_activities: bool = False):
        """Delete a Project. By default its Activities are kept and moved
        into the catch-all "General" project (every Activity must belong to
        one -- there's no "ungrouped" state anymore), recoloring their
        existing time blocks to match; pass delete_activities=True to
        instead delete every Activity that was inside it (which in turn
        keeps their existing time blocks, same as a normal Activity
        delete-but-keep-entries)."""
        with self._cursor() as cur:
            cur.execute("SELECT id FROM activities WHERE project_id=?", (project_id,))
            activity_ids = [row["id"] for row in cur.fetchall()]

        if delete_activities:
            for activity_id in activity_ids:
                self.delete_activity(activity_id, delete_entries=False)
        elif activity_ids:
            general_id, general_color = self.get_or_create_general_project(
                exclude_project_id=project_id)
            placeholders = ",".join("?" for _ in activity_ids)
            with self._cursor() as cur:
                cur.execute(
                    f"UPDATE activities SET project_id=? WHERE id IN ({placeholders})",
                    [general_id, *activity_ids],
                )
                cur.execute(
                    f"UPDATE time_entries SET color=? WHERE activity_id IN ({placeholders})",
                    [general_color, *activity_ids],
                )
                cur.execute(
                    f"UPDATE template_entries SET color=? WHERE activity_id IN ({placeholders})",
                    [general_color, *activity_ids],
                )

        with self._cursor() as cur:
            cur.execute("DELETE FROM projects WHERE id=?", (project_id,))

    def list_projects(self) -> List[Project]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM projects ORDER BY sort_order, name COLLATE NOCASE")
            rows = cur.fetchall()
        return [self._row_to_project(r) for r in rows]

    def get_project_by_name(self, name: str) -> Optional[Project]:
        needle = (name or "").strip().lower()
        if not needle:
            return None
        for project in self.list_projects():
            if project.name.strip().lower() == needle:
                return project
        return None

    def get_activity_by_jira_key(self, jira_key: str) -> Optional[Activity]:
        needle = (jira_key or "").strip().upper()
        if not needle:
            return None
        for activity in self.list_activities(include_archived=True):
            if (activity.jira_key or "").strip().upper() == needle:
                return activity
        return None

    def set_time_entry_worklog_id(self, entry_id: int, worklog_id: Optional[str]):
        """Keep the Jira worklog id on a row without treating it as synced.

        Tests (and older callers) use this to attach an id. The next push
        still sends that worklog unless mark_time_entry_worklog_synced has
        stored a matching fingerprint.
        """
        with self._cursor() as cur:
            cur.execute(
                "UPDATE time_entries SET jira_worklog_id=?, updated_at=? WHERE id=?",
                (worklog_id, _now(), entry_id),
            )

    def mark_time_entry_worklog_synced(
        self, entry_id: int, worklog_id: Optional[str],
        issue_key: Optional[str] = None,
        fingerprint: Optional[str] = None,
    ):
        """Record what was last successfully written to Jira for this block."""
        with self._cursor() as cur:
            cur.execute(
                """UPDATE time_entries
                   SET jira_worklog_id=?, jira_worklog_issue=?,
                       jira_worklog_fingerprint=?
                   WHERE id=?""",
                (worklog_id, issue_key, fingerprint, entry_id),
            )

    def get_project(self, project_id: int) -> Optional[Project]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM projects WHERE id=?", (project_id,))
            r = cur.fetchone()
        return self._row_to_project(r) if r else None

    @staticmethod
    def _row_to_project(r) -> Project:
        return Project(
            id=r["id"], name=r["name"], color=r["color"], sort_order=r["sort_order"],
            collapsed=bool(r["collapsed"]),
        )

    # ------------------------------------------------------------------
    # Activities
    # ------------------------------------------------------------------
    # Every column needed is listed explicitly (rather than activities.* +
    # a joined column) so the joined-in `color` alias can't collide with
    # anything -- a database upgraded from the step-2 naming still carries
    # a vestigial, unused `color` column on activities itself (see
    # _backfill_project_colors_from_legacy_activities) that this
    # deliberately leaves out.
    _ACTIVITY_SELECT = """
        SELECT
            activities.id AS id,
            activities.name AS name,
            activities.jira_key AS jira_key,
            activities.default_duration_minutes AS default_duration_minutes,
            activities.archived AS archived,
            activities.jira_project AS jira_project,
            activities.issue_type AS issue_type,
            activities.project_id AS project_id,
            activities.jira_status AS jira_status,
            activities.jira_status_category AS jira_status_category,
            COALESCE(projects.color, ?) AS color
        FROM activities
        LEFT JOIN projects ON activities.project_id = projects.id
    """
    # LEFT JOIN (not INNER) + COALESCE fallback rather than requiring the
    # join to match: an Activity should never silently disappear from every
    # list in the app just because something left its project_id dangling
    # (a data anomaly _ensure_activities_have_projects() otherwise prevents,
    # but this is cheap insurance against ever hiding a real Activity).
    _FALLBACK_COLOR = config.DEFAULT_PROJECT_COLORS[-1]

    def add_activity(self, a: Activity) -> int:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO activities
                   (name, jira_key, default_duration_minutes, archived, created_at,
                    jira_project, issue_type, project_id, jira_status, jira_status_category)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (a.name, a.jira_key, a.default_duration_minutes,
                 int(a.archived), _now(), a.jira_project, a.issue_type, a.project_id,
                 a.jira_status, a.jira_status_category),
            )
            lastrowid = cur.lastrowid
            assert lastrowid is not None
            return lastrowid

    def update_activity(self, a: Activity):
        if a.id is None:
            raise ValueError("Activity.id is required for update")
        with self._cursor() as cur:
            cur.execute("SELECT color FROM projects WHERE id=?", (a.project_id,))
            row = cur.fetchone()
            color = row["color"] if row else self._FALLBACK_COLOR
            cur.execute(
                """UPDATE activities
                   SET name=?, jira_key=?, default_duration_minutes=?, archived=?,
                       jira_project=?, issue_type=?, project_id=?,
                       jira_status=?, jira_status_category=?
                   WHERE id=?""",
                (a.name, a.jira_key, a.default_duration_minutes,
                 int(a.archived), a.jira_project, a.issue_type, a.project_id,
                 a.jira_status, a.jira_status_category, a.id),
            )
            # Keep existing time entries' snapshot in sync so the calendar
            # reflects a renamed activity, a moved-to-a-different-Project
            # activity (color follows the new Project), or changed Jira
            # fields immediately.
            cur.execute(
                """UPDATE time_entries
                   SET activity_name=?, jira_key=?, color=?, jira_project=?, issue_type=?
                   WHERE activity_id=?""",
                (a.name, a.jira_key, color, a.jira_project, a.issue_type, a.id),
            )
            cur.execute(
                """UPDATE template_entries
                   SET activity_name=?, jira_key=?, color=?, jira_project=?, issue_type=?
                   WHERE activity_id=?""",
                (a.name, a.jira_key, color, a.jira_project, a.issue_type, a.id),
            )

    def delete_activity(self, activity_id: int, delete_entries: bool = False):
        if delete_entries:
            with self._cursor() as cur:
                cur.execute(
                    "SELECT * FROM time_entries WHERE activity_id=?", (activity_id,))
                rows = cur.fetchall()
            for r in rows:
                self.queue_pending_worklog_delete(self._row_to_entry(r))
        with self._cursor() as cur:
            if delete_entries:
                cur.execute("DELETE FROM time_entries WHERE activity_id=?", (activity_id,))
                cur.execute("DELETE FROM template_entries WHERE activity_id=?", (activity_id,))
            else:
                cur.execute(
                    "UPDATE time_entries SET activity_id=NULL WHERE activity_id=?",
                    (activity_id,),
                )
                cur.execute(
                    "UPDATE template_entries SET activity_id=NULL WHERE activity_id=?",
                    (activity_id,),
                )
            cur.execute("DELETE FROM activities WHERE id=?", (activity_id,))

    def list_activities(self, include_archived: bool = False) -> List[Activity]:
        query = self._ACTIVITY_SELECT
        if not include_archived:
            query += " WHERE activities.archived = 0"
        query += " ORDER BY activities.name COLLATE NOCASE"
        with self._cursor() as cur:
            cur.execute(query, (self._FALLBACK_COLOR,))
            rows = cur.fetchall()
        return [self._row_to_activity(r) for r in rows]

    def get_activity(self, activity_id: int) -> Optional[Activity]:
        with self._cursor() as cur:
            cur.execute(self._ACTIVITY_SELECT + " WHERE activities.id=?",
                        (self._FALLBACK_COLOR, activity_id))
            r = cur.fetchone()
        if not r:
            return None
        return self._row_to_activity(r)

    @staticmethod
    def _row_to_activity(r) -> Activity:
        keys = r.keys()
        return Activity(
            id=r["id"], name=r["name"], jira_key=r["jira_key"],
            default_duration_minutes=r["default_duration_minutes"],
            archived=bool(r["archived"]),
            jira_project=r["jira_project"], issue_type=r["issue_type"],
            project_id=r["project_id"], color=r["color"],
            jira_status=r["jira_status"] if "jira_status" in keys else None,
            jira_status_category=(
                r["jira_status_category"] if "jira_status_category" in keys else None),
        )

    # ------------------------------------------------------------------
    # Time entries
    # ------------------------------------------------------------------
    def add_time_entry(self, e: TimeEntry) -> int:
        now = _now()
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO time_entries
                   (activity_id, activity_name, jira_key, color, date,
                    start_time, end_time, notes, created_at, updated_at,
                    jira_project, issue_type, jira_worklog_id,
                    jira_worklog_issue, jira_worklog_fingerprint)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (e.activity_id, e.activity_name, e.jira_key, e.color, e.date,
                 e.start_time, e.end_time, e.notes, now, now, e.jira_project, e.issue_type,
                 e.jira_worklog_id, e.jira_worklog_issue, e.jira_worklog_fingerprint),
            )
            lastrowid = cur.lastrowid
            assert lastrowid is not None
        if e.jira_worklog_id:
            self.cancel_pending_worklog_delete(e.jira_worklog_id)
        return lastrowid

    def update_time_entry(self, e: TimeEntry):
        if e.id is None:
            raise ValueError("TimeEntry.id is required for update")
        with self._cursor() as cur:
            cur.execute(
                """UPDATE time_entries
                   SET activity_id=?, activity_name=?, jira_key=?, color=?, date=?,
                       start_time=?, end_time=?, notes=?, updated_at=?, jira_project=?,
                       issue_type=?, jira_worklog_id=?
                   WHERE id=?""",
                (e.activity_id, e.activity_name, e.jira_key, e.color, e.date,
                 e.start_time, e.end_time, e.notes, _now(), e.jira_project, e.issue_type,
                 e.jira_worklog_id, e.id),
            )

    def delete_time_entry(self, entry_id: int):
        entry = self.get_time_entry(entry_id)
        if entry is not None:
            self.queue_pending_worklog_delete(entry)
        with self._cursor() as cur:
            cur.execute("DELETE FROM time_entries WHERE id=?", (entry_id,))

    def queue_pending_worklog_delete(self, entry: TimeEntry):
        """Remember a pushed worklog so the next Push to Jira can DELETE it."""
        worklog_id = (entry.jira_worklog_id or "").strip()
        key = ((entry.jira_worklog_issue or entry.jira_key or "")).strip()
        if not worklog_id or not key:
            return
        with self._cursor() as cur:
            cur.execute(
                """INSERT OR IGNORE INTO pending_worklog_deletes
                   (jira_key, jira_worklog_id, date, created_at)
                   VALUES (?, ?, ?, ?)""",
                (key, worklog_id, entry.date, _now()),
            )

    def cancel_pending_worklog_delete(self, worklog_id: str):
        needle = (worklog_id or "").strip()
        if not needle:
            return
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM pending_worklog_deletes WHERE jira_worklog_id=?",
                (needle,),
            )

    def remove_pending_worklog_delete(self, pending_id: int):
        with self._cursor() as cur:
            cur.execute("DELETE FROM pending_worklog_deletes WHERE id=?", (pending_id,))

    def list_pending_worklog_deletes(
        self, start_date: Optional[str] = None, end_date: Optional[str] = None,
    ) -> List[PendingWorklogDelete]:
        with self._cursor() as cur:
            if start_date and end_date:
                cur.execute(
                    "SELECT * FROM pending_worklog_deletes "
                    "WHERE date BETWEEN ? AND ? ORDER BY date, id",
                    (start_date, end_date),
                )
            else:
                cur.execute(
                    "SELECT * FROM pending_worklog_deletes ORDER BY date, id")
            rows = cur.fetchall()
        return [self._row_to_pending_worklog_delete(r) for r in rows]

    @staticmethod
    def _row_to_pending_worklog_delete(r) -> PendingWorklogDelete:
        return PendingWorklogDelete(
            id=r["id"], jira_key=r["jira_key"],
            jira_worklog_id=r["jira_worklog_id"], date=r["date"],
        )

    def get_time_entry(self, entry_id: int) -> Optional[TimeEntry]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM time_entries WHERE id=?", (entry_id,))
            r = cur.fetchone()
        return self._row_to_entry(r) if r else None

    def list_time_entries_for_week(self, dates: List[str]) -> List[TimeEntry]:
        """dates: list of 'YYYY-MM-DD' strings (Mon..Fri) to include."""
        if not dates:
            return []
        placeholders = ",".join("?" for _ in dates)
        with self._cursor() as cur:
            cur.execute(
                f"SELECT * FROM time_entries WHERE date IN ({placeholders}) "
                f"ORDER BY date, start_time",
                dates,
            )
            rows = cur.fetchall()
        return [self._row_to_entry(r) for r in rows]

    def list_time_entries_between(self, start_date: str, end_date: str) -> List[TimeEntry]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM time_entries WHERE date BETWEEN ? AND ? "
                "ORDER BY date, start_time",
                (start_date, end_date),
            )
            rows = cur.fetchall()
        return [self._row_to_entry(r) for r in rows]

    def list_known_jira_projects(self) -> List[str]:
        """Distinct Jira Project values actually used somewhere in this
        database (time blocks, template blocks, and QDMs), for offering
        as a controlled pick-list on the Time Block tab instead of free
        text that's easy to typo. Always includes config.DEFAULT_JIRA_PROJECT
        first, even if nothing has explicitly used it yet, since that's
        what a block uses implicitly whenever it doesn't set its own
        (see app/export_csv.py) -- so it's always the sensible default
        to show pre-selected."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT DISTINCT jira_project FROM time_entries "
                "WHERE jira_project IS NOT NULL AND TRIM(jira_project) != '' "
                "UNION "
                "SELECT DISTINCT jira_project FROM template_entries "
                "WHERE jira_project IS NOT NULL AND TRIM(jira_project) != '' "
                "UNION "
                "SELECT DISTINCT jira_project FROM activities "
                "WHERE jira_project IS NOT NULL AND TRIM(jira_project) != ''"
            )
            used = sorted({str(r[0]).strip() for r in cur.fetchall()})
        projects = [config.DEFAULT_JIRA_PROJECT]
        projects.extend(p for p in used if p != config.DEFAULT_JIRA_PROJECT)
        return projects

    def entries_overlap(self, date: str, start_time: str, end_time: str,
                         exclude_id: Optional[int] = None) -> bool:
        """True if [start_time, end_time) on `date` overlaps an existing entry."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT id, start_time, end_time FROM time_entries WHERE date=?",
                (date,),
            )
            rows = cur.fetchall()
        for r in rows:
            if exclude_id is not None and r["id"] == exclude_id:
                continue
            if start_time < r["end_time"] and r["start_time"] < end_time:
                return True
        return False

    @staticmethod
    def _row_to_entry(r) -> TimeEntry:
        return TimeEntry(
            id=r["id"], activity_id=r["activity_id"], activity_name=r["activity_name"],
            jira_key=r["jira_key"], color=r["color"], date=r["date"],
            start_time=r["start_time"], end_time=r["end_time"], notes=r["notes"],
            jira_project=r["jira_project"], issue_type=r["issue_type"],
            jira_worklog_id=r["jira_worklog_id"] if "jira_worklog_id" in r.keys() else None,
            jira_worklog_issue=(
                r["jira_worklog_issue"] if "jira_worklog_issue" in r.keys() else None),
            jira_worklog_fingerprint=(
                r["jira_worklog_fingerprint"] if "jira_worklog_fingerprint" in r.keys()
                else None),
        )

    # ------------------------------------------------------------------
    # Template entries (the permanent "Template" tab's recurring blocks)
    # ------------------------------------------------------------------
    def add_template_entry(self, e: TemplateEntry) -> int:
        now = _now()
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO template_entries
                   (activity_id, activity_name, jira_key, color, day_of_week,
                    start_time, end_time, notes, created_at, updated_at,
                    jira_project, issue_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (e.activity_id, e.activity_name, e.jira_key, e.color, e.day_of_week,
                 e.start_time, e.end_time, e.notes, now, now, e.jira_project, e.issue_type),
            )
            lastrowid = cur.lastrowid
            assert lastrowid is not None
            return lastrowid

    def update_template_entry(self, e: TemplateEntry):
        if e.id is None:
            raise ValueError("TemplateEntry.id is required for update")
        with self._cursor() as cur:
            cur.execute(
                """UPDATE template_entries
                   SET activity_id=?, activity_name=?, jira_key=?, color=?, day_of_week=?,
                       start_time=?, end_time=?, notes=?, updated_at=?, jira_project=?, issue_type=?
                   WHERE id=?""",
                (e.activity_id, e.activity_name, e.jira_key, e.color, e.day_of_week,
                 e.start_time, e.end_time, e.notes, _now(), e.jira_project, e.issue_type, e.id),
            )

    def delete_template_entry(self, entry_id: int):
        with self._cursor() as cur:
            cur.execute("DELETE FROM template_entries WHERE id=?", (entry_id,))

    def get_template_entry(self, entry_id: int) -> Optional[TemplateEntry]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM template_entries WHERE id=?", (entry_id,))
            r = cur.fetchone()
        return self._row_to_template_entry(r) if r else None

    def list_template_entries(self) -> List[TemplateEntry]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM template_entries ORDER BY day_of_week, start_time")
            rows = cur.fetchall()
        return [self._row_to_template_entry(r) for r in rows]

    def template_entries_overlap(self, day_of_week: int, start_time: str, end_time: str,
                                  exclude_id: Optional[int] = None) -> bool:
        """True if [start_time, end_time) on that weekday overlaps an existing template entry."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT id, start_time, end_time FROM template_entries WHERE day_of_week=?",
                (day_of_week,),
            )
            rows = cur.fetchall()
        for r in rows:
            if exclude_id is not None and r["id"] == exclude_id:
                continue
            if start_time < r["end_time"] and r["start_time"] < end_time:
                return True
        return False

    @staticmethod
    def _row_to_template_entry(r) -> TemplateEntry:
        return TemplateEntry(
            id=r["id"], activity_id=r["activity_id"], activity_name=r["activity_name"],
            jira_key=r["jira_key"], color=r["color"], day_of_week=r["day_of_week"],
            start_time=r["start_time"], end_time=r["end_time"], notes=r["notes"],
            jira_project=r["jira_project"], issue_type=r["issue_type"],
        )

    def apply_template_to_week(self, week_dates: List[str]) -> Tuple[int, List[TemplateEntry]]:
        """Copy every template block onto the given week (ISO date strings,
        Mon..Fri, indexed 0-4) as real time entries. Blocks whose target slot
        already has an entry are skipped (not overwritten) and returned so
        the caller can report them."""
        template = self.list_template_entries()
        created = 0
        skipped = []
        for t in template:
            if t.day_of_week < 0 or t.day_of_week >= len(week_dates):
                continue
            target_date = week_dates[t.day_of_week]
            if self.entries_overlap(target_date, t.start_time, t.end_time):
                skipped.append(t)
                continue
            self.add_time_entry(TimeEntry(
                None, t.activity_id, t.activity_name, t.jira_key, t.color,
                target_date, t.start_time, t.end_time, t.notes,
                jira_project=t.jira_project, issue_type=t.issue_type,
            ))
            created += 1
        return created, skipped

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key=?", (key,))
            r = cur.fetchone()
        return r["value"] if r else default

    def set_setting(self, key: str, value: str):
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
