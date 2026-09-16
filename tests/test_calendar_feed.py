"""ICS overlay parsing -- no Tk window, no network."""
import os
import sys
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.calendar_feed import (
    CalendarEvent, dump_ics_url_list, format_meeting_details, normalize_ics_url,
    parse_ics, parse_ics_url_list, saved_ics_urls,
)


def _ics(*vevents: str) -> str:
    body = "\r\n".join(vevents)
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Test\r\n"
        f"{body}\r\n"
        "END:VCALENDAR\r\n"
    )


def _titles(events):
    return [(e.date, e.start_time, e.end_time, e.title) for e in events]


class TestNormalizeIcsUrl(unittest.TestCase):
    def test_webcal_becomes_https(self):
        self.assertEqual(
            normalize_ics_url("webcal://outlook.office365.com/owa/calendar/abc/calendar.ics"),
            "https://outlook.office365.com/owa/calendar/abc/calendar.ics",
        )

    def test_bare_host_gets_https(self):
        self.assertEqual(
            normalize_ics_url("outlook.office.com/calendar.ics"),
            "https://outlook.office.com/calendar.ics",
        )

    def test_blank_stays_blank(self):
        self.assertEqual(normalize_ics_url("  "), "")


class TestParseIcs(unittest.TestCase):
    def test_one_off_meeting(self):
        feed = parse_ics(_ics(
            "BEGIN:VEVENT\r\n"
            "UID:one\r\n"
            "DTSTART:20260916T100000\r\n"
            "DTEND:20260916T103000\r\n"
            "SUMMARY:Standup\r\n"
            "END:VEVENT"
        ))
        events = feed.events_between(date(2026, 9, 14), date(2026, 9, 18))
        self.assertEqual(_titles(events), [
            ("2026-09-16", "10:00", "10:30", "Standup"),
        ])

    def test_outside_the_week_is_ignored(self):
        feed = parse_ics(_ics(
            "BEGIN:VEVENT\r\n"
            "UID:one\r\n"
            "DTSTART:20260916T100000\r\n"
            "DTEND:20260916T103000\r\n"
            "SUMMARY:Standup\r\n"
            "END:VEVENT"
        ))
        self.assertEqual(feed.events_between(date(2026, 9, 21), date(2026, 9, 25)), [])

    def test_all_day_events_are_not_timed_guides(self):
        feed = parse_ics(_ics(
            "BEGIN:VEVENT\r\n"
            "UID:ooo\r\n"
            "DTSTART;VALUE=DATE:20260916\r\n"
            "DTEND;VALUE=DATE:20260917\r\n"
            "SUMMARY:Out of office\r\n"
            "END:VEVENT"
        ))
        self.assertEqual(feed.events_between(date(2026, 9, 14), date(2026, 9, 18)), [])

    def test_cancelled_and_free_are_skipped(self):
        feed = parse_ics(_ics(
            "BEGIN:VEVENT\r\n"
            "UID:c\r\n"
            "DTSTART:20260916T100000\r\n"
            "DTEND:20260916T110000\r\n"
            "SUMMARY:Cancelled\r\n"
            "STATUS:CANCELLED\r\n"
            "END:VEVENT\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:f\r\n"
            "DTSTART:20260916T120000\r\n"
            "DTEND:20260916T130000\r\n"
            "SUMMARY:Free\r\n"
            "X-MICROSOFT-CDO-BUSYSTATUS:FREE\r\n"
            "END:VEVENT\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:t\r\n"
            "DTSTART:20260916T140000\r\n"
            "DTEND:20260916T150000\r\n"
            "SUMMARY:Tentative\r\n"
            "X-MICROSOFT-CDO-BUSYSTATUS:TENTATIVE\r\n"
            "END:VEVENT"
        ))
        events = feed.events_between(date(2026, 9, 16), date(2026, 9, 16))
        self.assertEqual([e.title for e in events], ["Tentative"])

    def test_folded_summary_line(self):
        feed = parse_ics(_ics(
            "BEGIN:VEVENT\r\n"
            "UID:fold\r\n"
            "DTSTART:20260916T090000\r\n"
            "DTEND:20260916T100000\r\n"
            "SUMMARY:Very long meeting name that Outlook\r\n"
            "  wraps onto a second line\r\n"
            "END:VEVENT"
        ))
        events = feed.events_between(date(2026, 9, 16), date(2026, 9, 16))
        self.assertEqual(events[0].title, "Very long meeting name that Outlook wraps onto a second line")

    def test_duration_without_dtend(self):
        feed = parse_ics(_ics(
            "BEGIN:VEVENT\r\n"
            "UID:dur\r\n"
            "DTSTART:20260916T090000\r\n"
            "DURATION:PT1H30M\r\n"
            "SUMMARY:Workshop\r\n"
            "END:VEVENT"
        ))
        events = feed.events_between(date(2026, 9, 16), date(2026, 9, 16))
        self.assertEqual(_titles(events), [
            ("2026-09-16", "09:00", "10:30", "Workshop"),
        ])

    def test_escaped_summary_comma(self):
        feed = parse_ics(_ics(
            "BEGIN:VEVENT\r\n"
            "UID:esc\r\n"
            "DTSTART:20260916T090000\r\n"
            "DTEND:20260916T093000\r\n"
            "SUMMARY:Design\\, review\r\n"
            "END:VEVENT"
        ))
        self.assertEqual(
            feed.events_between(date(2026, 9, 16), date(2026, 9, 16))[0].title,
            "Design, review",
        )

    def test_location_organizer_and_description(self):
        feed = parse_ics(_ics(
            "BEGIN:VEVENT\r\n"
            "UID:meta\r\n"
            "DTSTART:20260916T080000\r\n"
            "DTEND:20260916T090000\r\n"
            "SUMMARY:Daily Catch up - SM Leads\r\n"
            "LOCATION:Microsoft Teams Meeting\r\n"
            "ORGANIZER;CN=Alex Rae:mailto:alex@example.com\r\n"
            "DESCRIPTION:Join on Teams\\nBring the board\r\n"
            "END:VEVENT"
        ))
        event = feed.events_between(date(2026, 9, 16), date(2026, 9, 16))[0]
        self.assertEqual(event.location, "Microsoft Teams Meeting")
        self.assertEqual(event.organizer, "Alex Rae")
        self.assertIn("Join on Teams", event.description)
        self.assertIn("Bring the board", event.description)
        details = format_meeting_details(event)
        self.assertIn("Daily Catch up - SM Leads", details)
        self.assertIn("Location", details)
        self.assertIn("Organizer", details)
        self.assertIn("Description", details)

    def test_html_description_is_stripped(self):
        feed = parse_ics(_ics(
            "BEGIN:VEVENT\r\n"
            "UID:html\r\n"
            "DTSTART:20260916T080000\r\n"
            "DTEND:20260916T090000\r\n"
            "SUMMARY:Catch up\r\n"
            "DESCRIPTION:<html><body>Hello<br>World</body></html>\r\n"
            "END:VEVENT"
        ))
        event = feed.events_between(date(2026, 9, 16), date(2026, 9, 16))[0]
        self.assertIn("Hello", event.description)
        self.assertIn("World", event.description)
        self.assertNotIn("<br>", event.description)


