"""Tests for Jira credential merge, issue grouping, DB sync, and worklog push.

HTTP is mocked -- these never hit a real Jira. The UI wiring (Settings token
field, Sync/Push buttons) is covered separately by the existing smoke tests
still constructing MainWindow.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, jira_client, jira_sync
from app.db import Database
from app.models import Activity, Project, TimeEntry


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = json.dumps(payload).encode("utf-8") if not isinstance(payload, bytes) else payload
        self.status = status

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestCredentials(unittest.TestCase):
    def test_env_overrides_saved_settings(self):
        saved = {
            "jira_site_url": "https://saved.example",
            "jira_email": "saved@example.com",
            "jira_api_token": "saved-token",
            "jira_project_key": "OLD",
        }
        env = {
            "QUASAR_JIRA_URL": "https://env.example",
            "QUASAR_JIRA_EMAIL": "env@example.com",
            "QUASAR_JIRA_TOKEN": "env-token",
            "QUASAR_JIRA_PROJECT": "QDM",
        }
        with patch.dict(os.environ, env, clear=False):
            creds = jira_client.credentials_from_settings(lambda k, d=None: saved.get(k, d))
        self.assertEqual(creds.site_url, "https://env.example")
        self.assertEqual(creds.email, "env@example.com")
        self.assertEqual(creds.api_token, "env-token")
        self.assertEqual(creds.project_key, "QDM")

    def test_incomplete_without_token(self):
        creds = jira_client.JiraCredentials("https://x.atlassian.net", "a@b.c", "")
        self.assertFalse(creds.is_complete())
        creds.api_token = "tok"
        self.assertTrue(creds.is_complete())


class TestNormalizeSiteUrl(unittest.TestCase):
    def test_cloud_dashboard_strips_to_host(self):
        self.assertEqual(
            jira_client.normalize_site_url(
                "https://acme.atlassian.net/jira/dashboards/10000"),
            "https://acme.atlassian.net",
        )

    def test_cloud_your_work_and_board_links(self):
        self.assertEqual(
            jira_client.normalize_site_url(
                "https://acme.atlassian.net/jira/your-work"),
            "https://acme.atlassian.net",
        )
        self.assertEqual(
            jira_client.normalize_site_url(
                "https://acme.atlassian.net/jira/software/c/projects/QDM/boards/12"),
            "https://acme.atlassian.net",
        )

    def test_already_clean_host_unchanged(self):
        self.assertEqual(
            jira_client.normalize_site_url("https://acme.atlassian.net/"),
            "https://acme.atlassian.net",
        )

    def test_adds_https_when_scheme_missing(self):
        self.assertEqual(
            jira_client.normalize_site_url("acme.atlassian.net/jira/dashboards/1"),
            "https://acme.atlassian.net",
        )

    def test_server_dashboard_keeps_optional_jira_context(self):
        self.assertEqual(
            jira_client.normalize_site_url(
                "https://jira.example.com/jira/secure/Dashboard.jspa"),
            "https://jira.example.com/jira",
        )


class TestWorklogStarted(unittest.TestCase):
    def test_format_has_timezone_offset(self):
        stamp = jira_client.worklog_started("2026-07-24", "09:30")
        self.assertTrue(stamp.startswith("2026-07-24T09:30:00.000"))
        self.assertRegex(stamp, r"[+-]\d{4}$")


class TestGroupName(unittest.TestCase):
    def test_parent_wins(self):
        issue = jira_client.JiraIssue(
            "QDM-1", "Implement login", "Sub-task", "QDM",
            "Quasar Delivery Management", "In Progress",
            parent_key="QDM-10", parent_summary="Sprint 14")
        self.assertEqual(jira_sync.group_name_for_issue(issue), "Sprint 14")

    def test_falls_back_to_issue_type(self):
        issue = jira_client.JiraIssue(
            "QDM-2", "Fix crash", "Bug", "QDM",
            "Quasar Delivery Management", "To Do")
        self.assertEqual(jira_sync.group_name_for_issue(issue), "Bug")


class TestSyncIssues(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.tmpdir, "test.db"))

    def tearDown(self):
        self.db.close()

    def test_creates_project_and_activity_from_issue(self):
        issues = [
            jira_client.JiraIssue(
                "QDM-42", "Write timesheet sync", "Story", "QDM",
                "Quasar Delivery Management", "To Do",
                parent_key="QDM-1", parent_summary="Platform"),
        ]
        result = jira_sync.sync_issues_into_db(self.db, issues)
        self.assertEqual(result.fetched, 1)
        self.assertEqual(result.created, 1)
        self.assertEqual(result.projects_created, 1)
        act = self.db.get_activity_by_jira_key("QDM-42")
        self.assertIsNotNone(act)
        self.assertEqual(act.name, "Write timesheet sync")
        self.assertEqual(act.jira_status, "To Do")
        self.assertFalse(act.is_closed())
        project = self.db.get_project(act.project_id)
        self.assertEqual(project.name, "Platform")
        self.assertTrue(project.collapsed)

    def test_second_sync_updates_name_not_duplicate(self):
        issue = jira_client.JiraIssue(
            "QDM-42", "Old name", "Story", "QDM",
            "Quasar Delivery Management", "To Do")
        jira_sync.sync_issues_into_db(self.db, [issue])
        issue.summary = "New name"
        result = jira_sync.sync_issues_into_db(self.db, [issue])
        self.assertEqual(result.created, 0)
        self.assertEqual(result.updated, 1)
        matching = [a for a in self.db.list_activities() if a.jira_key == "QDM-42"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].name, "New name")

    def test_sync_stores_closed_status(self):
        issue = jira_client.JiraIssue(
            "QDM-9", "Shipped it", "Story", "QDM",
            "Quasar Delivery Management", "Done",
            status_category="done")
        jira_sync.sync_issues_into_db(self.db, [issue])
        act = self.db.get_activity_by_jira_key("QDM-9")
        self.assertEqual(act.jira_status, "Done")
        self.assertEqual(act.jira_status_category, "done")
        self.assertTrue(act.is_closed())

    def test_archives_qdm_keys_missing_from_later_fetch(self):
        stale = jira_client.JiraIssue(
            "QDM-1", "Ancient ticket", "Story", "QDM",
            "Quasar Delivery Management", "To Do")
        keep = jira_client.JiraIssue(
            "QDM-2", "Still assigned", "Story", "QDM",
            "Quasar Delivery Management", "To Do")
        jira_sync.sync_issues_into_db(self.db, [stale, keep])
        jira_sync.sync_issues_into_db(self.db, [keep])
        self.assertTrue(self.db.get_activity_by_jira_key("QDM-1").archived)
        self.assertFalse(self.db.get_activity_by_jira_key("QDM-2").archived)
        visible_qdm = [a.jira_key for a in self.db.list_activities()
                       if (a.jira_key or "").upper().startswith("QDM-")]
        self.assertEqual(visible_qdm, ["QDM-2"])

    def test_capped_open_fetch_does_not_archive_other_open_tickets(self):
        stale = jira_client.JiraIssue(
            "QDM-1", "Still assigned but not in this page", "Story", "QDM",
            "Quasar Delivery Management", "To Do")
        keep = jira_client.JiraIssue(
            "QDM-2", "On this page", "Story", "QDM",
            "Quasar Delivery Management", "To Do")
        jira_sync.sync_issues_into_db(self.db, [stale, keep])
        jira_sync.sync_issues_into_db(
            self.db, [keep], archive_open_missing=False, open_capped=True)
        self.assertFalse(self.db.get_activity_by_jira_key("QDM-1").archived)
        self.assertFalse(self.db.get_activity_by_jira_key("QDM-2").archived)

    def test_empty_fetch_does_not_archive_existing(self):
        issue = jira_client.JiraIssue(
            "QDM-42", "Keep me", "Story", "QDM",
            "Quasar Delivery Management", "To Do")
        jira_sync.sync_issues_into_db(self.db, [issue])
        jira_sync.sync_issues_into_db(self.db, [])
        self.assertFalse(self.db.get_activity_by_jira_key("QDM-42").archived)


class TestAssignedIssuesJql(unittest.TestCase):
    def test_open_jql_is_assigned_and_not_done(self):
        jql = jira_client.assigned_open_jql("QDM")
        self.assertIn("assignee = currentUser()", jql)
        self.assertIn("project = QDM", jql)
        self.assertIn("statusCategory != Done", jql)
        self.assertNotIn("updated >=", jql)

    def test_closed_jql_is_assigned_done_in_window(self):
        jql = jira_client.assigned_closed_jql("QDM", today=date(2026, 9, 14))
        self.assertIn("assignee = currentUser()", jql)
        self.assertIn("statusCategory = Done", jql)
        self.assertIn('updated >= "2025-01-01"', jql)

    def test_previous_calendar_year_includes_closed(self):
        jql = jira_client.assigned_issues_jql("QDM", today=date(2026, 9, 14))
        self.assertIn("assignee = currentUser()", jql)
        self.assertIn("project = QDM", jql)
        self.assertIn('updated >= "2025-01-01"', jql)
        self.assertNotIn("resolution IS EMPTY", jql)

    def test_issue_parses_status_category(self):
        issue = jira_client._issue_from_payload({
            "key": "QDM-1",
            "fields": {
                "summary": "Done ticket",
                "issuetype": {"name": "Story"},
                "project": {"key": "QDM", "name": "QDM"},
                "status": {
                    "name": "Closed",
                    "statusCategory": {"key": "done", "name": "Done"},
                },
            },
        })
        self.assertEqual(issue.status, "Closed")
        self.assertEqual(issue.status_category, "done")
        self.assertTrue(Activity(
            None, "x", jira_status=issue.status,
            jira_status_category=issue.status_category).is_closed())

    def test_is_closed_from_status_name_without_category(self):
        self.assertTrue(Activity(None, "x", jira_status="Resolved").is_closed())
        self.assertFalse(Activity(None, "x", jira_status="In Progress").is_closed())


class TestPushWorklogs(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.tmpdir, "test.db"))
        pid = self.db.add_project(Project(None, "Platform", "#4C6EF5"))
        aid = self.db.add_activity(Activity(
            None, "Write sync", "QDM-42", project_id=pid))
        self.entry_id = self.db.add_time_entry(TimeEntry(
            None, aid, "Write sync", "QDM-42", "#4C6EF5",
            "2026-07-24", "09:00", "10:00", "hooked up the API"))
        self.creds = jira_client.JiraCredentials(
            "https://example.atlassian.net", "dev@example.com", "tok")

    def tearDown(self):
        self.db.close()

    def test_posts_worklog_and_stores_id(self):
        with patch.object(jira_client, "add_worklog", return_value="10001") as add:
            entries = [self.db.get_time_entry(self.entry_id)]
            result = jira_sync.push_worklogs(self.db, self.creds, entries)
        add.assert_called_once()
        self.assertEqual(result.created, 1)
        stored = self.db.get_time_entry(self.entry_id)
        self.assertEqual(stored.jira_worklog_id, "10001")
        self.assertEqual(stored.jira_worklog_issue, "QDM-42")
        self.assertEqual(
            stored.jira_worklog_fingerprint,
            jira_sync.worklog_fingerprint(stored))

    def _mark_synced(self, worklog_id="10001"):
        entry = self.db.get_time_entry(self.entry_id)
        self.db.mark_time_entry_worklog_synced(
            self.entry_id, worklog_id, entry.jira_key,
            jira_sync.worklog_fingerprint(entry))

    def test_second_push_updates_existing_worklog(self):
        self.db.set_time_entry_worklog_id(self.entry_id, "10001")
        with patch.object(jira_client, "update_worklog", return_value="10001") as upd:
            with patch.object(jira_client, "add_worklog") as add:
                entries = [self.db.get_time_entry(self.entry_id)]
                result = jira_sync.push_worklogs(self.db, self.creds, entries)
        upd.assert_called_once()
        add.assert_not_called()
        self.assertEqual(result.updated, 1)
        self.assertEqual(result.created, 0)

    def test_skips_entries_without_jira_key(self):
        pid = self.db.add_project(Project(None, "Other", "#1098AD"))
        aid = self.db.add_activity(Activity(None, "No key", project_id=pid))
        eid = self.db.add_time_entry(TimeEntry(
            None, aid, "No key", None, "#1098AD",
            "2026-07-24", "10:00", "11:00", ""))
        with patch.object(jira_client, "add_worklog") as add:
            result = jira_sync.push_worklogs(self.db, self.creds, [self.db.get_time_entry(eid)])
        add.assert_not_called()
        self.assertEqual(len(result.skipped), 1)

    def test_second_push_skips_unchanged_worklog(self):
        self._mark_synced()
        with patch.object(jira_client, "update_worklog") as upd:
            with patch.object(jira_client, "add_worklog") as add:
                entries = [self.db.get_time_entry(self.entry_id)]
                result = jira_sync.push_worklogs(self.db, self.creds, entries)
        upd.assert_not_called()
        add.assert_not_called()
        self.assertEqual(result.unchanged, 1)
        self.assertEqual(result.updated, 0)
        self.assertEqual(result.created, 0)

    def test_second_push_updates_when_notes_change(self):
        self._mark_synced()
        entry = self.db.get_time_entry(self.entry_id)
        entry.notes = "rewrote the comment"
        self.db.update_time_entry(entry)
        with patch.object(jira_client, "update_worklog", return_value="10001") as upd:
            with patch.object(jira_client, "add_worklog") as add:
                result = jira_sync.push_worklogs(
                    self.db, self.creds, [self.db.get_time_entry(self.entry_id)])
        upd.assert_called_once()
        add.assert_not_called()
        self.assertEqual(result.updated, 1)
        stored = self.db.get_time_entry(self.entry_id)
        self.assertEqual(
            stored.jira_worklog_fingerprint,
            jira_sync.worklog_fingerprint(stored))

    def test_color_change_is_not_a_worklog_update(self):
        self._mark_synced()
        entry = self.db.get_time_entry(self.entry_id)
        entry.color = "#FF0000"
        self.db.update_time_entry(entry)
        with patch.object(jira_client, "update_worklog") as upd:
            result = jira_sync.push_worklogs(
                self.db, self.creds, [self.db.get_time_entry(self.entry_id)])
        upd.assert_not_called()
        self.assertEqual(result.unchanged, 1)

    def test_moving_to_another_qdm_deletes_old_and_posts_new(self):
        self._mark_synced()
        entry = self.db.get_time_entry(self.entry_id)
        entry.jira_key = "QDM-99"
        self.db.update_time_entry(entry)
        with patch.object(jira_client, "delete_worklog") as delete:
            with patch.object(jira_client, "add_worklog", return_value="20002") as add:
                with patch.object(jira_client, "update_worklog") as upd:
                    result = jira_sync.push_worklogs(
                        self.db, self.creds, [self.db.get_time_entry(self.entry_id)])
        delete.assert_called_once_with(self.creds, "QDM-42", "10001")
        add.assert_called_once()
        upd.assert_not_called()
        self.assertEqual(result.created, 1)
        self.assertEqual(result.deleted, 1)
        stored = self.db.get_time_entry(self.entry_id)
        self.assertEqual(stored.jira_worklog_id, "20002")
        self.assertEqual(stored.jira_worklog_issue, "QDM-99")

    def test_deleting_a_pushed_block_removes_jira_worklog_on_push(self):
        self._mark_synced()
        self.db.delete_time_entry(self.entry_id)
        pending = self.db.list_pending_worklog_deletes("2026-07-24", "2026-07-24")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].jira_worklog_id, "10001")
        with patch.object(jira_client, "delete_worklog") as delete:
            with patch.object(jira_client, "add_worklog") as add:
                result = jira_sync.push_worklogs(
                    self.db, self.creds, [], "2026-07-24", "2026-07-24")
        delete.assert_called_once_with(self.creds, "QDM-42", "10001")
        add.assert_not_called()
        self.assertEqual(result.deleted, 1)
        self.assertEqual(
            self.db.list_pending_worklog_deletes("2026-07-24", "2026-07-24"), [])

    def test_restoring_a_deleted_block_cancels_pending_jira_delete(self):
        self._mark_synced()
        entry = self.db.get_time_entry(self.entry_id)
        self.db.delete_time_entry(self.entry_id)
        self.assertEqual(len(self.db.list_pending_worklog_deletes()), 1)
        entry.id = None
        self.db.add_time_entry(entry)
        self.assertEqual(self.db.list_pending_worklog_deletes(), [])


class TestPullWorklogs(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.tmpdir, "test.db"))
        pid = self.db.add_project(Project(None, "Platform", "#4C6EF5"))
        self.db.add_activity(Activity(
            None, "Write sync", "QDM-42", project_id=pid))
        self.issue = jira_client.JiraIssue(
            "QDM-42", "Write sync", "Story", "QDM",
            "Quasar Delivery Management", "In Progress")

    def tearDown(self):
        self.db.close()

    def test_worklog_comment_flattens_adf(self):
        adf = {"type": "doc", "content": [
            {"type": "paragraph", "content": [
                {"type": "text", "text": "hooked up"},
                {"type": "text", "text": " the API"},
            ]}
        ]}
        self.assertEqual(jira_client.worklog_comment_text(adf), "hooked up the API")
        self.assertEqual(jira_client.worklog_comment_text("  plain  note "), "plain note")

    def test_range_jql_is_this_user_and_dates(self):
        jql = jira_client.worklogs_in_range_jql("2026-09-07", "2026-09-11")
        self.assertIn("worklogAuthor = currentUser()", jql)
        self.assertIn('worklogDate >= "2026-09-07"', jql)
        self.assertIn('worklogDate <= "2026-09-11"', jql)

    def test_slot_from_started_and_duration(self):
        stamp = jira_client.worklog_started("2026-07-20", "09:00")
        slot = jira_sync.worklog_to_local_slot(jira_client.JiraWorklog(
            "10001", "QDM-42", stamp, 7200, "notes"))
        self.assertEqual(slot[0], "2026-07-20")
        self.assertEqual(slot[1], "09:00")
        self.assertEqual(slot[2], "11:00")

    def test_midnight_start_snaps_to_workday(self):
        stamp = jira_client.worklog_started("2026-07-20", "00:00")
        slot = jira_sync.worklog_to_local_slot(jira_client.JiraWorklog(
            "10001", "QDM-42", stamp, 3600, ""))
        self.assertEqual(slot[1], f"{config.START_HOUR:02d}:00")

    def test_pull_creates_synced_block(self):
        stamp = jira_client.worklog_started("2026-07-20", "09:00")
        result = jira_sync.pull_worklogs_into_db(
            self.db, [self.issue],
            [jira_client.JiraWorklog("10001", "QDM-42", stamp, 7200, "from jira")])
        self.assertEqual(result.created, 1)
        entries = self.db.list_time_entries_for_week(["2026-07-20"])
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.jira_worklog_id, "10001")
        self.assertEqual(entry.notes, "from jira")
        self.assertEqual(entry.start_time, "09:00")
        self.assertEqual(entry.end_time, "11:00")
        self.assertEqual(
            entry.jira_worklog_fingerprint, jira_sync.worklog_fingerprint(entry))

    def test_pull_skips_worklog_already_on_calendar(self):
        stamp = jira_client.worklog_started("2026-07-20", "09:00")
        wl = jira_client.JiraWorklog("10001", "QDM-42", stamp, 7200, "from jira")
        jira_sync.pull_worklogs_into_db(self.db, [self.issue], [wl])
        again = jira_sync.pull_worklogs_into_db(self.db, [self.issue], [wl])
        self.assertEqual(again.created, 0)
        self.assertEqual(again.skipped, 1)
        self.assertEqual(len(self.db.list_time_entries_for_week(["2026-07-20"])), 1)

    def test_pull_does_not_resurrect_pending_delete(self):
        stamp = jira_client.worklog_started("2026-07-20", "09:00")
        wl = jira_client.JiraWorklog("10001", "QDM-42", stamp, 7200, "from jira")
        jira_sync.pull_worklogs_into_db(self.db, [self.issue], [wl])
        entry = self.db.list_time_entries_for_week(["2026-07-20"])[0]
        self.db.delete_time_entry(entry.id)
        again = jira_sync.pull_worklogs_into_db(self.db, [self.issue], [wl])
        self.assertEqual(again.created, 0)
        self.assertEqual(self.db.list_time_entries_for_week(["2026-07-20"]), [])


class TestJiraHttp(unittest.TestCase):
    def test_test_connection_reads_display_name(self):
        creds = jira_client.JiraCredentials(
            "https://example.atlassian.net", "dev@example.com", "tok")

        def fake_urlopen(req, timeout=None, context=None):
            self.assertIn("/rest/api/2/myself", req.full_url)
            return FakeResponse({"displayName": "Alex Rae"})

        with patch("urllib.request.urlopen", fake_urlopen):
            self.assertEqual(jira_client.test_connection(creds), "Alex Rae")

    def test_html_dashboard_response_is_a_clear_error(self):
        creds = jira_client.JiraCredentials(
            "https://example.atlassian.net", "dev@example.com", "tok")

        def fake_urlopen(req, timeout=None, context=None):
            html = b"<!DOCTYPE html><html><body>dashboard</body></html>"
            return FakeResponse(html)

        with patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(jira_client.JiraError) as ctx:
                jira_client.test_connection(creds)
        self.assertIn("webpage", str(ctx.exception).lower())

    def test_http_not_https_rejected(self):
        creds = jira_client.JiraCredentials("http://jira.example.com", "a@b.c", "tok")
        with self.assertRaises(jira_client.JiraError):
            jira_client.test_connection(creds)


class TestTransitions(unittest.TestCase):
    def test_parse_and_prefer_work_completed(self):
        payload = {
            "transitions": [
                {"id": "21", "name": "In Progress", "to": {
                    "name": "In Progress",
                    "statusCategory": {"key": "indeterminate"},
                }},
                {"id": "31", "name": "Work Completed", "to": {
                    "name": "Work Completed",
                    "statusCategory": {"key": "done"},
                }},
            ]
        }
        trans = jira_client.parse_transitions(payload)
        self.assertEqual(len(trans), 2)
        close = jira_client.preferred_close_transition(trans)
        self.assertIsNotNone(close)
        self.assertEqual(close.id, "31")
        self.assertTrue(close.closes_ticket())
        self.assertFalse(trans[0].closes_ticket())

    def test_work_completed_status_is_closed_locally(self):
        act = Activity(1, "QA Planning", jira_key="QDM-5557",
                       jira_status="Work Completed", jira_status_category="done")
        self.assertTrue(act.is_closed())
        by_name = Activity(2, "Other", jira_status="Work Completed")
        self.assertTrue(by_name.is_closed())


if __name__ == "__main__":
    unittest.main()
