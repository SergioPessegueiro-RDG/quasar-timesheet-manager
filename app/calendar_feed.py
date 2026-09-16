"""Outlook / Google ICS calendar overlay.

The timesheet stays local and dependency-free, so this talks to a
*published ICS URL* (Outlook: Settings → Calendar → Shared calendars →
Publish a calendar) over urllib, the same way jira_client.py talks to
Jira. No OAuth, no Microsoft Graph app registration.

Fetched events are a visual guide only -- they never become TimeEntry
rows, never count toward day totals, and never export to Jira.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from calendar import monthrange
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from . import config, update_check

_USER_AGENT = "QUASAR-Timesheet-Manager-calendar"
_TIMEOUT_SECONDS = 30
_MAX_ICS_BYTES = 8 * 1024 * 1024
_MAX_STEPS = 20000
CACHE_FILENAME = "calendar.ics"

# Windows Outlook TZID -> IANA. Used when zoneinfo is available so a
# "GMT Standard Time" meeting converts correctly on a laptop that isn't
# set to London. Unknown names fall back to floating/local time.
_WINDOWS_TZ = {
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Etc/GMT",
    "UTC": "UTC",
    "UTC Standard Time": "UTC",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw",
    "Romance Standard Time": "Europe/Paris",
    "GTB Standard Time": "Europe/Bucharest",
    "E. Europe Standard Time": "Europe/Helsinki",
    "FLE Standard Time": "Europe/Kiev",
    "Russian Standard Time": "Europe/Moscow",
    "GMT+0": "Etc/GMT",
    "Eastern Standard Time": "America/New_York",
    "US Eastern Standard Time": "America/Indianapolis",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "US Mountain Standard Time": "America/Phoenix",
    "Pacific Standard Time": "America/Los_Angeles",
    "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "Atlantic Standard Time": "America/Halifax",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "AUS Central Standard Time": "Australia/Darwin",
    "E. Australia Standard Time": "Australia/Brisbane",
    "W. Australia Standard Time": "Australia/Perth",
    "New Zealand Standard Time": "Pacific/Auckland",
    "India Standard Time": "Asia/Kolkata",
    "Singapore Standard Time": "Asia/Singapore",
    "China Standard Time": "Asia/Shanghai",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul",
    "Arabian Standard Time": "Asia/Dubai",
}

_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_DURATION_RE = re.compile(
    r"^P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$"
)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8
    ZoneInfo = None  # type: ignore


class CalendarFeedError(Exception):
    """Any failure fetching or parsing an ICS feed that the UI should show."""


@dataclass
class CalendarEvent:
    """One timed occurrence on a real calendar date, already in local time.

    `start_time`/`end_time` match TimeEntry so display_hours_for_entries
    can widen the grid when a meeting sits outside Settings' work hours.
    """
    uid: str
    title: str
    date: str          # YYYY-MM-DD
    start_time: str    # HH:MM
    end_time: str      # HH:MM (24:00 allowed)
    all_day: bool = False
    location: str = ""
    description: str = ""
    organizer: str = ""


@dataclass
class _ParsedEvent:
    uid: str
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    rrule: Optional[str] = None
    rdates: List[datetime] = field(default_factory=list)
    exdates: List[datetime] = field(default_factory=list)
    recurrence_id: Optional[datetime] = None
    cancelled: bool = False
    skip: bool = False
    location: str = ""
    description: str = ""
    organizer: str = ""


class CalendarFeed:
    """Parsed ICS kept in memory so week navigation doesn't hit the network."""

    def __init__(self):
        self._events: List[_ParsedEvent] = []
        self._exceptions: Dict[str, List[_ParsedEvent]] = {}

    def clear(self):
        self._events = []
        self._exceptions = {}

    def replace_from_ics(self, text: str):
        events, exceptions = _parse_ics(text)
        self._events = events
        self._exceptions = exceptions

    def is_empty(self) -> bool:
        return not self._events and not self._exceptions

    def extend(self, other: "CalendarFeed", source_id: str = ""):
        """Append another feed's events. `source_id` prefixes UIDs so two
        published calendars can't smash each other's recurrence exceptions."""
        if other is None or other is self:
            return
        prefix = f"{source_id}:" if source_id else ""
        for ev in other._events:
            self._events.append(replace(ev, uid=prefix + ev.uid) if prefix else ev)
        for uid, exceptions in other._exceptions.items():
            key = prefix + uid
            self._exceptions.setdefault(key, []).extend(
                [replace(ex, uid=prefix + (ex.uid or uid)) if prefix else ex
                 for ex in exceptions]
            )

    def events_between(self, start: date, end: date) -> List[CalendarEvent]:
        """Timed (not all-day) occurrences whose local date sits in [start, end]."""
        if end < start:
            return []
        out: List[CalendarEvent] = []
        for parsed in self._events:
            if parsed.skip or parsed.cancelled or parsed.all_day:
                continue
            if parsed.recurrence_id is not None:
                continue
            exceptions = self._exceptions.get(parsed.uid, [])
            skip_starts = {_flatten(ex.recurrence_id) for ex in exceptions if ex.recurrence_id}
            skip_starts.update(_flatten(x) for x in parsed.exdates)
            if parsed.rrule:
                for occ_start, occ_end in _expand_rrule(parsed, start, end):
                    if _flatten(occ_start) in skip_starts:
                        continue
                    out.extend(_split_days(
                        parsed.uid, parsed.title, occ_start, occ_end, start, end,
                        location=parsed.location, description=parsed.description,
                        organizer=parsed.organizer))
            else:
                out.extend(_split_days(
                    parsed.uid, parsed.title, parsed.start, parsed.end, start, end,
                    location=parsed.location, description=parsed.description,
                    organizer=parsed.organizer))
            for rdate in parsed.rdates:
                duration = parsed.end - parsed.start
                out.extend(_split_days(
                    parsed.uid, parsed.title, rdate, rdate + duration, start, end,
                    location=parsed.location, description=parsed.description,
                    organizer=parsed.organizer))
        for uid, exceptions in self._exceptions.items():
            for ex in exceptions:
                if ex.skip or ex.cancelled or ex.all_day:
                    continue
                out.extend(_split_days(
                    ex.uid or uid, ex.title, ex.start, ex.end, start, end,
                    location=ex.location, description=ex.description,
                    organizer=ex.organizer))
        out.sort(key=lambda e: (e.date, e.start_time, e.end_time, e.title))
        return _dedupe_events(out)