class TestRecurrence(unittest.TestCase):
    def test_weekly_byday(self):
        feed = parse_ics(_ics(
            "BEGIN:VEVENT\r\n"
            "UID:weekly\r\n"
            "DTSTART:20260914T093000\r\n"
            "DTEND:20260914T100000\r\n"
            "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR;INTERVAL=1\r\n"
            "SUMMARY:Standup\r\n"
            "END:VEVENT"
        ))
        events = feed.events_between(date(2026, 9, 14), date(2026, 9, 18))
        self.assertEqual([e.date for e in events], [
            "2026-09-14", "2026-09-16", "2026-09-18",
        ])
        self.assertTrue(all(e.start_time == "09:30" and e.end_time == "10:00" for e in events))

    def test_exdate_removes_one_instance(self):
        feed = parse_ics(_ics(
            "BEGIN:VEVENT\r\n"
            "UID:weekly\r\n"
            "DTSTART:20260914T093000\r\n"
            "DTEND:20260914T100000\r\n"
            "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR\r\n"
            "EXDATE:20260916T093000\r\n"
            "SUMMARY:Standup\r\n"
            "END:VEVENT"
        ))
        events = feed.events_between(date(2026, 9, 14), date(2026, 9, 18))
        self.assertEqual([e.date for e in events], ["2026-09-14", "2026-09-18"])

    def test_moved_instance_uses_exception_times(self):
        feed = parse_ics(_ics(
            "BEGIN:VEVENT\r\n"
            "UID:weekly\r\n"
            "DTSTART:20260914T093000\r\n"
            "DTEND:20260914T100000\r\n"
            "RRULE:FREQ=WEEKLY;BYDAY=MO\r\n"
            "SUMMARY:1:1\r\n"
            "END:VEVENT\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:weekly\r\n"
            "RECURRENCE-ID:20260921T093000\r\n"
            "DTSTART:20260921T140000\r\n"
            "DTEND:20260921T150000\r\n"
            "SUMMARY:1:1 (moved)\r\n"
            "END:VEVENT"
        ))
        events = feed.events_between(date(2026, 9, 21), date(2026, 9, 21))
        self.assertEqual(_titles(events), [
            ("2026-09-21", "14:00", "15:00", "1:1 (moved)"),
        ])

    def test_until_stops_the_series(self):
        feed = parse_ics(_ics(
            "BEGIN:VEVENT\r\n"
            "UID:until\r\n"
            "DTSTART:20260914T110000\r\n"
            "DTEND:20260914T113000\r\n"
            "RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20260914T110000\r\n"
            "SUMMARY:Once\r\n"
            "END:VEVENT"
        ))
        week = feed.events_between(date(2026, 9, 14), date(2026, 9, 18))
        next_week = feed.events_between(date(2026, 9, 21), date(2026, 9, 25))
        self.assertEqual(len(week), 1)
        self.assertEqual(next_week, [])

    def test_count_limits_occurrences(self):
        feed = parse_ics(_ics(
            "BEGIN:VEVENT\r\n"
            "UID:count\r\n"
            "DTSTART:20260914T080000\r\n"
            "DTEND:20260914T081500\r\n"
            "RRULE:FREQ=DAILY;COUNT=3\r\n"
            "SUMMARY:Burst\r\n"
            "END:VEVENT"
        ))
        events = feed.events_between(date(2026, 9, 14), date(2026, 9, 20))
        self.assertEqual([e.date for e in events], [
            "2026-09-14", "2026-09-15", "2026-09-16",
        ])

    def test_monthly_nth_weekday(self):
        feed = parse_ics(_ics(
            "BEGIN:VEVENT\r\n"
            "UID:month\r\n"
            "DTSTART:20260908T160000\r\n"
            "DTEND:20260908T170000\r\n"
            "RRULE:FREQ=MONTHLY;BYDAY=2TU\r\n"
            "SUMMARY:Board\r\n"
            "END:VEVENT"
        ))
        sept = feed.events_between(date(2026, 9, 8), date(2026, 9, 8))
        octb = feed.events_between(date(2026, 10, 13), date(2026, 10, 13))
        self.assertEqual(sept[0].title, "Board")
        self.assertEqual(octb[0].date, "2026-10-13")  # 2nd Tuesday of Oct 2026

    def test_long_running_weekly_still_lands_this_week(self):
        feed = parse_ics(_ics(
            "BEGIN:VEVENT\r\n"
            "UID:old\r\n"
            "DTSTART:20200106T093000\r\n"
            "DTEND:20200106T100000\r\n"
            "RRULE:FREQ=WEEKLY;BYDAY=MO\r\n"
            "SUMMARY:Ancient standup\r\n"
            "END:VEVENT"
        ))
        events = feed.events_between(date(2026, 9, 14), date(2026, 9, 14))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].start_time, "09:30")


