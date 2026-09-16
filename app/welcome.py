"""First-run Jira + Outlook calendar setup overlay.

A two-column card (Jira | Outlook) so the whole form fits a laptop
height. Covers the main window during setup — tabs, timer, and Sync
are the rest of the app, not this screen.

Settings → Welcome setup… reopens the same card with whatever is
already saved.
"""
from __future__ import annotations

import sys
import tkinter as tk
import webbrowser
from typing import Callable, List, Optional

from . import theme
from .widgets import RoundedButton, RoundedCard, RoundedEntry


JIRA_API_TOKENS_URL = "https://id.atlassian.com/manage-profile/security/api-tokens"


OUTLOOK_WEB_STEPS = (
    "On the Outlook website (outlook.office.com — not the desktop app):\n"
    "1. Settings (gear, top right). If you only see a short panel, click "
    "View all Outlook settings.\n"
    "2. Calendar → Shared calendars.\n"
    "3. Publish a calendar → Can view all details → Publish.\n"
    "4. Copy the ICS link (the one that ends in .ics). webcal:// is fine."
)


def should_show_welcome(get_setting, creds) -> bool:
    """True on a first launch that still has no usable Jira token.

    `welcome_done` is set after Continue or Skip, and also when credentials
    are already complete (env vars, or a returning install). Completeness
    is site URL + token — the same bar JiraCredentials.is_complete uses.
    """
    if (get_setting("welcome_done", "") or "") == "1":
        return False
    return not creds.is_complete()


def add_jira_api_token_link(parent, family: str, *, bg: Optional[str] = None) -> tk.Frame:
    """Clickable 'Create an API token' that opens Atlassian's token page."""
    bg = bg or theme.SURFACE
    row = tk.Frame(parent, bg=bg)
    link = tk.Label(
        row, text="Create an API token",
        fg=theme.ACCENT, bg=bg, font=(family, 9, "underline"),
        cursor="hand2")
    link.pack(side="left")

    def open_page(_event=None):
        webbrowser.open(JIRA_API_TOKENS_URL)

    def on_enter(_event=None):
        link.config(fg=theme.ACCENT_HOVER)

    def on_leave(_event=None):
        link.config(fg=theme.ACCENT)

    link.bind("<Button-1>", open_page)
    link.bind("<Enter>", on_enter)
    link.bind("<Leave>", on_leave)
    tk.Label(
        row, text=" — never leaves this machine",
        fg=theme.TEXT_MUTED, bg=bg, font=(family, 8)).pack(side="left")
    return row


