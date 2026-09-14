"""Plain data classes used across the app."""
from dataclasses import dataclass
from typing import Optional


@dataclass
class Project:
    """The overarching, collapsible group shown in the sidebar (e.g.
    "Client A", "Internal Work"). Every Activity belongs to exactly one
    Project -- see Activity below -- and a time block's color always comes
    from its Activity's Project, never set on the Activity itself. Purely
    organizational otherwise: renaming/recoloring one doesn't touch
    anything about how its Activities export to Jira."""
    id: Optional[int]
    name: str
    color: str = "#4C6EF5"
    sort_order: int = 0
    collapsed: bool = False


@dataclass
class Activity:
    """A single loggable thing you spend time on (e.g. "Sprint Planning",
    "Client A Retainer") -- what actually gets dragged onto the calendar.
    Shown grouped under its Project in the sidebar.

    `color` here is a read-only convenience, not a real field of its own:
    Database.list_activities()/get_activity() always join it in from the
    Activity's Project (see Project.color above) so every other part of the
    app can keep reading `activity.color` without caring where it actually
    comes from. Setting it directly on an Activity instance has no effect
    on what's stored -- change Project.color instead.
    """
    id: Optional[int]
    name: str
    jira_key: Optional[str] = None
    default_duration_minutes: Optional[int] = None
    archived: bool = False
    # The Jira *project* this exports under, e.g. "Quasar Delivery
    # Management" -- named jira_project (not just "project") to avoid
    # colliding with the sidebar's own "Project" grouping concept above.
    # Almost always the same for everything, so it lives in Settings as a
    # default rather than needing to be set here per activity.
    jira_project: Optional[str] = None
    issue_type: Optional[str] = None    # Jira issue type, e.g. "Sub-task"
    project_id: Optional[int] = None    # which Project this belongs to -- required in practice
    color: str = "#4C6EF5"              # denormalized from project_id's Project -- see docstring
    jira_status: Optional[str] = None
    jira_status_category: Optional[str] = None

    def is_closed(self) -> bool:
        """True when Jira considers this ticket done — used to mute/strike
        it in the sidebar so closed QDMs don't look like live work."""
        category = (self.jira_status_category or "").strip().lower()
        if category == "done":
            return True
        name = (self.jira_status or "").strip().lower()
        return name in {
            "done", "closed", "resolved", "cancelled", "canceled",
            "declined", "complete", "completed", "work completed", "won't do",
        }


@dataclass
class TimeEntry:
    id: Optional[int]
    activity_id: Optional[int]
    activity_name: str
    jira_key: Optional[str]
    color: str
    date: str          # "YYYY-MM-DD"
    start_time: str    # "HH:MM" (24h)
    end_time: str      # "HH:MM" (24h)
    notes: str = ""
    jira_project: Optional[str] = None
    issue_type: Optional[str] = None
    # Id of the Jira worklog this block was last pushed as, if any -- used
    # so a later "Push hours to Jira" can PUT (update) that worklog instead
    # of posting a duplicate. None means never pushed (or pushed via the
    # old CSV path, which Jira doesn't give us an id back for).
    jira_worklog_id: Optional[str] = None
    # Issue key the stored worklog id actually lives on, and a fingerprint
    # of the last payload we successfully sent. Together they let a later
    # push skip untouched blocks, PUT real edits, and DELETE+POST when the
    # block is moved to a different QDM.
    jira_worklog_issue: Optional[str] = None
    jira_worklog_fingerprint: Optional[str] = None

    def duration_minutes(self) -> int:
        sh, sm = (int(x) for x in self.start_time.split(":"))
        eh, em = (int(x) for x in self.end_time.split(":"))
        return (eh * 60 + em) - (sh * 60 + sm)


@dataclass
class PendingWorklogDelete:
    """A Jira worklog that should be removed on the next push, after the
    local time block that owned it was deleted."""
    id: Optional[int]
    jira_key: str
    jira_worklog_id: str
    date: str          # "YYYY-MM-DD" of the deleted block, for range pushes


@dataclass
class TemplateEntry:
    """A recurring weekly block on the permanent "Template" tab. Same shape
    as TimeEntry, except it's keyed by weekday (0=Monday..4=Friday) instead
    of a real calendar date, since a template isn't tied to any given week."""
    id: Optional[int]
    activity_id: Optional[int]
    activity_name: str
    jira_key: Optional[str]
    color: str
    day_of_week: int   # 0=Monday .. 4=Friday
    start_time: str    # "HH:MM" (24h)
    end_time: str      # "HH:MM" (24h)
    notes: str = ""
    jira_project: Optional[str] = None
    issue_type: Optional[str] = None

    def duration_minutes(self) -> int:
        sh, sm = (int(x) for x in self.start_time.split(":"))
        eh, em = (int(x) for x in self.end_time.split(":"))
        return (eh * 60 + em) - (sh * 60 + sm)
