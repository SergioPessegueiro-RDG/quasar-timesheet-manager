"""Standalone tests for Time Block QDM ↔ issue-key / Jira-project matching.
No Tkinter required -- the widgets themselves live in app/timeblock_panel.py."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import Activity
from app.timeblock_panel import (
    activity_matching_jira_key,
    activity_matching_jira_project,
)


def _act(name, jira_key=None, jira_project=None):
    return Activity(id=1, name=name, jira_key=jira_key, jira_project=jira_project)


class TestActivityMatchingJiraKey(unittest.TestCase):
    def setUp(self):
        self.activities = [
            _act("Code Review", "QDM-482"),
            _act("Planning", "QDM-12"),
            _act("No Key"),
        ]

    def test_matches_the_number_typed_without_the_prefix(self):
        self.assertEqual(
            activity_matching_jira_key(self.activities, "482").name, "Code Review")

    def test_matches_a_pasted_full_key(self):
        self.assertEqual(
            activity_matching_jira_key(self.activities, "QDM-12").name, "Planning")

    def test_does_not_partial_match_a_longer_key(self):
        self.assertIsNone(activity_matching_jira_key(self.activities, "4"))
        self.assertIsNone(activity_matching_jira_key(self.activities, "48"))

    def test_blank_or_unknown_returns_none(self):
        self.assertIsNone(activity_matching_jira_key(self.activities, ""))
        self.assertIsNone(activity_matching_jira_key(self.activities, "999"))


class TestActivityMatchingJiraProject(unittest.TestCase):
    def test_picks_the_qdm_when_exactly_one_uses_the_project(self):
        activities = [
            _act("Alpha", "QDM-1", "Quasar Delivery Management"),
            _act("Beta", "QDM-2", "Other Client"),
        ]
        match = activity_matching_jira_project(activities, "Other Client")
        self.assertEqual(match.name, "Beta")

    def test_does_not_guess_when_many_qdms_share_the_project(self):
        activities = [
            _act("Alpha", "QDM-1", "Quasar Delivery Management"),
            _act("Beta", "QDM-2", "Quasar Delivery Management"),
        ]
        self.assertIsNone(
            activity_matching_jira_project(activities, "Quasar Delivery Management"))

    def test_unknown_or_blank_returns_none(self):
        activities = [_act("Alpha", "QDM-1", "Quasar Delivery Management")]
        self.assertIsNone(activity_matching_jira_project(activities, ""))
        self.assertIsNone(activity_matching_jira_project(activities, "Nope"))


if __name__ == "__main__":
    unittest.main()
