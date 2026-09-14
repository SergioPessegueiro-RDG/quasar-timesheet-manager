"""Talk to Jira Cloud / Data Center over REST, using only the standard
library (urllib) -- same constraint as update_check.py, so this app stays
dependency-free.

Credentials live in the local SQLite settings table (see Settings) and
can be overridden by environment variables so a token can be supplied
without typing it into the UI:

    QUASAR_JIRA_URL       https://your-site.atlassian.net
    QUASAR_JIRA_EMAIL     the Atlassian account email the token belongs to
    QUASAR_JIRA_TOKEN     API token (Cloud) or personal access token (DC)
    QUASAR_JIRA_PROJECT   Jira project key, default QDM

Cloud auth is HTTP Basic with email:token. Data Center PATs use Bearer
when no email is set.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Union

from . import config, update_check

DEFAULT_PROJECT_KEY = "QDM"
_USER_AGENT = "QUASAR-Timesheet-Manager-jira"
_TIMEOUT_SECONDS = 30
_PAGE_SIZE = 50
# Closed tickets sit in a collapsed "Closed" subsection. Open and closed
# are fetched separately so a pile of recently-updated Done tickets
# cannot crowd out live work still assigned to you.
_MAX_OPEN_ISSUES = 200
_MAX_CLOSED_ISSUES = 200
_MAX_ISSUES = _MAX_OPEN_ISSUES  # used by the sync dialog when a fetch is truncated


class JiraError(Exception):
    """Any failure talking to Jira that the UI should show as a message."""


def normalize_site_url(raw: str) -> str:
    """Turn a pasted Jira link into the API host.

    People naturally paste the dashboard / board / issue they already have
    open (e.g. https://acme.atlassian.net/jira/dashboards/10000). Hitting
    that path with /rest/api/2/myself returns the HTML page, which is
    exactly the "wasn't JSON" error. Cloud sites always live at the host
    root; Data Center may sit under /jira.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if not text.lower().startswith(("http://", "https://")):
        text = "https://" + text
    parsed = urllib.parse.urlparse(text)
    host = parsed.netloc
    if not host:
        return text.rstrip("/")
    scheme = parsed.scheme or "https"
    host_l = host.lower()
    if host_l.endswith(".atlassian.net") or host_l.endswith(".atlassian.com"):
        return f"https://{host}"
    path = parsed.path or ""
    lowered = path.lower()
    for marker in (
        "/secure", "/browse", "/issues", "/rest", "/login.jsp", "/login",
        "/plugins", "/servicedesk", "/jira/software", "/jira/dashboards",
        "/jira/people", "/jira/your-work", "/jira/for-you",
    ):
        idx = lowered.find(marker)
        if idx >= 0:
            path = path[:idx]
            break
    path = path.rstrip("/")
    return urllib.parse.urlunparse((scheme, host, path, "", "", "")).rstrip("/")


@dataclass
class JiraCredentials:
    site_url: str
    email: str
    api_token: str
    project_key: str = DEFAULT_PROJECT_KEY

    def normalized_url(self) -> str:
        return normalize_site_url(self.site_url)

    def is_complete(self) -> bool:
        return bool(self.normalized_url() and self.api_token.strip())


@dataclass
class JiraIssue:
    key: str
    summary: str
    issue_type: str
    project_key: str
    project_name: str
    status: str
    parent_key: Optional[str] = None
    parent_summary: Optional[str] = None
    status_category: Optional[str] = None


def credentials_from_env() -> JiraCredentials:
    return JiraCredentials(
        site_url=os.environ.get("QUASAR_JIRA_URL", "").strip(),
        email=os.environ.get("QUASAR_JIRA_EMAIL", "").strip(),
        api_token=os.environ.get("QUASAR_JIRA_TOKEN", "").strip(),
        project_key=os.environ.get("QUASAR_JIRA_PROJECT", DEFAULT_PROJECT_KEY).strip()
        or DEFAULT_PROJECT_KEY,
    )


def credentials_from_settings(get_setting) -> JiraCredentials:
    """Merge saved settings with env overrides. Env wins when set, so a
    token handed over later (or a CI/dev shell) doesn't have to be typed
    into Settings first."""
    env = credentials_from_env()
    saved_url = (get_setting("jira_site_url", "") or "").strip()
    saved_email = (get_setting("jira_email", "") or "").strip()
    saved_token = (get_setting("jira_api_token", "") or "").strip()
    saved_key = (get_setting("jira_project_key", DEFAULT_PROJECT_KEY) or DEFAULT_PROJECT_KEY).strip()
    return JiraCredentials(
        site_url=env.site_url or saved_url,
        email=env.email or saved_email,
        api_token=env.api_token or saved_token,
        project_key=env.project_key if os.environ.get("QUASAR_JIRA_PROJECT") else (
            saved_key or DEFAULT_PROJECT_KEY),
    )


def _headers(creds: JiraCredentials) -> Dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
    }
    token = creds.api_token.strip()
    email = creds.email.strip()
    if email:
        raw = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {raw}"
    else:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _request(creds: JiraCredentials, method: str, path: str,
             query: Optional[Dict[str, str]] = None,
             body: Optional[dict] = None) -> Any:
    url = creds.normalized_url()
    if not url:
        raise JiraError("Jira site URL is missing. Add it in Settings.")
    if not creds.api_token.strip():
        raise JiraError("Jira API token is missing. Add it in Settings, or set QUASAR_JIRA_TOKEN.")
    if not url.lower().startswith("https://"):
        raise JiraError("Jira site URL must start with https://")

    full = f"{url}{path}"
    if query:
        full = f"{full}?{urllib.parse.urlencode(query)}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(full, data=data, headers=_headers(creds), method=method)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS,
                                    context=update_check.build_ssl_context()) as resp:
            raw = resp.read()
            if not raw:
                return {}
            text = raw.decode("utf-8", errors="replace")
            stripped = text.lstrip()
            if stripped[:15].lower().startswith("<!doctype") or stripped[:6].lower().startswith("<html"):
                raise JiraError(
                    "Jira sent back a webpage instead of data — the Site URL is "
                    "probably a dashboard or board link, not the site itself.\n\n"
                    "Paste only https://your-company.atlassian.net (or paste the "
                    "dashboard link again; the extra path is now stripped automatically)."
                )
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise JiraError(
                    "Jira returned a response that wasn't JSON. Check the Site URL "
                    "is https://your-company.atlassian.net with no /jira/... path."
                ) from exc
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            payload = exc.read().decode("utf-8", errors="replace")
            parsed = json.loads(payload) if payload else {}
            messages = parsed.get("errorMessages") or []
            if messages:
                detail = " ".join(str(m) for m in messages)
            elif parsed.get("message"):
                detail = str(parsed["message"])
            else:
                detail = payload[:300]
        except Exception:
            detail = str(exc)
        if exc.code in (401, 403):
            raise JiraError(
                "Jira rejected the credentials (HTTP "
                f"{exc.code}). Check the site URL, email, and API token."
            ) from exc
        raise JiraError(f"Jira request failed (HTTP {exc.code}): {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise JiraError(f"Couldn't reach Jira: {exc.reason}") from exc


def _log(message: str):
    try:
        os.makedirs(config.APP_DIR, exist_ok=True)
        with open(os.path.join(config.APP_DIR, "jira.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except Exception:
        pass


def test_connection(creds: JiraCredentials) -> str:
    """Hit /myself and return the display name. Raises JiraError on failure."""
    me = _request(creds, "GET", "/rest/api/2/myself")
    name = (me.get("displayName") or me.get("emailAddress") or "").strip()
    if not name:
        raise JiraError("Connected, but Jira didn't return a user name.")
    return name


def _issue_from_payload(raw: dict) -> Optional[JiraIssue]:
    key = (raw.get("key") or "").strip()
    fields = raw.get("fields") or {}
    if not key:
        return None
    issuetype = fields.get("issuetype") or {}
    project = fields.get("project") or {}
    parent = fields.get("parent") or {}
    parent_fields = parent.get("fields") or {}
    status = fields.get("status") or {}
    status_cat = status.get("statusCategory") or {}
    status_category = (
        (status_cat.get("key") or status_cat.get("name") or "").strip().lower() or None
    )
    return JiraIssue(
        key=key,
        summary=(fields.get("summary") or key).strip(),
        issue_type=(issuetype.get("name") or "").strip(),
        project_key=(project.get("key") or "").strip(),
        project_name=(project.get("name") or "").strip(),
        status=(status.get("name") or "").strip(),
        parent_key=(parent.get("key") or None),
        parent_summary=(parent_fields.get("summary") or None),
        status_category=status_category,
    )


def _issues_from_search_payload(payload: Any) -> List[JiraIssue]:
    if not isinstance(payload, dict):
        raise JiraError("Jira search returned something that wasn't a JSON object.")
    issues = []
    for raw in payload.get("issues") or []:
        parsed = _issue_from_payload(raw)
        if parsed is not None:
            issues.append(parsed)
    return issues


def _search_jql(creds: JiraCredentials, jql: str, limit: int = _MAX_ISSUES) -> List[JiraIssue]:
    """Try Cloud enhanced search, then classic POST/GET search."""
    fields = ["summary", "issuetype", "parent", "status", "project"]
    errors: List[JiraError] = []
    limit = max(1, int(limit))

    try:
        issues: List[JiraIssue] = []
        next_page = None
        while len(issues) < limit:
            body: Dict[str, Any] = {
                "jql": jql,
                "maxResults": min(_PAGE_SIZE, limit - len(issues)),
                "fields": fields,
            }
            if next_page:
                body["nextPageToken"] = next_page
            payload = _request(creds, "POST", "/rest/api/3/search/jql", body=body)
            batch = _issues_from_search_payload(payload)
            issues.extend(batch)
            next_page = payload.get("nextPageToken")
            if not next_page or not batch:
                break
        _log(f"search/jql ok jql={jql!r} count={len(issues)}")
        return issues[:limit]
    except JiraError as exc:
        _log(f"search/jql failed: {exc}")
        errors.append(exc)

    for method, path, use_query in (
        ("POST", "/rest/api/2/search", False),
        ("GET", "/rest/api/2/search", True),
        ("POST", "/rest/api/3/search", False),
    ):
        try:
            issues = []
            start_at = 0
            total = None
            while len(issues) < limit:
                query = None
                body = None
                page = min(_PAGE_SIZE, limit - len(issues))
                params = {
                    "jql": jql,
                    "startAt": start_at,
                    "maxResults": page,
                    "fields": fields,
                }
                if use_query:
                    query = {
                        "jql": jql,
                        "startAt": str(start_at),
                        "maxResults": str(page),
                        "fields": ",".join(fields),
                    }
                else:
                    body = params
                payload = _request(creds, method, path, query=query, body=body)
                batch = _issues_from_search_payload(payload)
                issues.extend(batch)
                if total is None:
                    total = int(payload.get("total") or 0)
                start_at += max(len(batch), 1)
                if not batch or (total and start_at >= total):
                    break
            _log(f"{method} {path} ok jql={jql!r} count={len(issues)}")
            return issues[:limit]
        except JiraError as exc:
            _log(f"{method} {path} failed: {exc}")
            errors.append(exc)

    raise errors[0] if errors else JiraError("Jira search failed.")


def _project_key(project_key: str) -> str:
    return (project_key or DEFAULT_PROJECT_KEY).strip() or DEFAULT_PROJECT_KEY


def assigned_open_jql(project_key: str) -> str:
    """Live tickets still assigned to you — no date window, so an old QDM
    you still own is not dropped just because nothing touched it recently."""
    key = _project_key(project_key)
    return (
        f'assignee = currentUser() AND project = {key} '
        f'AND statusCategory != Done ORDER BY updated DESC'
    )


def assigned_closed_jql(project_key: str, today: Optional[Union[date, datetime]] = None) -> str:
    """Recently-updated Done tickets assigned to you, for the collapsed
    Closed subsection. Date-windowed so years of old Done work don't
    fill the sidebar."""
    if today is None:
        today = date.today()
    elif isinstance(today, datetime):
        today = today.date()
    start = date(today.year - 1, 1, 1).isoformat()
    key = _project_key(project_key)
    return (
        f'assignee = currentUser() AND project = {key} '
        f'AND statusCategory = Done AND updated >= "{start}" ORDER BY updated DESC'
    )


def assigned_issues_jql(project_key: str, today: Optional[Union[date, datetime]] = None) -> str:
    """Back-compat single query (open + closed). Sync uses the split
    open/closed JQLs in fetch_issues() instead."""
    if today is None:
        today = date.today()
    elif isinstance(today, datetime):
        today = today.date()
    start = date(today.year - 1, 1, 1).isoformat()
    key = _project_key(project_key)
    return (
        f'assignee = currentUser() AND project = {key} '
        f'AND updated >= "{start}" ORDER BY updated DESC'
    )


@dataclass
class FetchResult:
    issues: List[JiraIssue]
    open_capped: bool = False
    closed_capped: bool = False


def fetch_issues(creds: JiraCredentials, jql: Optional[str] = None) -> FetchResult:
    """Issues assigned to the token's user in the configured Jira project
    (QDM by default). Read only — nothing is written back to Jira.

    Open tickets are fetched first (still assigned, any age). Closed
    tickets are a second query so Done work cannot crowd them out.
    """
    if jql is not None:
        issues = _search_jql(creds, jql)
        return FetchResult(issues=issues, open_capped=len(issues) >= _MAX_ISSUES)

    open_issues = _search_jql(creds, assigned_open_jql(creds.project_key), _MAX_OPEN_ISSUES)
    closed_issues = _search_jql(creds, assigned_closed_jql(creds.project_key), _MAX_CLOSED_ISSUES)
    by_key: Dict[str, JiraIssue] = {}
    for issue in open_issues + closed_issues:
        key = (issue.key or "").strip().upper()
        if key:
            by_key[key] = issue
    result = FetchResult(
        issues=list(by_key.values()),
        open_capped=len(open_issues) >= _MAX_OPEN_ISSUES,
        closed_capped=len(closed_issues) >= _MAX_CLOSED_ISSUES,
    )
    _log(f"fetch_issues open={len(open_issues)} closed={len(closed_issues)} "
         f"merged={len(result.issues)} open_capped={result.open_capped} "
         f"closed_capped={result.closed_capped}")
    return result


def worklog_started(date_str: str, start_time: str) -> str:
    """Jira's worklog `started` stamp: 2026-07-24T09:00:00.000+0100."""
    naive = datetime.strptime(f"{date_str} {start_time}", "%Y-%m-%d %H:%M")
    local = naive.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return local.strftime("%Y-%m-%dT%H:%M:%S.000%z")


def add_worklog(creds: JiraCredentials, issue_key: str, started: str,
                time_spent_seconds: int, comment: str) -> str:
    if time_spent_seconds < 60:
        raise JiraError("Jira won't accept a worklog shorter than 1 minute.")
    body = {
        "started": started,
        "timeSpentSeconds": int(time_spent_seconds),
        "comment": (comment or "").strip() or config.DEFAULT_JIRA_PROJECT,
    }
    payload = _request(creds, "POST", f"/rest/api/2/issue/{urllib.parse.quote(issue_key)}/worklog",
                       body=body)
    worklog_id = str(payload.get("id") or "").strip()
    if not worklog_id:
        raise JiraError(f"Jira accepted the worklog for {issue_key} but didn't return an id.")
    return worklog_id


def update_worklog(creds: JiraCredentials, issue_key: str, worklog_id: str,
                   started: str, time_spent_seconds: int, comment: str) -> str:
    if time_spent_seconds < 60:
        raise JiraError("Jira won't accept a worklog shorter than 1 minute.")
    body = {
        "started": started,
        "timeSpentSeconds": int(time_spent_seconds),
        "comment": (comment or "").strip() or config.DEFAULT_JIRA_PROJECT,
    }
    path = (f"/rest/api/2/issue/{urllib.parse.quote(issue_key)}"
            f"/worklog/{urllib.parse.quote(str(worklog_id))}")
    payload = _request(creds, "PUT", path, body=body)
    return str(payload.get("id") or worklog_id)


def delete_worklog(creds: JiraCredentials, issue_key: str, worklog_id: str) -> None:
    path = (f"/rest/api/2/issue/{urllib.parse.quote(issue_key)}"
            f"/worklog/{urllib.parse.quote(str(worklog_id))}")
    _request(creds, "DELETE", path)


@dataclass
class JiraTransition:
    """One workflow button Jira will currently allow on an issue."""
    id: str
    name: str
    to_name: str = ""
    to_category: Optional[str] = None

    def label(self) -> str:
        dest = (self.to_name or self.name or "").strip()
        action = (self.name or "").strip()
        if dest and action and dest.lower() != action.lower():
            return f"{action} → {dest}"
        return dest or action

    def closes_ticket(self) -> bool:
        category = (self.to_category or "").strip().lower()
        if category == "done":
            return True
        name = (self.to_name or self.name or "").strip().lower()
        return name in {
            "done", "closed", "resolved", "complete", "completed",
            "work completed", "won't do", "cancelled", "canceled",
        }


def parse_transitions(payload: Any) -> List[JiraTransition]:
    if not isinstance(payload, dict):
        return []
    out: List[JiraTransition] = []
    for raw in payload.get("transitions") or []:
        if not isinstance(raw, dict):
            continue
        trans_id = str(raw.get("id") or "").strip()
        if not trans_id:
            continue
        dest = raw.get("to") or {}
        cat = dest.get("statusCategory") or {}
        to_category = (cat.get("key") or cat.get("name") or "").strip().lower() or None
        out.append(JiraTransition(
            id=trans_id,
            name=(raw.get("name") or "").strip(),
            to_name=(dest.get("name") or "").strip(),
            to_category=to_category,
        ))
    return out


def preferred_close_transition(transitions: List[JiraTransition]) -> Optional[JiraTransition]:
    """Work Completed first, then any Done-category status."""
    for trans in transitions:
        if (trans.to_name or trans.name).strip().lower() == "work completed":
            return trans
    for trans in transitions:
        if trans.closes_ticket():
            return trans
    return None


def list_transitions(creds: JiraCredentials, issue_key: str) -> List[JiraTransition]:
    key = (issue_key or "").strip()
    if not key:
        return []
    payload = _request(
        creds, "GET",
        f"/rest/api/2/issue/{urllib.parse.quote(key)}/transitions",
    )
    return parse_transitions(payload)


def transition_issue(creds: JiraCredentials, issue_key: str, transition_id: str) -> None:
    key = (issue_key or "").strip()
    trans_id = str(transition_id or "").strip()
    if not key or not trans_id:
        raise JiraError("Missing issue key or status transition.")
    _request(
        creds, "POST",
        f"/rest/api/2/issue/{urllib.parse.quote(key)}/transitions",
        body={"transition": {"id": trans_id}},
    )
