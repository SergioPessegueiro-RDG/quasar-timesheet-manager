"""Turn Jira issues into sidebar Activities/Projects, and time blocks into
Jira worklogs.

Kept separate from jira_client.py (which is the HTTP layer) so the mapping
rules can be unit-tested without a network, and so the UI only has to call
two functions: sync_issues_into_db and push_worklogs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from . import config, jira_client
from .db import Database
from .models import Activity, TimeEntry


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    projects_created: int = 0
    fetched: int = 0
    open_capped: bool = False
    closed_capped: bool = False


@dataclass
class PushItemResult:
    entry_id: Optional[int]
    key: str
    ok: bool
    detail: str = ""


@dataclass
class PushResult:
    created: int = 0
    updated: int = 0
    skipped: List[TimeEntry] = field(default_factory=list)
    failed: List[PushItemResult] = field(default_factory=list)


def group_name_for_issue(issue: jira_client.JiraIssue) -> str:
    """Sidebar Project this QDM should land under.

    Parent/epic first (a sub-task of "Sprint 14" belongs with the rest of
    that epic), then issue type ("Bug", "Story"), then the Jira project
    name, so every fetched issue has somewhere to go.
    """
    parent = (issue.parent_summary or "").strip()
    if parent:
        return parent
    if issue.issue_type:
        return issue.issue_type
    return issue.project_name or issue.project_key or "QDM"


def sync_issues_into_db(
    db: Database,
    issues: List[jira_client.JiraIssue],
    *,
    archive_open_missing: bool = True,
    archive_closed_missing: bool = True,
    open_capped: bool = False,
    closed_capped: bool = False,
) -> SyncResult:
    """Create/update Activities (and their Projects) from Jira issues.

    Match on Jira Issue Key. An existing activity with that key gets its
    name / issue type / Jira project refreshed; a new key becomes a new
    activity under the Project named by group_name_for_issue(). Existing
    activities that Jira didn't return are left in the database (time
    blocks may still point at them) but QDM-keyed ones are archived so
    they drop out of the sidebar after a non-empty fetch.

    Archiving is skipped per open/closed when that half of the fetch was
    truncated at the cap — otherwise tickets still assigned to you but
    not in the newest-N page would vanish, leaving empty epic groups.
    """
    result = SyncResult(
        fetched=len(issues),
        open_capped=open_capped,
        closed_capped=closed_capped,
    )
    projects_by_name = {p.name.strip().lower(): p for p in db.list_projects()}
    activities_by_key = {}
    for act in db.list_activities(include_archived=True):
        if act.jira_key and act.jira_key.strip():
            activities_by_key[act.jira_key.strip().upper()] = act

    for issue in issues:
        key = (issue.key or "").strip()
        if not key:
            continue
        group = group_name_for_issue(issue)
        project = projects_by_name.get(group.lower())
        if project is None:
            project = db.add_project_with_default_color(group)
            projects_by_name[group.lower()] = project
            result.projects_created += 1

        jira_project_name = issue.project_name or config.DEFAULT_JIRA_PROJECT
        status = issue.status or None
        status_category = issue.status_category or None
        existing = activities_by_key.get(key.upper())
        if existing is None:
            new_id = db.add_activity(Activity(
                None, issue.summary, key, None,
                jira_project=jira_project_name,
                issue_type=issue.issue_type or None,
                project_id=project.id,
                jira_status=status,
                jira_status_category=status_category,
            ))
            created = db.get_activity(new_id)
            if created is not None:
                activities_by_key[key.upper()] = created
            result.created += 1
            continue

        changed = (
            existing.name != issue.summary
            or (existing.issue_type or "") != (issue.issue_type or "")
            or (existing.jira_project or "") != jira_project_name
            or existing.project_id != project.id
            or (existing.jira_status or "") != (status or "")
            or (existing.jira_status_category or "") != (status_category or "")
            or existing.archived
        )
        if not changed:
            continue
        existing.name = issue.summary
        existing.jira_key = key
        existing.issue_type = issue.issue_type or None
        existing.jira_project = jira_project_name
        existing.project_id = project.id
        existing.jira_status = status
        existing.jira_status_category = status_category
        existing.archived = False
        db.update_activity(existing)
        result.updated += 1

    # A previous oversized sync left hundreds of old QDMs in the local DB.
    # After a successful fetch, hide any QDM-keyed activity Jira didn't
    # return — time blocks stay, they just drop out of the sidebar. Skip
    # this on an empty fetch so a JQL miss doesn't wipe the list. Skip
    # open or closed separately when that query hit its cap, so tickets
    # still assigned to you aren't archived just because they weren't
    # among the newest N.
    if issues and (archive_open_missing or archive_closed_missing):
        prefix = (config.JIRA_KEY_PREFIX or "QDM-").upper()
        returned_keys = {
            (issue.key or "").strip().upper()
            for issue in issues if (issue.key or "").strip()
        }
        for act in db.list_activities(include_archived=True):
            key = (act.jira_key or "").strip().upper()
            if not key.startswith(prefix) or key in returned_keys or act.archived:
                continue
            if act.is_closed():
                if not archive_closed_missing:
                    continue
            elif not archive_open_missing:
                continue
            act.archived = True
            db.update_activity(act)

    db.collapse_all_projects()
    return result


def worklog_comment(entry: TimeEntry) -> str:
    notes = " ".join((entry.notes or "").split())
    return notes or (entry.activity_name or "")


def push_worklogs(db: Database, creds: jira_client.JiraCredentials,
                  entries: List[TimeEntry]) -> PushResult:
    """POST new worklogs / PUT ones we've already pushed.

    Entries without a Jira Issue Key are skipped (same rule as CSV export).
    A worklog id stored on the row means this block was pushed before --
    we update that worklog so a later edit of hours doesn't double-book.
    If Jira no longer has that worklog (404), we post a fresh one.
    """
    result = PushResult()
    for entry in entries:
        key = (entry.jira_key or "").strip()
        if not key:
            result.skipped.append(entry)
            continue
        seconds = max(0, entry.duration_minutes()) * 60
        if seconds < 60:
            result.failed.append(PushItemResult(entry.id, key, False, "shorter than 1 minute"))
            continue
        started = jira_client.worklog_started(entry.date, entry.start_time)
        comment = worklog_comment(entry)
        try:
            if entry.jira_worklog_id:
                try:
                    worklog_id = jira_client.update_worklog(
                        creds, key, entry.jira_worklog_id, started, seconds, comment)
                except jira_client.JiraError as exc:
                    if "HTTP 404" not in str(exc):
                        raise
                    worklog_id = jira_client.add_worklog(creds, key, started, seconds, comment)
                    result.created += 1
                else:
                    result.updated += 1
            else:
                worklog_id = jira_client.add_worklog(creds, key, started, seconds, comment)
                result.created += 1
        except jira_client.JiraError as exc:
            result.failed.append(PushItemResult(entry.id, key, False, str(exc)))
            continue
        if entry.id is not None:
            db.set_time_entry_worklog_id(entry.id, worklog_id)
            entry.jira_worklog_id = worklog_id
    return result