def cache_path() -> str:
    return os.path.join(config.APP_DIR, CACHE_FILENAME)


def cache_path_for(url: str) -> str:
    digest = hashlib.sha256(normalize_ics_url(url).encode("utf-8")).hexdigest()[:20]
    return os.path.join(config.APP_DIR, f"calendar_{digest}.ics")


def _read_ics_file(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    return text if "BEGIN:VCALENDAR" in text.upper() else None


def read_cache() -> Optional[str]:
    return _read_ics_file(cache_path())


def read_cache_for(url: str) -> Optional[str]:
    return _read_ics_file(cache_path_for(url))


def write_cache(text: str) -> None:
    _write_ics_file(cache_path(), text)


def write_cache_for(url: str, text: str) -> None:
    _write_ics_file(cache_path_for(url), text)
    if not os.path.exists(cache_path()):
        _write_ics_file(cache_path(), text)


def _write_ics_file(path: str, text: str) -> None:
    try:
        os.makedirs(config.APP_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass


def clear_cache() -> None:
    clear_all_caches()


def clear_all_caches() -> None:
    try:
        names = os.listdir(config.APP_DIR)
    except OSError:
        return
    for name in names:
        if name == CACHE_FILENAME or (
            name.startswith("calendar_") and name.endswith(".ics")
        ):
            try:
                os.remove(os.path.join(config.APP_DIR, name))
            except OSError:
                pass


def parse_ics_url_list(raw: str) -> List[str]:
    """JSON array of ICS URLs, or a single pasted URL."""
    text = (raw or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        if isinstance(data, list):
            return normalize_url_list(data)
        return []
    url = normalize_ics_url(text)
    return [url] if url else []


def dump_ics_url_list(urls: List[str]) -> str:
    return json.dumps(normalize_url_list(urls))


def normalize_url_list(urls) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in urls or []:
        url = normalize_ics_url("" if item is None else str(item))
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def saved_ics_urls(get_setting) -> List[str]:
    """Read the current list, falling back to the original single-URL setting."""
    urls = parse_ics_url_list(get_setting("calendar_ics_urls", "") or "")
    if urls:
        return urls
    legacy = normalize_ics_url(get_setting("calendar_ics_url", "") or "")
    return [legacy] if legacy else []


def source_id_for(url: str) -> str:
    return hashlib.sha256(normalize_ics_url(url).encode("utf-8")).hexdigest()[:8]


def normalize_ics_url(raw: str) -> str:
    """Turn a pasted Outlook/Google calendar link into an https URL.

    Outlook's "ICS" copy often uses webcal://; some people paste the URL
    with no scheme at all. Both should still fetch.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    lower = text.lower()
    if lower.startswith("webcal://"):
        text = "https://" + text[9:]
    elif lower.startswith("webcals://"):
        text = "https://" + text[10:]
    elif not lower.startswith(("http://", "https://")):
        text = "https://" + text
    return text.strip()


def fetch_ics(url: str) -> str:
    """GET an ICS feed. Raises CalendarFeedError on any failure."""
    full = normalize_ics_url(url)
    if not full:
        raise CalendarFeedError("Paste an ICS calendar link first.")
    parsed = urllib.parse.urlparse(full)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise CalendarFeedError("That doesn't look like a calendar URL.")
    if not full.lower().startswith("https://"):
        raise CalendarFeedError("Calendar links must start with https://")

    req = urllib.request.Request(full, headers={
        "User-Agent": _USER_AGENT,
        "Accept": "text/calendar, text/plain, */*",
    })
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS,
                                    context=update_check.build_ssl_context()) as resp:
            raw = resp.read(_MAX_ICS_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise CalendarFeedError(
                "The calendar link was rejected (HTTP "
                f"{exc.code}). Publishing may be turned off, or the link expired."
            ) from exc
        raise CalendarFeedError(f"Calendar request failed (HTTP {exc.code}).") from exc
    except urllib.error.URLError as exc:
        raise CalendarFeedError(f"Couldn't reach the calendar: {exc.reason}") from exc
    except Exception as exc:
        raise CalendarFeedError(f"Couldn't fetch the calendar: {exc}") from exc

    if len(raw) > _MAX_ICS_BYTES:
        raise CalendarFeedError("The calendar file is too large to import.")
    text = raw.decode("utf-8-sig", errors="replace")
    if "BEGIN:VCALENDAR" not in text.upper():
        stripped = text.lstrip()
        if stripped[:15].lower().startswith("<!doctype") or stripped[:6].lower().startswith("<html"):
            raise CalendarFeedError(
                "That URL sent back a webpage, not a calendar. In Outlook, "
                "copy the ICS link from Publish a calendar, not the sharing page."
            )
        raise CalendarFeedError("That URL didn't return an .ics calendar.")
    return text


def parse_ics(text: str) -> CalendarFeed:
    feed = CalendarFeed()
    feed.replace_from_ics(text or "")
    return feed


def _log(message: str):
    try:
        os.makedirs(config.APP_DIR, exist_ok=True)
        with open(os.path.join(config.APP_DIR, "calendar_feed.log"), "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# ICS parsing
# ---------------------------------------------------------------------------
def _unfold(text: str) -> List[str]:
    lines: List[str] = []
    for raw in text.splitlines():
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw.rstrip("\r"))
    return lines


def _parse_prop(line: str) -> Optional[Tuple[str, Dict[str, str], str]]:
    if ":" not in line:
        return None
    meta, value = line.split(":", 1)
    parts = meta.split(";")
    name = parts[0].strip().upper()
    if not name:
        return None
    params: Dict[str, str] = {}
    for piece in parts[1:]:
        if "=" not in piece:
            continue
        key, val = piece.split("=", 1)
        params[key.strip().upper()] = val.strip().strip('"')
    return name, params, value


def _unescape(value: str) -> str:
    return (
        value.replace("\\\\", "\x00")
             .replace("\\n", "\n")
             .replace("\\N", "\n")
             .replace("\\,", ",")
             .replace("\\;", ";")
             .replace("\x00", "\\")
    )


def _plain_description(value: str) -> str:
    """ICS DESCRIPTION, including the HTML Outlook sometimes stuffs in."""
    text = _unescape(value or "").strip()
    if "<" in text and ">" in text:
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</p\s*>", "\n\n", text)
        text = re.sub(r"<[^>]+>", "", text)
        text = html.unescape(text)
    lines = [ln.strip() for ln in text.splitlines()]
    cleaned: List[str] = []
    blank = False
    for ln in lines:
        if not ln:
            if cleaned and not blank:
                cleaned.append("")
            blank = True
            continue
        blank = False
        cleaned.append(ln)
    text = "\n".join(cleaned).strip()
    if len(text) > 8000:
        text = text[:8000].rstrip() + "…"
    return text


def _organizer_label(params: Dict[str, str], value: str) -> str:
    cn = _unescape(params.get("CN", "") or "").strip()
    if cn:
        return cn
    email = (value or "").strip()
    if email.lower().startswith("mailto:"):
        email = email[7:]
    return email


def format_meeting_details(event: CalendarEvent) -> str:
    """Plain-text body for the right-click meeting details dialog."""
    lines = [event.title or "(no title)", ""]
    try:
        day = datetime.strptime(event.date, "%Y-%m-%d")
        lines.append(day.strftime("%A, %b %d, %Y"))
    except ValueError:
        lines.append(event.date)
    lines.append(f"{event.start_time} – {event.end_time}")
    if event.location:
        lines.extend(["", "Location", event.location])
    if event.organizer:
        lines.extend(["", "Organizer", event.organizer])
    if event.description:
        lines.extend(["", "Description", event.description])
    return "\n".join(lines).strip()


def _parse_ics(text: str) -> Tuple[List[_ParsedEvent], Dict[str, List[_ParsedEvent]]]:
    events: List[_ParsedEvent] = []
    stack: List[str] = []
    current: Optional[dict] = None

    for line in _unfold(text):
        if not line.strip() or line.startswith("\x00"):
            continue
        upper = line.strip().upper()
        if upper.startswith("BEGIN:"):
            name = upper[6:].strip()
            stack.append(name)
            if name == "VEVENT":
                current = {"props": []}
            continue
        if upper.startswith("END:"):
            name = upper[4:].strip()
            if stack and stack[-1] == name:
                stack.pop()
            if name == "VEVENT" and current is not None:
                parsed = _vevent_from_props(current["props"])
                if parsed is not None:
                    events.append(parsed)
                current = None
            continue
        if not stack:
            continue
        if stack[-1] != "VEVENT" or current is None:
            continue
        parsed_prop = _parse_prop(line)
        if parsed_prop is None:
            continue
        current["props"].append(parsed_prop)

    masters: List[_ParsedEvent] = []
    exceptions: Dict[str, List[_ParsedEvent]] = {}
    for ev in events:
        if ev.recurrence_id is not None:
            exceptions.setdefault(ev.uid, []).append(ev)
        else:
            masters.append(ev)
    return masters, exceptions


def _vevent_from_props(props: List[Tuple[str, Dict[str, str], str]]) -> Optional[_ParsedEvent]:
    fields: Dict[str, Tuple[Dict[str, str], str]] = {}
    exdates: List[Tuple[Dict[str, str], str]] = []
    rdates: List[Tuple[Dict[str, str], str]] = []
    status = ""
    transp = ""
    busy = ""
    uid = ""
    title = ""
    location = ""
    description = ""
    organizer = ""
    rrule = None
    recurrence_raw: Optional[Tuple[Dict[str, str], str]] = None

    for name, params, value in props:
        if name == "UID":
            uid = value.strip()
        elif name == "SUMMARY":
            title = _unescape(value).strip()
        elif name == "LOCATION":
            location = _unescape(value).strip()
        elif name == "DESCRIPTION":
            description = _plain_description(value)
        elif name == "ORGANIZER":
            organizer = _organizer_label(params, value)
        elif name == "RRULE":
            rrule = value.strip()
        elif name == "EXDATE":
            exdates.append((params, value))
        elif name == "RDATE":
            rdates.append((params, value))
        elif name == "RECURRENCE-ID":
            recurrence_raw = (params, value)
        elif name == "STATUS":
            status = value.strip().upper()
        elif name == "TRANSP":
            transp = value.strip().upper()
        elif name == "X-MICROSOFT-CDO-BUSYSTATUS":
            busy = value.strip().upper()
        elif name in ("DTSTART", "DTEND", "DURATION"):
            fields[name] = (params, value)

    if "DTSTART" not in fields:
        return None
    start, all_day = _parse_dt(fields["DTSTART"][1], fields["DTSTART"][0])
    if start is None:
        return None
    end = None
    if "DTEND" in fields:
        end, end_all_day = _parse_dt(fields["DTEND"][1], fields["DTEND"][0])
        all_day = all_day or end_all_day
        if end is not None and all_day:
            # RFC 5545 all-day DTEND is exclusive.
            end = end - timedelta(days=1)
            if end < start:
                end = start
            end = datetime.combine(end.date(), datetime.min.time()) + timedelta(days=1)
    elif "DURATION" in fields:
        end = start + _parse_duration(fields["DURATION"][1])
    if end is None:
        end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
    if end <= start:
        return None

    cancelled = status == "CANCELLED"
    skip = (not cancelled) and (
        transp == "TRANSPARENT" or busy == "FREE"
    )
    recurrence_id = None
    if recurrence_raw is not None:
        recurrence_id, _ = _parse_dt(recurrence_raw[1], recurrence_raw[0])

    parsed_exdates: List[datetime] = []
    for params, value in exdates:
        for piece in value.split(","):
            dt, _ = _parse_dt(piece.strip(), params)
            if dt is not None:
                parsed_exdates.append(dt)
    parsed_rdates: List[datetime] = []
    for params, value in rdates:
        for piece in value.split(","):
            dt, _ = _parse_dt(piece.strip(), params)
            if dt is not None:
                parsed_rdates.append(dt)

    if not uid:
        uid = f"{start.isoformat()}|{title}"
    if not title:
        title = "(busy)" if busy else "(no title)"

    return _ParsedEvent(
        uid=uid,
        title=title,
        start=start,
        end=end,
        all_day=all_day,
        rrule=rrule,
        rdates=parsed_rdates,
        exdates=parsed_exdates,
        recurrence_id=recurrence_id,
        cancelled=cancelled,
        skip=skip,
        location=location,
        description=description,
        organizer=organizer,
    )


def _parse_dt(value: str, params: Dict[str, str]) -> Tuple[Optional[datetime], bool]:
    raw = (value or "").strip()
    if not raw:
        return None, False
    tzid = (params.get("TZID") or "").strip()
    is_date = params.get("VALUE", "").upper() == "DATE" or ("T" not in raw and len(raw) >= 8)
    try:
        if is_date:
            d = datetime.strptime(raw[:8], "%Y%m%d")
            return d, True
        compact = raw.replace("-", "").replace(":", "")
        utc = compact.endswith("Z")
        compact = compact[:-1] if utc else compact
        if len(compact) >= 15:
            naive = datetime.strptime(compact[:15], "%Y%m%dT%H%M%S")
        elif len(compact) >= 13:
            naive = datetime.strptime(compact[:13], "%Y%m%dT%H%M")
        else:
            return None, False
    except ValueError:
        return None, False
    if utc:
        aware = naive.replace(tzinfo=timezone.utc)
        return aware.astimezone(_local_tz()).replace(tzinfo=None), False
    tz = _resolve_tz(tzid) if tzid else None
    if tz is not None:
        try:
            aware = naive.replace(tzinfo=tz)
            return aware.astimezone(_local_tz()).replace(tzinfo=None), False
        except Exception:
            pass
    return naive, False


def _local_tz():
    return datetime.now().astimezone().tzinfo or timezone.utc


def _resolve_tz(tzid: str):
    if ZoneInfo is None or not tzid:
        return None
    name = tzid.strip().strip('"')
    if name.startswith("/"):
        parts = [p for p in name.split("/") if p]
        if len(parts) >= 2:
            name = "/".join(parts[-2:])
    iana = _WINDOWS_TZ.get(name, name)
    try:
        return ZoneInfo(iana)
    except Exception:
        return None


def _parse_duration(value: str) -> timedelta:
    text = (value or "").strip().upper()
    sign = -1 if text.startswith("-") else 1
    if text.startswith(("+", "-")):
        text = text[1:]
    match = _DURATION_RE.match(text)
    if not match:
        return timedelta(hours=1)
    weeks, days, hours, minutes, seconds = match.groups()
    return sign * timedelta(
        weeks=int(weeks or 0),
        days=int(days or 0),
        hours=int(hours or 0),
        minutes=int(minutes or 0),
        seconds=float(seconds or 0),
    )


# ---------------------------------------------------------------------------
# Recurrence
# ---------------------------------------------------------------------------
def _expand_rrule(event: _ParsedEvent, range_start: date, range_end: date) -> List[Tuple[datetime, datetime]]:
    rule = _parse_rrule(event.rrule or "")
    freq = (rule.get("FREQ") or "").upper()
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        if range_start <= event.start.date() <= range_end:
            return [(event.start, event.end)]
        return []
    interval = max(1, _int_rule(rule.get("INTERVAL"), 1))
    count = _int_rule(rule.get("COUNT"), 0)
    until = _parse_until(rule.get("UNTIL"))
    byday = _parse_byday(rule.get("BYDAY"))
    bymonthday = _parse_int_list(rule.get("BYMONTHDAY"))
    wkst = _WEEKDAYS.get((rule.get("WKST") or "MO").upper(), 0)
    duration = event.end - event.start
    candidates = _rrule_candidate_starts(
        event.start, freq, interval, byday, bymonthday, wkst, range_end, until, count)
    return _clip_occurrences(candidates, duration, range_start, range_end)


def _rrule_candidate_starts(
        dtstart: datetime, freq: str, interval: int,
        byday: List[Tuple[int, int]], bymonthday: List[int],
        wkst: int, range_end: date, until: Optional[datetime],
        count: int) -> List[datetime]:
    """Walk the series from DTSTART. COUNT/UNTIL apply from the beginning
    so a meeting that started years ago still lands on this week."""
    starts: List[datetime] = []
    seen = set()

    def take(dt: datetime) -> bool:
        if dt < dtstart:
            return True
        if until is not None and dt > until:
            return False
        key = (dt.year, dt.month, dt.day, dt.hour, dt.minute)
        if key in seen:
            return True
        seen.add(key)
        starts.append(dt)
        if count and len(starts) >= count:
            return False
        if not count and dt.date() > range_end:
            return False
        return True

    if not take(dtstart):
        return starts

    if freq == "DAILY":
        weekdays = {wd for _nth, wd in byday} if byday else None
        i = 1
        while i < _MAX_STEPS:
            dt = dtstart + timedelta(days=i * interval)
            i += 1
            if weekdays is not None and dt.weekday() not in weekdays:
                continue
            if not take(dt):
                break
    elif freq == "WEEKLY":
        weekdays = [wd for _nth, wd in byday] if byday else [dtstart.weekday()]
        anchor0 = dtstart.date() - timedelta(days=(dtstart.weekday() - wkst) % 7)
        week_i = 0
        while week_i < _MAX_STEPS:
            week_anchor = anchor0 + timedelta(weeks=week_i * interval)
            week_i += 1
            if week_anchor > range_end + timedelta(days=7) and (not count or len(starts) >= count):
                break
            stop = False
            for wd in sorted(set(weekdays)):
                inst_date = week_anchor + timedelta(days=(wd - wkst) % 7)
                dt = datetime.combine(inst_date, dtstart.time())
                if not take(dt):
                    stop = True
                    break
            if stop:
                break
    elif freq == "MONTHLY":
        month_i = 0
        while month_i < _MAX_STEPS:
            y, m = _add_months(dtstart.year, dtstart.month, month_i * interval)
            month_i += 1
            if date(y, m, 1) > range_end + timedelta(days=32) and (not count or len(starts) >= count):
                break
            stop = False
            for inst_date in _monthly_days(y, m, dtstart, byday, bymonthday):
                dt = datetime.combine(inst_date, dtstart.time())
                if not take(dt):
                    stop = True
                    break
            if stop:
                break
    elif freq == "YEARLY":
        year_i = 0
        while year_i < _MAX_STEPS:
            y = dtstart.year + year_i * interval
            year_i += 1
            if y > range_end.year + 1 and (not count or len(starts) >= count):
                break
            try:
                inst_date = date(y, dtstart.month, dtstart.day)
            except ValueError:
                continue
            dt = datetime.combine(inst_date, dtstart.time())
            if not take(dt):
                break
    return starts


def _clip_occurrences(starts: List[datetime], duration: timedelta,
                      range_start: date, range_end: date) -> List[Tuple[datetime, datetime]]:
    out = []
    for start in starts:
        end = start + duration
        if end.date() < range_start or start.date() > range_end:
            continue
        out.append((start, end))
    return out


def _parse_rrule(raw: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for piece in (raw or "").split(";"):
        if "=" not in piece:
            continue
        key, val = piece.split("=", 1)
        result[key.strip().upper()] = val.strip()
    return result


def _int_rule(raw: Optional[str], default: int) -> int:
    try:
        return int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _parse_until(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    dt, all_day = _parse_dt(raw, {"VALUE": "DATE"} if "T" not in raw else {})
    if dt is None:
        return None
    if all_day:
        return datetime.combine(dt.date(), datetime.max.time().replace(microsecond=0))
    return dt


def _parse_byday(raw: Optional[str]) -> List[Tuple[int, int]]:
    """Return [(nth, weekday)] where nth=0 means every, weekday Mon=0."""
    if not raw:
        return []
    out: List[Tuple[int, int]] = []
    for piece in raw.split(","):
        token = piece.strip().upper()
        if len(token) < 2:
            continue
        wd = _WEEKDAYS.get(token[-2:])
        if wd is None:
            continue
        nth_s = token[:-2]
        nth = int(nth_s) if nth_s else 0
        out.append((nth, wd))
    return out


def _parse_int_list(raw: Optional[str]) -> List[int]:
    if not raw:
        return []
    out = []
    for piece in raw.split(","):
        try:
            out.append(int(piece.strip()))
        except ValueError:
            continue
    return out


def _add_months(year: int, month: int, delta: int) -> Tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def _monthly_days(year: int, month: int, dtstart: datetime,
                  byday: List[Tuple[int, int]], bymonthday: List[int]) -> List[date]:
    last = monthrange(year, month)[1]
    days: List[date] = []
    if bymonthday:
        for raw in bymonthday:
            day = last + 1 + raw if raw < 0 else raw
            if 1 <= day <= last:
                days.append(date(year, month, day))
    elif byday:
        for nth, wd in byday:
            if nth == 0:
                d = date(year, month, 1)
                while d.month == month:
                    if d.weekday() == wd:
                        days.append(d)
                    d += timedelta(days=1)
                continue
            found = _nth_weekday(year, month, wd, nth)
            if found is not None:
                days.append(found)
    else:
        day = min(dtstart.day, last)
        days.append(date(year, month, day))
    return days


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> Optional[date]:
    last = monthrange(year, month)[1]
    if nth > 0:
        first = date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        day = 1 + offset + (nth - 1) * 7
        if day > last:
            return None
        return date(year, month, day)
    if nth < 0:
        last_d = date(year, month, last)
        offset = (last_d.weekday() - weekday) % 7
        day = last - offset + (nth + 1) * 7
        if day < 1:
            return None
        return date(year, month, day)
    return None


def _flatten(dt: Optional[datetime]) -> Optional[Tuple[int, int, int, int, int]]:
    if dt is None:
        return None
    return (dt.year, dt.month, dt.day, dt.hour, dt.minute)


def _hhmm(dt: datetime) -> str:
    return f"{dt.hour:02d}:{dt.minute:02d}"


def _split_days(uid: str, title: str, start: datetime, end: datetime,
                range_start: date, range_end: date, location: str = "",
                description: str = "", organizer: str = "") -> List[CalendarEvent]:
    if end <= start:
        return []
    extra = dict(location=location or "", description=description or "",
                 organizer=organizer or "")
    out: List[CalendarEvent] = []
    cursor = start
    while cursor.date() < end.date():
        day_end = datetime.combine(cursor.date() + timedelta(days=1), datetime.min.time())
        if range_start <= cursor.date() <= range_end:
            out.append(CalendarEvent(
                uid=f"{uid}:{cursor.date().isoformat()}",
                title=title,
                date=cursor.date().isoformat(),
                start_time=_hhmm(cursor),
                end_time="24:00",
                **extra,
            ))
        cursor = day_end
        if cursor >= end:
            break
    if cursor < end and range_start <= cursor.date() <= range_end:
        end_label = "24:00" if end.hour == 0 and end.minute == 0 and end.date() != cursor.date() else _hhmm(end)
        if end_label != _hhmm(cursor):
            out.append(CalendarEvent(
                uid=f"{uid}:{cursor.date().isoformat()}",
                title=title,
                date=cursor.date().isoformat(),
                start_time=_hhmm(cursor),
                end_time=end_label,
                **extra,
            ))
    return out


def _dedupe_events(events: List[CalendarEvent]) -> List[CalendarEvent]:
    seen = set()
    out = []
    for ev in events:
        key = (ev.date, ev.start_time, ev.end_time, ev.title)
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out
