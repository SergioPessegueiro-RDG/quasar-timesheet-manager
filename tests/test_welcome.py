"""First-run welcome helpers — no Tkinter required."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.welcome import JIRA_API_TOKENS_URL, should_show_welcome


class _Creds:
    def __init__(self, complete):
        self._complete = complete

    def is_complete(self):
        return self._complete


class TestShouldShowWelcome(unittest.TestCase):
    def test_shows_when_jira_is_not_configured(self):
        settings = {}
        self.assertTrue(should_show_welcome(settings.get, _Creds(False)))

    def test_hides_after_connect_or_skip(self):
        settings = {"welcome_done": "1"}
        self.assertFalse(should_show_welcome(settings.get, _Creds(False)))

    def test_hides_when_jira_credentials_are_already_complete(self):
        settings = {}
        self.assertFalse(should_show_welcome(settings.get, _Creds(True)))

    def test_complete_creds_win_even_if_welcome_was_never_marked(self):
        # Env-supplied token, or an upgrade that already has a saved token:
        # don't cover the timesheet with a card they already finished.
        settings = {"welcome_done": ""}
        self.assertFalse(should_show_welcome(settings.get, _Creds(True)))

    def test_api_token_url_is_atlassians_manage_page(self):
        self.assertEqual(
            JIRA_API_TOKENS_URL,
            "https://id.atlassian.com/manage-profile/security/api-tokens")


if __name__ == "__main__":
    unittest.main()