class TestMultipleCalendars(unittest.TestCase):
    def test_json_roundtrip_dedupes_and_normalizes(self):
        raw = dump_ics_url_list([
            "webcal://a.example/x.ics",
            "https://b.example/y.ics",
            "https://a.example/x.ics",
            "  ",
        ])
        self.assertEqual(parse_ics_url_list(raw), [
            "https://a.example/x.ics",
            "https://b.example/y.ics",
        ])

    def test_legacy_single_setting_still_loads(self):
        def get_setting(key, default=""):
            return {
                "calendar_ics_url": "https://outlook.office.com/one.ics",
            }.get(key, default)
        self.assertEqual(
            saved_ics_urls(get_setting),
            ["https://outlook.office.com/one.ics"],
        )

    def test_list_setting_wins_over_legacy_single(self):
        def get_setting(key, default=""):
            return {
                "calendar_ics_urls": '["https://a.example/1.ics","https://b.example/2.ics"]',
                "calendar_ics_url": "https://old.example/x.ics",
            }.get(key, default)
        self.assertEqual(saved_ics_urls(get_setting), [
            "https://a.example/1.ics",
            "https://b.example/2.ics",
        ])

    def test_merges_two_feeds_onto_the_same_week(self):
        a = parse_ics(_ics(
            "BEGIN:VEVENT\r\n"
            "UID:a\r\n"
            "DTSTART:20260916T100000\r\n"
            "DTEND:20260916T103000\r\n"
            "SUMMARY:Standup\r\n"
            "END:VEVENT"
        ))
        b = parse_ics(_ics(
            "BEGIN:VEVENT\r\n"
            "UID:b\r\n"
            "DTSTART:20260916T140000\r\n"
            "DTEND:20260916T150000\r\n"
            "SUMMARY:1:1\r\n"
            "END:VEVENT"
        ))
        a.extend(b, source_id="other")
        events = a.events_between(date(2026, 9, 16), date(2026, 9, 16))
        self.assertEqual([e.title for e in events], ["Standup", "1:1"])


class TestHoursShape(unittest.TestCase):
    def test_calendar_event_matches_time_entry_hours_fields(self):
        event = CalendarEvent("u", "A", "2026-09-16", "08:00", "08:30")
        self.assertEqual(event.start_time, "08:00")
        self.assertEqual(event.end_time, "08:30")
        self.assertFalse(event.all_day)


if __name__ == "__main__":
    unittest.main()