class WelcomeOverlay(tk.Frame):
    """Covers the main window until the user continues or skips."""

    def __init__(self, master, family: str, *,
                 on_continue: Callable[[dict], None],
                 on_skip: Callable[[], None],
                 jira_site_url: str = "",
                 jira_email: str = "",
                 jira_api_token: str = "",
                 calendar_ics_urls: Optional[List[str]] = None):
        super().__init__(master, bg=theme.APP_BG, highlightthickness=0, bd=0)
        self.family = family
        self.on_continue = on_continue
        self.on_skip = on_skip
        self._busy = False
        self._calendar_url_rows: List[dict] = []

        card = RoundedCard(self, bg=theme.SURFACE, radius=16, outline=False, shrink=True)
        inner = tk.Frame(card.body, bg=theme.SURFACE)
        inner.pack(fill="both", expand=True, padx=28, pady=24)
        inner.columnconfigure(0, weight=1)

        tk.Label(inner, text="Welcome to QUASAR",
                 font=(self.family, 18, "bold"),
                 bg=theme.SURFACE, fg=theme.TEXT_PRIMARY).grid(
            row=0, column=0, sticky="w")
        tk.Label(
            inner,
            text="Connect Jira and paste Outlook calendar links. Skip either "
                 "part and finish later in Settings.",
            font=(self.family, 10),
            bg=theme.SURFACE, fg=theme.TEXT_SECONDARY,
            wraplength=720, justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(6, 16))

        cols = tk.Frame(inner, bg=theme.SURFACE)
        cols.grid(row=2, column=0, sticky="nsew")
        cols.columnconfigure(0, weight=1, uniform="welcome")
        cols.columnconfigure(1, weight=1, uniform="welcome")

        jira = tk.Frame(cols, bg=theme.SURFACE)
        jira.grid(row=0, column=0, sticky="nsew", padx=(0, 28))
        jira.columnconfigure(1, weight=1)
        self._build_jira(jira, jira_site_url, jira_email, jira_api_token)

        cal = tk.Frame(cols, bg=theme.SURFACE)
        cal.grid(row=0, column=1, sticky="nsew")
        cal.columnconfigure(0, weight=1)
        self._build_calendar(cal, calendar_ics_urls or [])

        footer = tk.Frame(inner, bg=theme.SURFACE)
        footer.grid(row=3, column=0, sticky="ew", pady=(18, 0))
        RoundedButton(footer, text="Continue", style="Accent.TButton",
                      command=self._continue, bg=theme.SURFACE).pack(side="left")
        RoundedButton(footer, text="Skip for now", style="Secondary.TButton",
                      command=self._skip, bg=theme.SURFACE).pack(side="left", padx=(8, 0))
        self.status_label = tk.Label(
            footer, text="", fg=theme.TEXT_SECONDARY, bg=theme.SURFACE,
            font=(self.family, 9), wraplength=420, justify="left")
        self.status_label.pack(side="left", padx=(16, 0))

        # Cover the window but leave the macOS traffic-light strip empty
        # so Close/Minimize/Zoom stay clickable. The Timesheet is unpacked
        # while this is up, so that strip is just APP_BG — not the top of
        # the sidebar/calendar cards.
        y = 0
        if sys.platform == "darwin" and getattr(master, "_themed_titlebar", False):
            y = theme.MAC_TITLEBAR_PX
        self.place(x=0, y=y, relwidth=1, relheight=1, height=-y)
        card.place(relx=0.5, rely=0.5, anchor="center")
        self.lift()

    def _build_jira(self, parent, site_url, email, api_token):
        tk.Label(parent, text="Jira", font=(self.family, 12, "bold"),
                 bg=theme.SURFACE, fg=theme.TEXT_PRIMARY).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        tk.Label(parent, text="Site URL", bg=theme.SURFACE, fg=theme.TEXT_PRIMARY,
                 font=(self.family, 10)).grid(row=1, column=0, sticky="w", pady=(0, 6))
        self.jira_url_var = tk.StringVar(value=site_url or "")
        RoundedEntry(parent, textvariable=self.jira_url_var, width=32,
                     bg=theme.SURFACE).grid(
            row=1, column=1, sticky="ew", padx=(12, 0), pady=(0, 4))
        tk.Label(parent, text="https://your-company.atlassian.net — a dashboard link is fine",
                 fg=theme.TEXT_MUTED, bg=theme.SURFACE, font=(self.family, 8),
                 wraplength=320, justify="left").grid(
            row=2, column=1, sticky="w", padx=(12, 0), pady=(0, 10))

        tk.Label(parent, text="Email", bg=theme.SURFACE, fg=theme.TEXT_PRIMARY,
                 font=(self.family, 10)).grid(row=3, column=0, sticky="w", pady=(0, 6))
        self.jira_email_var = tk.StringVar(value=email or "")
        RoundedEntry(parent, textvariable=self.jira_email_var, width=32,
                     bg=theme.SURFACE).grid(
            row=3, column=1, sticky="ew", padx=(12, 0), pady=(0, 12))

        tk.Label(parent, text="API token", bg=theme.SURFACE, fg=theme.TEXT_PRIMARY,
                 font=(self.family, 10)).grid(row=4, column=0, sticky="nw", pady=(0, 6))
        token_row = tk.Frame(parent, bg=theme.SURFACE)
        token_row.grid(row=4, column=1, sticky="ew", padx=(12, 0), pady=(0, 6))
        token_row.columnconfigure(0, weight=1)
        self.jira_token_var = tk.StringVar(value=api_token or "")
        self.jira_token_entry = RoundedEntry(
            token_row, textvariable=self.jira_token_var, width=24, show="•", bg=theme.SURFACE)
        self.jira_token_entry.pack(side="left", fill="x", expand=True)
        self._token_visible = False
        RoundedButton(token_row, text="Show", style="Secondary.TButton",
                      command=self._toggle_token, bg=theme.SURFACE).pack(side="left", padx=(8, 0))
        add_jira_api_token_link(parent, self.family).grid(
            row=5, column=1, sticky="w", padx=(12, 0))

    def _build_calendar(self, parent, urls: List[str]):
        tk.Label(parent, text="Outlook calendar", font=(self.family, 12, "bold"),
                 bg=theme.SURFACE, fg=theme.TEXT_PRIMARY).grid(
            row=0, column=0, sticky="w", pady=(0, 8))
        tk.Label(
            parent,
            text="Meetings show as a pale guide behind your hours — they are "
                 "not booked time. Add as many calendars as you need.",
            font=(self.family, 10),
            bg=theme.SURFACE, fg=theme.TEXT_SECONDARY,
            wraplength=340, justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(0, 8))
        tk.Label(
            parent, text=OUTLOOK_WEB_STEPS,
            fg=theme.TEXT_MUTED, bg=theme.SURFACE, font=(self.family, 9),
            wraplength=340, justify="left",
        ).grid(row=2, column=0, sticky="w", pady=(0, 10))

        self._calendar_rows_host = tk.Frame(parent, bg=theme.SURFACE)
        self._calendar_rows_host.grid(row=3, column=0, sticky="ew")
        self._calendar_rows_host.columnconfigure(0, weight=1)
        values = [u for u in urls if (u or "").strip()] or [""]
        for url in values:
            self._add_ics_row(url)

        RoundedButton(parent, text="Add ICS link", style="Secondary.TButton",
                      command=lambda: self._add_ics_row(""),
                      bg=theme.SURFACE).grid(row=4, column=0, sticky="w", pady=(4, 0))

    def _add_ics_row(self, url: str = ""):
        host = self._calendar_rows_host
        row = tk.Frame(host, bg=theme.SURFACE)
        row.pack(fill="x", pady=(0, 6))
        var = tk.StringVar(value=url or "")
        entry = RoundedEntry(row, textvariable=var, width=28, bg=theme.SURFACE)
        entry.pack(side="left", fill="x", expand=True)
        RoundedButton(
            row, text="Remove", style="Secondary.TButton",
            command=lambda f=row: self._remove_ics_row(f),
            bg=theme.SURFACE,
        ).pack(side="left", padx=(8, 0))
        self._calendar_url_rows.append({"frame": row, "var": var, "entry": entry})
        if len(self._calendar_url_rows) > 1 and not (url or "").strip():
            try:
                entry.focus_set()
            except tk.TclError:
                pass

    def _remove_ics_row(self, frame):
        if len(self._calendar_url_rows) <= 1:
            self._calendar_url_rows[0]["var"].set("")
            return
        remaining = []
        for row in self._calendar_url_rows:
            if row["frame"] is frame:
                row["frame"].destroy()
            else:
                remaining.append(row)
        self._calendar_url_rows = remaining

    def fields(self) -> dict:
        return {
            "jira": {
                "site_url": self.jira_url_var.get().strip(),
                "email": self.jira_email_var.get().strip(),
                "api_token": self.jira_token_var.get().strip(),
                "project_key": "QDM",
            },
            "ics_urls": [row["var"].get().strip() for row in self._calendar_url_rows],
        }

    def set_status(self, text: str):
        try:
            self.status_label.config(text=text or "")
        except tk.TclError:
            pass

    def set_busy(self, busy: bool):
        self._busy = bool(busy)

    def _toggle_token(self):
        self._token_visible = not self._token_visible
        self.jira_token_entry.configure(show="" if self._token_visible else "•")

    def _continue(self):
        if self._busy:
            return
        self.on_continue(self.fields())

    def _skip(self):
        if self._busy:
            return
        self.on_skip()
