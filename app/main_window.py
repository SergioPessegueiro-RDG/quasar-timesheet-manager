"""Top-level application window: header bar, sidebar, and weekly calendar grid."""
import os
import platform
import sys
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from datetime import date, timedelta
from tkinter import messagebox, ttk
from typing import Optional

from . import auto_update, config, jira_client, jira_sync, theme, update_check
from .calendar_view import CalendarGrid
from .db import Database
from .export_csv import export_entries
from .models import Project
from .panels import ActivityPanel, BackupPanel, DuplicatePanel, ExportPanel, ProjectPanel, SettingsPanel
from .sidebar import Sidebar
from .summary_panel import SummaryPanel
from .timeblock_panel import TimeBlockPanel
from .timer_bar import TimerBar
from .version import APP_VERSION
from .widgets import RoundedButton, RoundedCombobox, RoundedEntry


class MainWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("QUASAR Timesheet Manager")
        self.geometry("1240x680")
        self.minsize(960, 520)
        self._center_on_start()
        self._log_startup_diagnostics()

        self.db = Database()

        # The user's own "Custom" palette seeds -- loaded before
        # set_theme() below so that, if theme_mode is "custom", it
        # resolves against the user's actual saved colors rather than
        # theme.py's built-in defaults for a brand-new install.
        _custom_defaults = theme.get_custom_seeds()
        theme.set_custom_seeds(
            self.db.get_setting("custom_theme_app_bg", _custom_defaults["app_bg"]) or _custom_defaults["app_bg"],
            self.db.get_setting("custom_theme_panel_bg", _custom_defaults["panel_bg"]) or _custom_defaults["panel_bg"],
            self.db.get_setting("custom_theme_text", _custom_defaults["text_primary"]) or _custom_defaults["text_primary"],
            self.db.get_setting("custom_theme_accent", _custom_defaults["accent"]) or _custom_defaults["accent"],
        )

        saved_theme = self.db.get_setting("theme_mode", theme.DEFAULT_THEME_ID) or theme.DEFAULT_THEME_ID
        theme.set_theme(saved_theme)

        saved_alpha = self.db.get_setting("glass_alpha", str(theme.WINDOW_ALPHA_DEFAULT))
        theme.set_glass_alpha(saved_alpha)

        # Work Hours / Show Weekends (Settings tab) -- same "load once at
        # startup, mutate the config module's globals" pattern theme.py's
        # own colors use, so every place that already reads
        # config.START_HOUR/config.END_HOUR/config.DAY_NAMES picks this up
        # with no further plumbing needed.
        saved_start_hour = int(self.db.get_setting("work_start_hour", str(config.START_HOUR))
                                or config.START_HOUR)
        saved_end_hour = int(self.db.get_setting("work_end_hour", str(config.END_HOUR))
                              or config.END_HOUR)
        config.set_work_hours(saved_start_hour, saved_end_hour)
        config.set_show_weekends((self.db.get_setting("show_weekends", "0") or "0") == "1")

        # Heading (Settings tab) -- same "load once at startup" pattern as
        # the theme/work-hours settings above. "standard" is the original,
        # unchanged header (full logo + title); "compact" shrinks it;
        # "hidden" removes the header row entirely. Any unrecognized/stale
        # value (a setting from an older build, a hand-edited db, ...)
        # falls back to "standard" rather than a KeyError deeper in
        # _build_top_bar's HEADER_STYLES lookup.
        self.header_style = self.db.get_setting("header_style", "standard") or "standard"
        if self.header_style not in ("standard", "compact", "hidden"):
            self.header_style = "standard"

        # Timer bar visibility (Settings tab). Defaults to shown ("1") so
        # existing installs' behavior is unchanged until someone opts in
        # to hiding it. self.timer_bar itself is always built (see
        # _build_top_bar) even when this is False -- only its on-screen
        # row is skipped -- so a timer already running keeps running
        # invisibly rather than being interrupted by hiding the bar.
        self.show_timer_bar = (self.db.get_setting("show_timer_bar", "1") or "1") == "1"

        # Sidebar collapse (small « / » toggle on the Sidebar itself, not
        # a Settings entry -- collapsing/expanding is a live layout choice
        # someone makes while looking at the window, the same kind of
        # thing a theme or work-hours change isn't). Shared by both
        # Sidebar instances (Timesheet and Template tabs -- see
        # _build_body) so they always collapse together.
        self.sidebar_collapsed = (self.db.get_setting("sidebar_collapsed", "0") or "0") == "1"
        try:
            self.sidebar_width = int(
                self.db.get_setting("sidebar_width", str(config.DEFAULT_SIDEBAR_WIDTH_PX))
                or config.DEFAULT_SIDEBAR_WIDTH_PX)
        except (TypeError, ValueError):
            self.sidebar_width = config.DEFAULT_SIDEBAR_WIDTH_PX
        self.sidebar_width = max(
            config.MIN_SIDEBAR_WIDTH_PX,
            min(config.MAX_SIDEBAR_WIDTH_PX, self.sidebar_width))
        self._sidebar_bodies = []
        self._sash_dragging = False

        self.family = theme.apply_theme(self)
        self.configure(bg=theme.APP_BG)
        self._jira_sync_in_flight = False
        self._worklog_pull_seq = 0

        self._build_menu()
        self._build_top_bar()
        self._build_body()
        self._bind_global_shortcuts()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if sys.platform == "darwin":
            # Older Tk builds (the one macOS still bundles system-wide) have
            # a long-standing Cocoa bug where a freshly-created window shows
            # up blank until something forces it to repaint -- e.g. the user
            # resizing or minimizing it. Nudging the window by a pixel and
            # back right after launch forces that repaint automatically so
            # nobody has to do it by hand.
            self.after(150, self._nudge_to_force_repaint)

        # Delayed so it never competes with getting the window itself on
        # screen first -- the actual network call happens on a background
        # thread regardless (see _check_for_updates), this delay is just
        # about not kicking that thread off in the same instant as
        # everything else __init__ is doing.
        self._jira_sync_in_flight = False
        self.after(400, self._sync_qdms_on_startup)
        self.after(1500, self._check_for_updates)

    def _pull_worklogs_for_week(self, week_start: date):
        """Quietly import this user's Jira worklogs for the visible week.

        So going back a week shows hours already logged in Jira, not just
        blocks created in this app. Local rows and pending deletes win.
        """
        creds = self._jira_creds()
        if not creds.is_complete():
            return
        start_date = week_start.isoformat()
        end_date = (week_start + timedelta(days=config.week_end_offset())).isoformat()
        self._worklog_pull_seq += 1
        seq = self._worklog_pull_seq

        def worker():
            try:
                issues, worklogs = jira_client.fetch_my_worklogs(
                    creds, start_date, end_date)
            except Exception as exc:
                jira_client._log(
                    f"fetch_my_worklogs {start_date}..{end_date} failed: "
                    f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
                return
            self.after(
                0,
                lambda issues=issues, worklogs=worklogs, seq=seq,
                week_start=week_start: self._apply_worklog_pull(
                    seq, week_start, issues, worklogs))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_worklog_pull(self, seq: int, week_start: date, issues, worklogs):
        if seq != self._worklog_pull_seq:
            return
        if not hasattr(self, "calendar") or self.calendar.week_start != week_start:
            return
        try:
            result = jira_sync.pull_worklogs_into_db(self.db, issues, worklogs)
        except Exception as exc:
            jira_client._log(f"pull_worklogs_into_db failed: {exc}\n{traceback.format_exc()}")
            return
        if result.created:
            self.calendar.refresh()
            self._on_sidebar_change()
            self._set_jira_busy(
                False, f"Loaded {result.created} worklog(s) from Jira.", quiet=True)

    def _check_for_updates(self):
        """Best-effort, silent-on-failure check for a newer GitHub Release
        than this build's own version.py -- see update_check.py's module
        docstring for exactly what "failure" covers (no network, private
        repo, no releases yet, ...). Runs the actual HTTP request on a
        background thread since it can block for a few seconds; the
        result comes back onto the main thread via self.after(0, ...)
        because Tkinter widgets (the popup) can only be touched from
        there."""
        def worker():
            info = update_check.check_latest_version()
            if info is not None:
                self.after(0, lambda: self._on_update_check_result(info))
        threading.Thread(target=worker, daemon=True).start()

    def _on_update_check_result(self, info: dict):
        latest_tag = info.get("tag_name", "")
        if not update_check.is_newer(latest_tag, APP_VERSION):
            return

        # A version the user already said "skip" to for THIS build stays
        # skipped -- but only until something newer than that skipped
        # version shows up, so a follow-up release after the one they
        # skipped still gets offered.
        skipped = self.db.get_setting("skipped_update_version", "") or ""
        if skipped and not update_check.is_newer(latest_tag, skipped):
            return

        html_url = info.get("html_url") or f"https://github.com/{update_check.GITHUB_REPO}/releases/latest"
        choice = messagebox.askyesnocancel(
            "Update available",
            f"A new version of QUASAR Timesheet Manager is available: {latest_tag} "
            f"(you have v{APP_VERSION}).\n\n"
            "Download it now? QUASAR will download it, close, and reopen on "
            "its own -- there's nothing to unzip or move yourself. (Choosing "
            "\u201cNo\u201d won't ask again about this version -- \u201cCancel\u201d "
            "will ask again next time you open the app.)",
        )
        if choice is True:
            self._start_auto_update(info, html_url)
        elif choice is False:
            self.db.set_setting("skipped_update_version", latest_tag)
        # choice is None (Cancel/closed) -- do nothing, ask again next launch.

    def _start_auto_update(self, info: dict, html_url: str):
        """Kicks off the download/swap in the background (see
        auto_update.py) after the user picks "Yes". Runs off the main
        thread since the download can take a few seconds; a failure at
        any point -- not a packaged build, no matching release asset, a
        network hiccup mid-download, a bad zip -- falls back to exactly
        the old behavior (open the release page in a browser) instead of
        leaving the user stuck."""
        messagebox.showinfo(
            "Downloading update",
            "Downloading the latest version now. QUASAR Timesheet Manager "
            "will close and reopen automatically once it's ready -- this "
            "can take a few seconds depending on your connection.",
        )

        def worker():
            try:
                auto_update.perform_update(info.get("assets") or [])
            except auto_update.AutoUpdateError as exc:
                self.after(0, lambda: self._auto_update_failed(html_url, exc))
            else:
                self.after(0, self._quit_for_update)

        threading.Thread(target=worker, daemon=True).start()

    def _auto_update_failed(self, html_url: str, exc: Exception):
        messagebox.showinfo(
            "Couldn't auto-update",
            "The automatic update couldn't complete, so opening the "
            "download page in your browser instead -- you'll need to "
            "download and reinstall it yourself this time.\n\n"
            f"(Details: {exc})",
        )
        webbrowser.open(html_url)

    def _quit_for_update(self):
        """Closes the app the same way _on_close does (log any running
        timer, close the database) so the swap script -- already waiting
        on this process's PID -- can safely replace the install the
        moment this process actually exits."""
        if self.timer_bar.is_running():
            self.timer_bar.stop()
        self.db.close()
        self.destroy()

    def _nudge_to_force_repaint(self):
        try:
            x, y = self.winfo_x(), self.winfo_y()
            self.geometry(f"+{x + 1}+{y}")
            self.after(50, lambda: self.geometry(f"+{x}+{y}"))
        except tk.TclError:
            pass

    def _log_startup_diagnostics(self):
        """Best-effort, never-blocking record of exactly what Tk thinks
        it's running on, written once per launch to
        ~/.jira_timesheet/startup_diagnostics.log -- added specifically
        to chase down a real report of a packaged macOS build (built via
        GitHub Actions, see release.yml) opening at the wrong size and
        rendering noticeably slower than the exact same code built
        locally, despite both bundling the same Tcl/Tk 8.6. The likely
        suspects (which windowing backend Tk actually initialized as --
        native Aqua vs. a generic/X11-style fallback -- and what it
        thinks the screen's real dimensions are) are only visible from
        inside a running Tk instance, so this is the only way to compare
        a 'good' build against a 'bad' one without guessing. Wrapped in
        its own try/except, same reasoning as update_check.py's
        _log_check_failure: a diagnostic must never itself become a new
        way for the app to fail to start."""
        try:
            # Forces Tk to actually process the geometry() call from
            # _center_on_start() before asking winfo_width/height for
            # it below -- without this, they'd still report Tk's
            # not-yet-realized placeholder size (1x1) rather than what
            # was actually requested.
            self.update_idletasks()
            os.makedirs(config.APP_DIR, exist_ok=True)
            log_path = os.path.join(config.APP_DIR, "startup_diagnostics.log")
            lines = [
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} APP_VERSION={APP_VERSION} "
                f"frozen={getattr(sys, 'frozen', False)} platform={sys.platform}",
                f"  mac_ver={platform.mac_ver()[0]!r}" if sys.platform == "darwin" else "  (not macOS)",
                f"  tk windowingsystem={self.tk.call('tk', 'windowingsystem')!r} "
                f"tcl patchlevel={self.tk.call('info', 'patchlevel')!r}",
                f"  screen={self.winfo_screenwidth()}x{self.winfo_screenheight()} "
                f"window={self.winfo_width()}x{self.winfo_height()} "
                f"requested_geometry={self.geometry()!r} "
                f"fpixels_per_inch={self.winfo_fpixels('1i')}",
            ]
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception:
            pass  # a diagnostic must never itself become a startup failure

    def _center_on_start(self):
        """Open at the designed size, centered — not fullscreen."""
        try:
            self.update_idletasks()
            w, h = 1240, 780
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
            x = max(40, (sw - w) // 2)
            y = max(40, (sh - h) // 2)
            self.geometry(f"{w}x{h}+{x}+{y}")
        except tk.TclError:
            pass

    def _build_menu(self):
        menubar = tk.Menu(self)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Sync QDMs from Jira…", command=self._sync_qdms_from_jira)
        file_menu.add_command(label="Push hours to Jira…", command=self._open_export_dialog)
        file_menu.add_command(label="Export to Jira CSV…", command=self._open_export_dialog)
        file_menu.add_separator()
        file_menu.add_command(label="Backup & Restore…", command=self._open_backup_dialog)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        settings_menu = tk.Menu(menubar, tearoff=0)
        settings_menu.add_command(label="Jira & Settings…", command=self._open_settings_dialog)
        menubar.add_cascade(label="Settings", menu=settings_menu)

        # The old binary "Dark Mode" checkbutton lived here; it's been
        # replaced by a full theme picker embedded in the Settings tab (see
        # panels.SettingsPanel and app/theme.py's THEMES) since there are
        # now twenty curated themes (plus a Custom one) to choose from, not
        # just two. This menu item is
        # a shortcut straight to that picker rather than a second, separate
        # place to change it.
        view_menu = tk.Menu(menubar, tearoff=0)
        view_menu.add_command(label="Theme…", command=self._open_settings_dialog)
        menubar.add_cascade(label="View", menu=view_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="How to use", command=self._show_help)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.config(menu=menubar)

    # Size profile for each "Heading" choice (Settings tab): pixel size of
    # the logo mark, its canvas, the outer row's vertical padding, and the
    # title's font size. "standard" matches the header's original,
    # unchanged look; "compact" shrinks all four; "hidden" isn't listed
    # here since _build_top_bar returns before using this at all in that
    # case.
    _HEADER_PROFILES = {
        "standard": {"logo_size": 22, "canvas_px": 24, "pady": 6, "title_pt": 13},
        "compact": {"logo_size": 16, "canvas_px": 18, "pady": 4, "title_pt": 12},
    }

    def _build_top_bar(self, initial_timer_state=None):
        # Title and timer share one toolbar row. Two stacked full-width
        # bands (logo banner + padded timer card) were most of the
        # vertical chrome on a laptop, and Tk would not shrink past them.
        show_header = self.header_style != "hidden"
        show_timer = self.show_timer_bar
        themed = getattr(self, "_themed_titlebar", False)

        if not show_header and not show_timer:
            if themed:
                tk.Frame(self, bg=theme.APP_BG, height=theme.MAC_TITLEBAR_PX).pack(fill="x")
            self.timer_bar = TimerBar(
                self, self.db, get_activities=lambda: self.db.list_activities(),
                on_saved=self._on_timer_saved, family=self.family,
                initial_state=initial_timer_state,
                on_need_notes=self._prompt_timer_notes)
            return

        bar = tk.Frame(self, bg=theme.PANEL_BG)
        bar.pack(fill="x")
        pad_l = theme.themed_titlebar_left_pad() if themed else 16
        profile = self._HEADER_PROFILES.get(self.header_style, self._HEADER_PROFILES["standard"])
        inner = tk.Frame(bar, bg=theme.PANEL_BG)
        inner.pack(fill="x", padx=(pad_l, 12), pady=profile["pady"] if show_header else 4)

        if show_header:
            title_row = tk.Frame(inner, bg=theme.PANEL_BG)
            title_row.pack(side="left")
            logo = tk.Canvas(title_row, width=profile["canvas_px"], height=profile["canvas_px"],
                              bg=theme.PANEL_BG, highlightthickness=0)
            logo.pack(side="left", padx=(0, 8))
            theme.draw_logo_mark(logo, size=profile["logo_size"])
            tk.Label(title_row, text="QUASAR Timesheet Manager",
                     font=(self.family, profile["title_pt"], "bold"),
                     bg=theme.PANEL_BG, fg=theme.TEXT_PRIMARY).pack(side="left")

        status = None
        if show_timer:
            # Confirmation after Stop lives here in the leftover header gap
            # (between the title and the timer), not inside TimerBar. The
            # timer cluster is packed right first so its width never includes
            # that message — otherwise "Logged 15 min to …" shoves the picker
            # left. width=1 + expand is the Tk clip trick: the label only
            # takes leftover space and won't steal width from the controls.
            status = tk.Label(
                inner, text="", font=(self.family, 9), bg=theme.PANEL_BG,
                fg=theme.TEXT_SECONDARY, anchor="center", width=1)
        self.timer_bar = TimerBar(
            inner if show_timer else self, self.db,
            get_activities=lambda: self.db.list_activities(),
            on_saved=self._on_timer_saved, family=self.family,
            initial_state=initial_timer_state, bg=theme.PANEL_BG,
            status_widget=status,
            on_need_notes=self._prompt_timer_notes)
        if show_timer:
            self.timer_bar.pack(side="right" if show_header else "left")
            status.pack(side="left", fill="x", expand=True, padx=12)

        sep = tk.Frame(self, bg=theme.BORDER, height=1)
        sep.pack(fill="x")

    def _prompt_timer_notes(self, activity, date_str: str, start_str: str, end_str: str):
        """Stop-timer path: same Time Block description prompt as placing a QDM."""
        self.calendar._go_today()
        log_date = date.fromisoformat(date_str)
        day_idx = (log_date - self.calendar.week_start).days
        force_date = None
        if not (0 <= day_idx < len(config.DAY_NAMES)):
            day_idx = 0
            force_date = date_str

        def on_committed(entry):
            mins = entry.duration_minutes()
            self.timer_bar._set_status(f"Logged {mins} min to {entry.activity_name}.")

        def on_cancelled():
            self.timer_bar._set_status("Timer stopped — nothing logged.")

        self.calendar._open_entry_dialog(
            new=True, day_idx=day_idx,
            start_hhmm=start_str, end_hhmm=end_str,
            placed_activity=activity,
            require_notes=True,
            force_date=force_date,
            on_committed=on_committed,
            on_cancelled=on_cancelled,
        )

    def _on_timer_saved(self, entry):
        # The timer always logs against *today*, regardless of which tab or
        # which week is currently on screen -- jump the Timesheet tab (not
        # Template, which isn't date-based) to today's week and bring it to
        # the front so the block that was just logged is immediately
        # visible, the same way a manually-drawn block would be.
        self._on_sidebar_change()
        self.calendar._go_today()
        self.notebook.select(0)

    def _toggle_sidebar_collapsed(self):
        """The «/» button on the Sidebar itself (both the Timesheet and
        Template tabs' -- see _build_body). Persists, then reuses the
        same full destroy-and-rebuild _select_theme already triggers for
        a theme/work-hours change: the sidebar's width is set via the
        grid column it lives in (see _place_sidebar_and_calendar), which is
        owned by MainWindow, not by Sidebar itself, so there's no
        cheaper way to resize it in place."""
        self.sidebar_collapsed = not self.sidebar_collapsed
        self.db.set_setting("sidebar_collapsed", "1" if self.sidebar_collapsed else "0")
        self._apply_theme_and_rebuild(theme.get_theme_id())

    def _place_sidebar_and_calendar(self, body, sidebar, calendar):
        """Sidebar | draggable sash | calendar.

        Width is a stored pixel size, not a fraction of the window: long
        QDM names need the list to grow horizontally without waiting for
        the whole window to grow, and growing the window should give that
        extra space to the calendar instead of stretching names you
        already chose a width for.
        """
        width = (config.SIDEBAR_COLLAPSED_WIDTH_PX if self.sidebar_collapsed
                 else self.sidebar_width)
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, minsize=width, weight=0)
        body.grid_columnconfigure(1, minsize=8, weight=0)
        body.grid_columnconfigure(2, weight=1)

        sidebar.grid(row=0, column=0, sticky="nsew")
        # Hit target only — same fill as the panes so it isn't a visible
        # gutter between QDMs and the calendar.
        sash = tk.Frame(body, width=8, bg=theme.APP_BG, highlightthickness=0, bd=0,
                        cursor="" if self.sidebar_collapsed else "sb_h_double_arrow")
        sash.grid(row=0, column=1, sticky="ns")
        sash.grid_propagate(False)
        calendar.grid(row=0, column=2, sticky="nsew")
        self._sidebar_bodies.append(body)
        self._bind_sidebar_sash(sash, body, sidebar, calendar)

    def _apply_sidebar_column_width(self, body=None, sidebar=None):
        width = (config.SIDEBAR_COLLAPSED_WIDTH_PX if self.sidebar_collapsed
                 else self.sidebar_width)
        bodies = [body] if body is not None else self._sidebar_bodies
        for b in bodies:
            try:
                b.grid_columnconfigure(0, minsize=width, weight=0)
            except tk.TclError:
                pass
        if self.sidebar_collapsed:
            return
        sidebars = [sidebar] if sidebar is not None else (
            getattr(self, "sidebar", None), getattr(self, "template_sidebar", None))
        for sb in sidebars:
            if sb is not None:
                sb.set_content_width(self.sidebar_width)

    def _bind_sidebar_sash(self, sash, body, sidebar, calendar):
        state = {"origin": 0, "start": 0, "active": False}

        def start(event):
            if self.sidebar_collapsed:
                return
            state["origin"] = event.x_root
            state["start"] = self.sidebar_width
            state["active"] = True
            self._sash_dragging = True
            calendar.set_relayout_deferred(True)
            try:
                sash.grab_set()
            except tk.TclError:
                pass
            sash.configure(bg=theme.ACCENT_SOFT)
            # Pointer routinely leaves the 6px sash mid-drag; grab plus a
            # window-level bind keep motion events coming so names track
            # the mouse instead of stalling until release.
            state["motion"] = self.bind("<B1-Motion>", drag)
            state["release"] = self.bind("<ButtonRelease-1>", end)

        def drag(event):
            if self.sidebar_collapsed or not state["active"]:
                return
            window_cap = max(config.MIN_SIDEBAR_WIDTH_PX, int(self.winfo_width() * 0.55))
            max_w = min(config.MAX_SIDEBAR_WIDTH_PX, window_cap)
            new_w = state["start"] + (event.x_root - state["origin"])
            new_w = max(config.MIN_SIDEBAR_WIDTH_PX, min(max_w, new_w))
            if new_w == self.sidebar_width:
                return
            self.sidebar_width = new_w
            # Only the pane being dragged — the other tab's calendar stays
            # put until mouse-up. wraplength updates in place; nothing is
            # destroyed, so names track the sash without a black flash.
            self._apply_sidebar_column_width(body=body, sidebar=sidebar)
            body.update_idletasks()

        def end(_event=None):
            if not state["active"]:
                return
            state["active"] = False
            self._sash_dragging = False
            try:
                sash.grab_release()
            except tk.TclError:
                pass
            for key, seq in (("motion", "<B1-Motion>"), ("release", "<ButtonRelease-1>")):
                funcid = state.pop(key, None)
                if funcid:
                    try:
                        self.unbind(seq, funcid)
                    except tk.TclError:
                        pass
            sash.configure(bg=theme.APP_BG)
            self._apply_sidebar_column_width()
            calendar.set_relayout_deferred(False)
            self.db.set_setting("sidebar_width", str(int(self.sidebar_width)))

        def hover_in(_event):
            if not self.sidebar_collapsed:
                sash.configure(bg=theme.ACCENT_SOFT)

        def hover_out(_event):
            if not state["active"]:
                sash.configure(bg=theme.APP_BG)

        sash.bind("<ButtonPress-1>", start)
        sash.bind("<B1-Motion>", drag)
        sash.bind("<ButtonRelease-1>", end)
        sash.bind("<Enter>", hover_in)
        sash.bind("<Leave>", hover_out)

    def _build_body(self, initial_week_start=None):
        # A Notebook (tabs at the top) rather than a bare frame: every
        # dialog that used to be a pop-up window (add/edit time block,
        # duplicate, add/edit project, settings, export) is now a tab that
        # appears next to "Timesheet" only while it's in use, and hides
        # again afterwards -- see _show_panel()/_hide_panel() below and the
        # docstrings in app/timeblock_panel.py and app/panels.py for why
        # tabs instead of pop-up windows.
        #
        # "Template" is different: it's a second permanent tab (never hidden
        # via _register_panel/_show_panel) holding a recurring Mon-Fri week
        # of blocks that isn't tied to any real date -- see the module
        # docstring in app/calendar_view.py. "Apply Template to This Week"
        # on the Timesheet tab copies it onto whatever week is open there.
        # Hand-drawn tab strip instead of ttk.Notebook's own (square-
        # cornered, see theme.apply_theme's tabposition="" note) -- this
        # row of RoundedButton toggles is purely cosmetic. It doesn't
        # replace any of the notebook's actual tab-management: `self.
        # notebook` underneath still gets every .add/.tab(state=...)/
        # .select() call exactly as before, from the exact same call
        # sites (_register_panel/_show_panel/_hide_panel below) -- this
        # bar just mirrors whichever tabs are currently in "normal" state
        # and calls .select() on click, refreshed by _refresh_tab_bar()
        # (called from _on_tab_changed, so it stays in sync with every
        # notebook.select()/tab(state=...) call anywhere in this file).
        # tab_row holds the hand-drawn tab strip on the left and Export
        # CSV / Push to Jira on the right -- those used to live in the
        # header (see _build_top_bar), but now always sit here instead,
        # regardless of which Heading size is chosen (including "hidden",
        # which has no header row left to hold them at all). self.tab_bar
        # itself holds only the tab buttons: _refresh_tab_bar() below
        # destroys and rebuilds *its* children on every tab change, so
        # the CTAs live in this separate sibling frame instead of inside
        # tab_bar, where that rebuild would otherwise destroy them too.
        tab_row = tk.Frame(self, bg=theme.APP_BG)
        # Equal gap above and below -- it used to be flush against the
        # separator under the Timer bar (pady top=0) while still getting
        # 8px before the notebook/content card below, which read as
        # crowded against the Timer section and lopsided against the card.
        tab_row.pack(fill="x", padx=16, pady=(10, 10))

        # pack() side=right stacks inward, so Push is packed first to stay
        # on the far right, with Export CSV immediately to its left.
        RoundedButton(tab_row, text="Push to Jira", style="Accent.TButton",
                      command=self._open_export_dialog).pack(side="right")
        RoundedButton(tab_row, text="Export CSV", style="Secondary.TButton",
                      command=self._open_export_dialog).pack(side="right", padx=(0, 8))

        self.tab_bar = tk.Frame(tab_row, bg=theme.APP_BG)
        self.tab_bar.pack(side="left", fill="x", expand=True)
        self._all_tabs: list = []  # [(widget, tab_text), ...] in tab order

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._panels = []

        body = tk.Frame(self.notebook, bg=theme.APP_BG)
        self.notebook.add(body, text="Timesheet")
        self._all_tabs.append((body, "Timesheet"))
        # Kept so _active_calendar() (see the keyboard-shortcut handlers
        # below) can tell which tab is currently showing -- notebook.select()
        # returns the tab *container* widget's path, not self.calendar
        # itself, since the calendar is nested a level deeper alongside the
        # sidebar.
        self.timesheet_tab = body
        self._sidebar_bodies = []

        self.sidebar = Sidebar(body, self.db, on_change=self._on_sidebar_change,
                                open_activity_panel=self._open_activity_panel,
                                open_project_panel=self._open_project_panel,
                                collapsed=self.sidebar_collapsed,
                                on_toggle_collapse=self._toggle_sidebar_collapsed,
                                on_jira_sync=self._sync_qdms_from_jira,
                                content_width=self.sidebar_width)
        self.calendar = CalendarGrid(
            body, self.db,
            get_armed_activity=lambda: self.sidebar.get_armed_activity(),
            clear_armed_activity=lambda: self.sidebar.clear_armed(),
            open_time_block=self._open_time_block_panel,
            open_duplicate=self._open_duplicate_panel,
            initial_week_start=initial_week_start,
            on_week_change=self._pull_worklogs_for_week,
        )
        self._place_sidebar_and_calendar(body, self.sidebar, self.calendar)
        self.sidebar.set_calendar(self.calendar)

        # Permanent "Template" tab -- built the same way as "Timesheet"
        # above, but backed by TemplateEntry rows (template_mode=True) with
        # its own Sidebar/CalendarGrid pair so arming/editing activities
        # there doesn't interfere with whatever's armed on the real
        # Timesheet tab. Added directly via self.notebook.add (NOT
        # self._register_panel) so it's never auto-hidden by _show_panel.
        template_body = tk.Frame(self.notebook, bg=theme.APP_BG)
        self.notebook.add(template_body, text="Template")
        self._all_tabs.append((template_body, "Template"))
        self.template_tab = template_body

        self.template_sidebar = Sidebar(template_body, self.db, on_change=self._on_sidebar_change,
                                         open_activity_panel=self._open_activity_panel,
                                         open_project_panel=self._open_project_panel,
                                         collapsed=self.sidebar_collapsed,
                                         on_toggle_collapse=self._toggle_sidebar_collapsed,
                                         on_jira_sync=self._sync_qdms_from_jira,
                                         content_width=self.sidebar_width)
        self.template_calendar = CalendarGrid(
            template_body, self.db,
            get_armed_activity=lambda: self.template_sidebar.get_armed_activity(),
            clear_armed_activity=lambda: self.template_sidebar.clear_armed(),
            open_time_block=self._open_time_block_panel,
            open_duplicate=self._open_duplicate_panel,
            template_mode=True,
        )
        self._place_sidebar_and_calendar(template_body, self.template_sidebar, self.template_calendar)
        self.template_sidebar.set_calendar(self.template_calendar)

        # Permanent "Summary" tab -- like Template, added directly via
        # self.notebook.add (not self._register_panel) so it's never
        # auto-hidden; it's a place you come back to, not a one-shot dialog.
        self.summary_panel = SummaryPanel(self.notebook, self.db, family=self.family)
        self.notebook.add(self.summary_panel, text="Summary")
        self._all_tabs.append((self.summary_panel, "Summary"))

        # Permanent "Settings" tab -- same treatment as Template/Summary
        # above (added directly, never auto-hidden) instead of the
        # hide-until-opened panel it used to be, reached by a header
        # button that's now gone. on_close no longer hides this tab (there
        # would be nothing left to switch back to it with) -- Save/Cancel
        # just jump back to the Timesheet tab, same "you're done, here's
        # your other work" feel as before, minus actually disappearing.
        self.settings_panel = SettingsPanel(
            self.notebook, family=self.family,
            on_close=lambda: self.notebook.select(0))
        self.notebook.add(self.settings_panel, text="Settings")
        self._all_tabs.append((self.settings_panel, "Settings"))
        self._load_settings_panel()

        self.timeblock_panel = TimeBlockPanel(
            self.notebook, family=self.family,
            on_close=lambda: self._hide_panel(self.timeblock_panel))
        self._register_panel(self.timeblock_panel, "Time Block")

        self.duplicate_panel = DuplicatePanel(
            self.notebook, family=self.family,
            on_close=lambda: self._hide_panel(self.duplicate_panel))
        self._register_panel(self.duplicate_panel, "Duplicate")

        self.activity_panel = ActivityPanel(
            self.notebook, family=self.family,
            on_close=lambda: self._hide_panel(self.activity_panel),
            get_projects=lambda: self.db.list_projects(),
            create_project=self._create_project_inline)
        self._register_panel(self.activity_panel, "Add QDM")

        self.project_panel = ProjectPanel(
            self.notebook, family=self.family,
            on_close=lambda: self._hide_panel(self.project_panel))
        self._register_panel(self.project_panel, "Project")

        self.backup_panel = BackupPanel(
            self.notebook, family=self.family,
            on_close=lambda: self._hide_panel(self.backup_panel),
            on_backup=self._backup_data, on_restore=self._restore_data)
        self._register_panel(self.backup_panel, "Backup")

        self.export_panel = ExportPanel(
            self.notebook, family=self.family,
            on_close=lambda: self._hide_panel(self.export_panel))
        self._register_panel(self.export_panel, "Export")

        self._refresh_tab_bar()

    def _on_sidebar_change(self):
        # Both tabs share the same activities/projects tables, so an
        # arm/edit/delete on either sidebar needs to refresh both
        # calendars, both sidebars, and the timer bar's activity picker
        # to stay in sync.
        self.calendar.refresh()
        self.template_calendar.refresh()
        self.sidebar.refresh()
        self.template_sidebar.refresh()
        self.timer_bar.refresh_activities()

    def _create_project_inline(self, name: str) -> Project:
        """Used by the Add QDM tab's "+ New Project..." option (see
        ActivityPanel.create_project in app/panels.py) to create a Project
        without leaving that tab -- unlike the dedicated Add Project tab,
        this doesn't ask for a color; one is auto-picked (see
        Database.add_project_with_default_color)."""
        project = self.db.add_project_with_default_color(name)
        self._on_sidebar_change()
        return project

    # ------------------------------------------------------------------
    # Keyboard shortcuts: undo/redo (app-wide) + giving the calendar focus
    # whenever its tab becomes visible (so arrow keys/Delete/Escape --
    # bound directly on the canvas, see CalendarGrid._build_widgets -- work
    # right away without an extra click first).
    # ------------------------------------------------------------------
    def _active_calendar(self):
        """The CalendarGrid for whichever of Timesheet/Template is the
        currently-selected notebook tab, or None while some other tab (a
        panel like Settings/Project/Time Block, or the window isn't fully
        built yet) is showing. Undo/redo only ever acts on a calendar
        that's actually visible."""
        if not hasattr(self, "notebook"):
            return None
        try:
            current = self.notebook.select()
        except tk.TclError:
            return None
        if not current:
            return None
        if hasattr(self, "timesheet_tab") and current == str(self.timesheet_tab):
            return self.calendar
        if hasattr(self, "template_tab") and current == str(self.template_tab):
            return self.template_calendar
        return None

    def _on_tab_changed(self, event=None):
        self._refresh_tab_bar()
        cal = self._active_calendar()
        if cal is not None:
            cal.canvas.focus_set()
        # The Summary tab doesn't live-update while entries change on the
        # other tabs, so refresh its totals every time it becomes visible.
        # (Unlike Timesheet/Template, summary_panel itself IS the tab
        # widget -- there's no separate container Frame to compare against.)
        if hasattr(self, "summary_panel"):
            try:
                current = self.notebook.select()
            except tk.TclError:
                current = None
            if current and current == str(self.summary_panel):
                self.summary_panel.refresh()

    @staticmethod
    def _is_typing_target(widget) -> bool:
        """True while the keyboard focus is on a widget that expects to
        consume Ctrl+Z/Ctrl+Shift+Z itself for normal text editing (even
        though none of our Entry/Combobox/Text widgets actually bind
        anything to those keys today, a text field silently swallowing
        undo/redo instead of the calendar acting on it would be a worse
        surprise than this shortcut occasionally doing nothing)."""
        w = widget
        while w is not None:
            if isinstance(w, (tk.Entry, tk.Text, ttk.Entry, ttk.Combobox, ttk.Spinbox,
                              RoundedCombobox, RoundedEntry)):
                return True
            w = getattr(w, "master", None)
        return False

    def _on_undo_shortcut(self, event=None):
        if self._is_typing_target(self.focus_get()):
            return
        cal = self._active_calendar()
        if cal is not None:
            cal.undo()

    def _on_redo_shortcut(self, event=None):
        if self._is_typing_target(self.focus_get()):
            return
        cal = self._active_calendar()
        if cal is not None:
            cal.redo()

    def _bind_global_shortcuts(self):
        """Ctrl+Z / Ctrl+Shift+Z (and Ctrl+Y) undo/redo the last calendar
        edit -- create, move, resize, delete, or duplicate -- on whichever
        of the Timesheet/Template tabs is currently visible (see
        _active_calendar). Bound once, here, rather than re-bound on every
        theme change: `self.bind_all` registers on Tk's global "all"
        bindtag, which outlives the plain tk widgets that
        _apply_theme_and_rebuild destroys and recreates, and
        _on_undo_shortcut/_on_redo_shortcut look up self.calendar/
        self.template_calendar fresh each time rather than closing over
        them, so this keeps working across rebuilds without re-binding.

        Left/Right/Up/Down and Delete/Backspace are deliberately NOT bound
        here -- see CalendarGrid._build_widgets, which binds them directly
        on each canvas instead, precisely so they never fight with a
        Notebook tab strip's own arrow-key tab-switching or with normal
        text editing in an Entry/Combobox/Text field elsewhere in the app.
        Ctrl+Z doesn't have that conflict (nothing else in this app binds
        it), so a single app-wide binding is simpler and just as safe."""
        self.bind_all("<Control-z>", self._on_undo_shortcut, add="+")
        self.bind_all("<Control-Z>", self._on_undo_shortcut, add="+")
        self.bind_all("<Control-y>", self._on_redo_shortcut, add="+")
        self.bind_all("<Control-Y>", self._on_redo_shortcut, add="+")
        self.bind_all("<Control-Shift-Z>", self._on_redo_shortcut, add="+")
        if sys.platform == "darwin":
            # macOS Tk supports a "Command" modifier the same way other
            # platforms support "Control" -- bind both so the shortcut
            # works with whichever key a Mac user reaches for. Guarded with
            # try/except (rather than an outright platform check alone)
            # since exactly how forgiving a given Tk build is about
            # modifier names it doesn't recognize isn't worth relying on.
            for sequence, handler in (
                ("<Command-z>", self._on_undo_shortcut), ("<Command-Z>", self._on_undo_shortcut),
                ("<Command-Shift-Z>", self._on_redo_shortcut), ("<Command-y>", self._on_redo_shortcut),
            ):
                try:
                    self.bind_all(sequence, handler, add="+")
                except tk.TclError:
                    pass

    # ------------------------------------------------------------------
    # Tab panels (replace what used to be pop-up dialogs -- see _build_body)
    # ------------------------------------------------------------------
    def _register_panel(self, widget, tab_text):
        self.notebook.add(widget, text=tab_text)
        self.notebook.tab(widget, state="hidden")
        self._panels.append(widget)
        self._all_tabs.append((widget, tab_text))

    def _refresh_tab_bar(self):
        """Rebuild the hand-drawn tab strip (see _build_body) from
        self._all_tabs + the notebook's own current state -- one button
        per tab currently in "normal" state (every permanent tab, plus
        whichever single transient panel _show_panel most recently made
        visible, if any), styled Accent for whichever one is selected and
        Secondary for the rest -- the exact same selected/unselected style
        convention SummaryPanel already uses for its Week/Month toggle."""
        try:
            selected = self.notebook.select()
        except tk.TclError:
            selected = None
        for child in self.tab_bar.winfo_children():
            child.destroy()
        for widget, tab_text in self._all_tabs:
            try:
                state = self.notebook.tab(widget, option="state")
            except tk.TclError:
                continue
            if state == "hidden":
                continue
            is_selected = selected is not None and selected == str(widget)
            RoundedButton(
                self.tab_bar, text=tab_text,
                style="Accent.TButton" if is_selected else "Secondary.TButton",
                command=lambda w=widget: self.notebook.select(w),
            ).pack(side="left", padx=(0, 6))

    def _show_panel(self, widget):
        # Only one extra tab is ever shown at a time, alongside "Timesheet".
        for other in self._panels:
            if other is not widget:
                self.notebook.tab(other, state="hidden")
        self.notebook.tab(widget, state="normal")
        self.notebook.select(widget)

    def _hide_panel(self, widget):
        self.notebook.tab(widget, state="hidden")
        self.notebook.select(0)

    def _open_time_block_panel(self, **kwargs):
        kwargs["known_jira_projects"] = self.db.list_known_jira_projects()
        inner_save = kwargs.get("on_save")
        kwargs["on_save"] = lambda result, inner=inner_save: self._save_time_block_and_status(inner, result)
        kwargs["on_fetch_transitions"] = self._fetch_jira_transitions
        self.timeblock_panel.load(**kwargs)
        self._show_panel(self.timeblock_panel)

    def _fetch_jira_transitions(self, issue_key: str, done):
        creds = self._jira_creds()
        if not creds.is_complete():
            self.after(0, lambda: done([]))
            return

        def worker():
            try:
                transitions = jira_client.list_transitions(creds, issue_key)
            except Exception as exc:
                jira_client._log(f"list_transitions {issue_key} failed: {exc}")
                transitions = []
            self.after(0, lambda trans=transitions: done(trans))

        threading.Thread(target=worker, daemon=True).start()

    def _save_time_block_and_status(self, inner_save, result: dict):
        ok = inner_save(result) if inner_save else True
        if ok is False:
            return False
        trans_id = (result.get("jira_transition_id") or "").strip()
        key = (result.get("jira_key") or "").strip()
        if not trans_id or not key:
            return ok
        creds = self._jira_creds()
        if not creds.is_complete():
            self._jira_alert(
                "Jira status",
                "The time block was saved, but Jira isn’t configured so the "
                "ticket status was left as-is.")
            return ok
        try:
            jira_client.transition_issue(creds, key, trans_id)
        except Exception as exc:
            jira_client._log(f"transition_issue {key} failed: {exc}")
            self._jira_alert(
                "Jira status",
                "The time block was saved, but Jira didn’t accept the status "
                f"change:\n\n{exc}")
            return ok
        act = self.db.get_activity_by_jira_key(key)
        if act is not None:
            to_name = (result.get("jira_transition_to_name") or "").strip()
            to_cat = (result.get("jira_transition_to_category") or "").strip() or None
            if to_name:
                act.jira_status = to_name
            if to_cat:
                act.jira_status_category = to_cat
            self.db.update_activity(act)
            self._on_sidebar_change()
        return ok

    def _open_duplicate_panel(self, **kwargs):
        self.duplicate_panel.load(**kwargs)
        self._show_panel(self.duplicate_panel)

    def _open_activity_panel(self, activity, on_save, on_delete=None):
        self.activity_panel.load(activity, on_save, on_delete)
        self._show_panel(self.activity_panel)

    def _open_project_panel(self, project, on_save, on_delete=None):
        self.project_panel.load(project, on_save, on_delete)
        self._show_panel(self.project_panel)

    # ------------------------------------------------------------------
    # Theme (see app/theme.py's THEMES for the twenty curated choices, plus
    # the "custom" id built live from the user's own picked colors)
    # ------------------------------------------------------------------
    def _select_theme(self, theme_id: str):
        """Persist and apply a theme chosen from the Settings picker (see
        panels.SettingsPanel). Safe to call even when `theme_id` is the
        theme that's already active -- the rebuild is a little wasted work
        in that case, but simpler and safer than trying to special-case a
        no-op, and it's on the Settings-Save path anyway, not called on
        every keystroke."""
        self.db.set_setting("theme_mode", theme_id)
        self._apply_theme_and_rebuild(theme_id)

    def _apply_theme_and_rebuild(self, theme_id: str):
        theme.set_theme(theme_id)
        self.family = theme.apply_theme(self)
        self.configure(bg=theme.APP_BG)

        # Plain tk widgets (Frame/Label/Canvas/Menu, as opposed to ttk ones)
        # cache their colors at construction time and won't pick up the new
        # palette on their own -- rebuild them from scratch. ttk widgets
        # (buttons, entries, comboboxes...) already re-rendered the instant
        # apply_theme() re-configured their styles above.
        week_start = self.calendar.week_start if hasattr(self, "calendar") else None
        # A running timer would otherwise be silently lost here -- capture
        # its state (see TimerBar.get_state) before the old TimerBar is
        # destroyed below, and hand it to the new one so counting continues
        # uninterrupted right through the toggle.
        timer_state = self.timer_bar.get_state() if hasattr(self, "timer_bar") else None
        for child in list(self.winfo_children()):
            child.destroy()

        self._build_menu()
        self._build_top_bar(initial_timer_state=timer_state)
        self._build_body(initial_week_start=week_start)

    # ------------------------------------------------------------------
    def _load_settings_panel(self):
        """(Re)populate the Settings tab from the db's current values. Used
        to be called only from _open_settings_dialog, right before that
        opened the tab (back when Settings was a hide-until-opened panel
        like Time Block/Duplicate/etc.) -- now that it's a permanent tab
        built once in _build_body, this runs once at build time instead,
        and again from _open_settings_dialog below just to stay safe if
        anything reaches that path."""
        display_name = self.db.get_setting("jira_display_name", "") or ""
        current_theme_id = theme.get_theme_id()
        current_work_start_hour = config.START_HOUR
        current_work_end_hour = config.END_HOUR
        current_show_weekends = config.SHOW_WEEKENDS
        current_show_timer_bar = self.show_timer_bar
        current_header_style = self.header_style

        def on_save(new_display_name, new_theme_id,
                    new_work_start_hour, new_work_end_hour, new_show_weekends,
                    new_show_timer_bar, new_header_style, jira_fields=None):
            self.db.set_setting("jira_display_name", new_display_name)
            self.db.set_setting("work_start_hour", str(new_work_start_hour))
            self.db.set_setting("work_end_hour", str(new_work_end_hour))
            self.db.set_setting("show_weekends", "1" if new_show_weekends else "0")
            self.db.set_setting("show_timer_bar", "1" if new_show_timer_bar else "0")
            self.db.set_setting("header_style", new_header_style)
            self._persist_jira_fields(jira_fields or {})

            # Always persist the Custom palette's current seed colors,
            # whether or not "custom" is the theme actually being saved --
            # SettingsPanel keeps theme.get_custom_seeds() in sync with
            # whatever the user last picked in the Custom color row, so
            # this just makes sure it's remembered next time, even if they
            # ended up saving a different preset instead.
            custom_seeds = theme.get_custom_seeds()
            self.db.set_setting("custom_theme_app_bg", custom_seeds["app_bg"])
            self.db.set_setting("custom_theme_panel_bg", custom_seeds["panel_bg"])
            self.db.set_setting("custom_theme_text", custom_seeds["text_primary"])
            self.db.set_setting("custom_theme_accent", custom_seeds["accent"])
            self.db.set_setting("glass_alpha", f"{theme.get_glass_alpha():.2f}")

            hours_changed = (new_work_start_hour != current_work_start_hour
                              or new_work_end_hour != current_work_end_hour
                              or new_show_weekends != current_show_weekends)
            if hours_changed:
                config.set_work_hours(new_work_start_hour, new_work_end_hour)
                config.set_show_weekends(new_show_weekends)

            # Same idea as hours_changed above, for the two Heading/Timer
            # bar toggles: mutate the instance attributes _build_top_bar
            # actually reads *before* the rebuild below,
            # same as config.set_work_hours/set_show_weekends just did for
            # their own globals.
            chrome_changed = (new_show_timer_bar != current_show_timer_bar
                               or new_header_style != current_header_style)
            if chrome_changed:
                self.show_timer_bar = new_show_timer_bar
                self.header_style = new_header_style

            # A theme change, a work-hours/weekend change, and a Heading/
            # Timer-bar change all need the same full destroy-and-rebuild
            # (_select_theme already triggers one for its own reason:
            # plain tk widgets cache their colors at construction time).
            # The calendar's visible-hours grid height and day count, and
            # the header/timer-bar widgets themselves, are all baked into
            # their widgets at construction exactly the same way, so reuse
            # the same path here rather than a second, separate rebuild
            # routine. Deferred via after(0, ...) since this callback runs
            # from the very Settings tab the rebuild is about to destroy.
            if new_theme_id != current_theme_id or hours_changed or chrome_changed:
                self.after(0, lambda: self._select_theme(new_theme_id))
            else:
                theme.apply_window_opacity(self)

        self.settings_panel.load(
            display_name, current_theme_id, current_work_start_hour,
            current_work_end_hour, current_show_weekends,
            current_show_timer_bar, current_header_style, on_save,
            jira_site_url=self.db.get_setting("jira_site_url", "") or "",
            jira_email=self.db.get_setting("jira_email", "") or "",
            jira_api_token=self.db.get_setting("jira_api_token", "") or "",
            jira_project_key=self.db.get_setting("jira_project_key", "QDM") or "QDM",
            on_test_jira=self._test_jira_connection,
            on_sync_jira=self._sync_qdms_from_jira_fields,
        )

    def _open_settings_dialog(self):
        # Settings is a permanent tab now (see _build_body) -- this just
        # jumps the notebook to it, still wired up from the Settings/View
        # menu's "Jira Export Settings…"/"Theme…" entries now that the
        # header button that used to call this is gone.
        self._load_settings_panel()
        self.notebook.select(self.settings_panel)

    def _open_backup_dialog(self):
        self._show_panel(self.backup_panel)

    def _backup_data(self, path: str):
        try:
            self.db.backup_to(path)
        except Exception as exc:
            messagebox.showwarning("Backup Failed", f"Could not write the backup file:\n\n{exc}")
            return
        messagebox.showinfo("Backup Complete", f"Your data was backed up to:\n\n{path}")

    def _restore_data(self, path: str):
        if self.timer_bar.is_running():
            messagebox.showwarning(
                "Timer Running",
                "Stop the running timer first, so its time isn't lost, then try "
                "restoring again.")
            return
        try:
            self.db.restore_from(path)
        except Exception as exc:
            messagebox.showwarning("Restore Failed", f"Could not restore from that file:\n\n{exc}")
            return
        # Restoring can change literally everything the UI shows -- every
        # project/activity/time block/template entry, plus every setting
        # (display name, defaults, and the theme itself) -- so the safest
        # way to reflect it is the same full destroy-and-rebuild
        # _select_theme() already uses for a theme change. Deferred via
        # after(0, ...) for the same reason as that theme-change path: this
        # callback was invoked from the very Settings tab the rebuild is
        # about to destroy, so let the current event finish first.
        self.after(0, self._finish_restore)

    def _finish_restore(self):
        restored_theme_id = self.db.get_setting("theme_mode", theme.DEFAULT_THEME_ID) or theme.DEFAULT_THEME_ID
        self._apply_theme_and_rebuild(restored_theme_id)
        messagebox.showinfo("Restore Complete", "Your data has been restored.")

    def _open_export_dialog(self):
        def on_export(start_date, end_date):
            self._do_export(start_date, end_date)

        def on_push(start_date, end_date):
            self._push_hours_to_jira(start_date, end_date)

        self.export_panel.load(self.calendar.week_start, on_export, on_push=on_push)
        self._show_panel(self.export_panel)

    def _persist_jira_fields(self, fields: dict):
        if not fields:
            return
        if "site_url" in fields:
            self.db.set_setting(
                "jira_site_url",
                jira_client.normalize_site_url(fields.get("site_url") or ""),
            )
        if "email" in fields:
            self.db.set_setting("jira_email", fields.get("email") or "")
        if "api_token" in fields and (fields.get("api_token") or "").strip():
            self.db.set_setting("jira_api_token", fields["api_token"].strip())
        if "project_key" in fields:
            self.db.set_setting("jira_project_key", (fields.get("project_key") or "QDM").strip() or "QDM")

    def _jira_creds(self, fields: Optional[dict] = None) -> jira_client.JiraCredentials:
        if fields:
            self._persist_jira_fields(fields)
        return jira_client.credentials_from_settings(self.db.get_setting)

    def _jira_alert(self, title: str, message: str, kind: str = "warning"):
        show = messagebox.showwarning if kind == "warning" else messagebox.showinfo
        show(title, message, parent=self)

    def _set_jira_busy(self, busy: bool, status: str = "", *, quiet: bool = False):
        if not quiet:
            try:
                self.config(cursor="watch" if busy else "")
            except tk.TclError:
                pass
        if hasattr(self, "settings_panel"):
            try:
                if status:
                    self.settings_panel.jira_status_label.config(text=status)
                elif not busy:
                    pass
            except tk.TclError:
                pass

    def _test_jira_connection(self, fields: dict):
        creds = self._jira_creds(fields)
        if not creds.is_complete():
            self._jira_alert(
                "Jira",
                "Need a site URL and API token first.\n\n"
                "Create a token at https://id.atlassian.com/manage-profile/security/api-tokens "
                "and paste it in Settings, or set QUASAR_JIRA_TOKEN.")
            return

        self._set_jira_busy(True, "Testing connection…")

        def worker():
            try:
                name = jira_client.test_connection(creds)
            except Exception as exc:
                err = str(exc)
                jira_client._log(f"test_connection failed: {type(exc).__name__}: {exc}")
                self.after(0, lambda err=err: self._jira_job_failed("Jira", err))
                return
            self.after(0, lambda: self._jira_test_ok(name))

        threading.Thread(target=worker, daemon=True).start()

    def _jira_test_ok(self, name: str):
        self._set_jira_busy(False, f"Connected as {name}.")
        self._jira_alert(
            "Jira",
            f"Connected as {name}. Sync QDMs only reads your assigned issues — "
            "it does not change anything in Jira.",
            kind="info")

    def _jira_job_failed(self, title: str, err: str, quiet: bool = False):
        self._jira_sync_in_flight = False
        self._set_jira_busy(False, err, quiet=quiet)
        if quiet:
            jira_client._log(f"startup sync failed: {err}")
            return
        self._jira_alert(title, err)

    def _sync_qdms_on_startup(self):
        """Refresh assigned QDMs as soon as the window is up.

        Quiet on purpose: no dialogs, no watch cursor, and a missing
        token is skipped so a first launch without Jira configured
        still opens normally. Manual Sync (sidebar / Settings / menu)
        stays noisy so you still get the full result when you ask.
        """
        self._sync_qdms_from_jira_fields(None, quiet=True)

    def _sync_qdms_from_jira(self):
        self._sync_qdms_from_jira_fields(None)

    def _sync_qdms_from_jira_fields(self, fields: Optional[dict], *, quiet: bool = False):
        if self._jira_sync_in_flight:
            return
        creds = self._jira_creds(fields)
        if not creds.is_complete():
            if quiet:
                return
            self._jira_alert(
                "Sync QDMs",
                "Add your Jira site URL and API token in Settings first "
                "(or set QUASAR_JIRA_URL / QUASAR_JIRA_TOKEN), then try again.")
            self._open_settings_dialog()
            return

        self._jira_sync_in_flight = True
        self._set_jira_busy(True, "Fetching your assigned QDMs from Jira…", quiet=quiet)

        def worker():
            # HTTP only on this thread. SQLite must stay on the Tk main
            # thread — using self.db here used to raise ProgrammingError
            # and die silently, which looked like "Sync does nothing".
            try:
                fetched = jira_client.fetch_issues(creds)
            except Exception as exc:
                err = str(exc)
                jira_client._log(
                    f"fetch_issues failed: {type(exc).__name__}: {exc}\n"
                    f"{traceback.format_exc()}")
                self.after(
                    0,
                    lambda err=err, quiet=quiet: self._jira_job_failed(
                        "Sync QDMs", err, quiet=quiet))
                return
            self.after(
                0,
                lambda fetched=fetched, quiet=quiet: self._apply_jira_sync(
                    fetched, quiet=quiet))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_jira_sync(self, fetched, quiet: bool = False):
        try:
            issues = fetched.issues if isinstance(fetched, jira_client.FetchResult) else fetched
            open_capped = bool(getattr(fetched, "open_capped", False))
            closed_capped = bool(getattr(fetched, "closed_capped", False))
            result = jira_sync.sync_issues_into_db(
                self.db, issues,
                archive_open_missing=not open_capped,
                archive_closed_missing=not closed_capped,
                open_capped=open_capped,
                closed_capped=closed_capped,
            )
            self._on_jira_sync_done(result, quiet=quiet)
        except Exception as exc:
            jira_client._log(f"sync_issues_into_db failed: {exc}\n{traceback.format_exc()}")
            self._jira_job_failed("Sync QDMs", str(exc), quiet=quiet)

    def _on_jira_sync_done(self, result: jira_sync.SyncResult, quiet: bool = False):
        self._jira_sync_in_flight = False
        self._on_sidebar_change()
        self._set_jira_busy(False, f"Fetched {result.fetched} assigned QDM(s).", quiet=quiet)
        if quiet:
            return
        if result.fetched == 0:
            self._jira_alert(
                "Sync QDMs",
                "Jira returned no open issues assigned to this account.\n\n"
                "Nothing was changed in Jira — this is only a read.\n\n"
                "If you expected a list, check the Project key (QDM) and that "
                "the token belongs to the same Atlassian account those tickets "
                "are assigned to.",
                kind="info")
            return
        cap_note = ""
        if result.open_capped or result.closed_capped:
            bits = []
            if result.open_capped:
                bits.append(f"{jira_client._MAX_OPEN_ISSUES} open")
            if result.closed_capped:
                bits.append(f"{jira_client._MAX_CLOSED_ISSUES} closed")
            cap_note = (
                f"\n\nStopped at {' / '.join(bits)} so the sidebar stays usable "
                "(newest assigned first). Older assigned tickets may not appear."
            )
        self._jira_alert(
            "Sync QDMs",
            f"Fetched {result.fetched} issue(s) assigned to you.\n"
            f"Added {result.created} QDM(s) to the sidebar, updated {result.updated}, "
            f"created {result.projects_created} project group(s).\n\n"
            "Only QDMs with a ticket assigned to you are listed. "
            "Nothing was written to Jira. Groups are collapsed — click a "
            "triangle in the left sidebar to see that group's QDMs."
            f"{cap_note}",
            kind="info")

    def _push_hours_to_jira(self, start_date: str, end_date: str):
        creds = self._jira_creds()
        if not creds.is_complete():
            self._jira_alert(
                "Push to Jira",
                "Add your Jira site URL and API token in Settings first, then try again.")
            self._open_settings_dialog()
            return
        entries = self.db.list_time_entries_between(start_date, end_date)
        pending = self.db.list_pending_worklog_deletes(start_date, end_date)
        if not entries and not pending:
            self._jira_alert("Push to Jira", "There are no time blocks in that date range.", kind="info")
            return

        plan = jira_sync.plan_worklog_push(entries, pending)
        if not plan.has_jira_writes():
            if not entries:
                self._jira_alert(
                    "Push to Jira", "There are no time blocks in that date range.", kind="info")
            elif plan.skipped and not plan.unchanged and not plan.too_short:
                self._jira_alert(
                    "Push to Jira",
                    "None of those time blocks have a Jira Issue Key, so nothing "
                    "would be logged.\n\nCSV export uses the same rule — assign a "
                    "QDM (issue key) on each block first.",
                    kind="info")
            else:
                self._jira_alert(
                    "Push to Jira",
                    "Nothing to send — every block in that range is already "
                    "up to date in Jira, or is shorter than 1 minute.\n\n"
                    "Untouched hours are skipped.",
                    kind="info")
            return

        lines = [
            f"Log hours to Jira for {start_date} – {end_date}?",
            "",
        ]
        if plan.create:
            lines.append(f"  • {len(plan.create)} new worklog(s) will be added")
        if plan.update:
            lines.append(
                f"  • {len(plan.update)} existing worklog(s) will be updated "
                "(description, time, or day changed)")
        if plan.move:
            lines.append(
                f"  • {len(plan.move)} worklog(s) will be moved to a different QDM")
        if plan.remove_key:
            lines.append(
                f"  • {len(plan.remove_key)} worklog(s) will be removed "
                "(block no longer has a Jira Issue Key)")
        if plan.pending_deletes:
            lines.append(
                f"  • {len(plan.pending_deletes)} deleted block(s) will be "
                "removed from Jira")
        if plan.unchanged:
            lines.append(
                f"  • {len(plan.unchanged)} unchanged block(s) will be skipped")
        if plan.skipped:
            lines.append(
                f"  • {len(plan.skipped)} block(s) skipped (no Jira Issue Key)")
        if plan.too_short:
            lines.append(
                f"  • {len(plan.too_short)} block(s) shorter than 1 minute "
                "cannot be logged")
        lines += [
            "",
            "Only real changes are sent (new blocks, edits, and deletions).",
            "It does not change ticket status, assignee, description, or anything else.",
        ]
        if not messagebox.askyesno("Push to Jira", "\n".join(lines), parent=self):
            return

        self._set_jira_busy(True, "Logging hours to Jira…")

        def worker():
            # Own connection: this thread can't use the window's sqlite conn.
            db = Database(self.db.path)
            try:
                result = jira_sync.push_worklogs(
                    db, creds, entries, start_date, end_date)
            except Exception as exc:
                err = str(exc)
                jira_client._log(f"push_worklogs failed: {exc}\n{traceback.format_exc()}")
                self.after(0, lambda err=err: self._jira_job_failed("Push to Jira", err))
                return
            finally:
                db.close()
            self.after(0, lambda: self._on_jira_push_done(result))

        threading.Thread(target=worker, daemon=True).start()

    def _on_jira_push_done(self, result: jira_sync.PushResult):
        self._set_jira_busy(False)
        self.calendar.refresh()
        parts = []
        if result.created:
            parts.append(f"logged {result.created} new worklog(s)")
        if result.updated:
            parts.append(f"updated {result.updated}")
        if result.deleted:
            parts.append(f"removed {result.deleted}")
        if result.unchanged:
            parts.append(f"skipped {result.unchanged} unchanged")
        if parts:
            msg = "Jira: " + ", ".join(parts) + "."
        else:
            msg = "Nothing was written to Jira."
        if result.skipped:
            msg += (f"\n\n{len(result.skipped)} block(s) skipped — no Jira Issue Key:\n" +
                    "\n".join(f"  • {e.activity_name} ({e.date} {e.start_time}–{e.end_time})"
                              for e in result.skipped[:8]))
            if len(result.skipped) > 8:
                msg += f"\n  …and {len(result.skipped) - 8} more."
        if result.failed:
            msg += "\n\nFailed:\n" + "\n".join(
                f"  • {item.key}: {item.detail}" for item in result.failed[:8])
        self._jira_alert("Push to Jira", msg, kind="info")

    def _do_export(self, start_date: str, end_date: str):
        entries = self.db.list_time_entries_between(start_date, end_date)
        if not entries:
            messagebox.showinfo("Nothing to export", "There are no time blocks in that date range.")
            return

        default_name = f"jira_worklog_{start_date}_to_{end_date}.csv"
        from tkinter import filedialog
        filepath = filedialog.asksaveasfilename(
            title="Save Jira CSV export",
            initialfile=default_name,
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not filepath:
            return

        display_name = self.db.get_setting("jira_display_name", "") or ""
        if not display_name:
            proceed = messagebox.askyesno(
                "No Display Name set",
                "You haven't set a Display Name in Settings → Jira Export Settings.\n"
                "Exported rows will have a blank Display Name. Continue anyway?",
            )
            if not proceed:
                return

        written, skipped = export_entries(entries, filepath, display_name)

        msg = f"Exported {written} worklog row(s) to:\n{filepath}"
        if skipped:
            msg += (f"\n\n{len(skipped)} time block(s) were skipped because they have no "
                    f"Jira Issue Key assigned:\n" +
                    "\n".join(f"  • {e.activity_name} ({e.date} {e.start_time}-{e.end_time})"
                              for e in skipped[:10]))
            if len(skipped) > 10:
                msg += f"\n  …and {len(skipped) - 10} more."
        messagebox.showinfo("Export complete", msg)

    def _show_help(self):
        messagebox.showinfo(
            "How to use",
            "• Timer (top of the window): pick an activity and click Start "
            "Timer. Click Stop Timer when you're done and it logs a time "
            "block for today automatically, rounded to the nearest 15 "
            "minutes.\n"
            "• Drag on an empty part of the grid to create a time block.\n"
            "• Drag a block's top/bottom edge to resize it.\n"
            "• Drag the middle of a block to move it (even to another day).\n"
            "• Right-click a block to edit, duplicate, or delete it.\n"
            "• Ctrl+click a block to instantly duplicate it into the same slot.\n"
            "• Overlapping blocks are allowed -- they're shown side by side "
            "instead of one hiding the other.\n"
            "• Click an activity in the sidebar to \"arm\" it, then click an "
            "empty slot to instantly place it (Esc cancels).\n"
            "• Double-click or right-click an activity to edit/delete it.\n"
            "• \"+ Project\" groups activities into a collapsible project, "
            "which sets the color every one of its activities' time blocks "
            "shows -- click the arrow to collapse/expand, or right-click a "
            "project to edit/delete it.\n"
            "• File → Sync QDMs from Jira… pulls open issues into the sidebar "
            "(needs a token in Settings). File → Push hours to Jira… logs "
            "the week's blocks as worklogs on those issues — CSV export is "
            "still there as a fallback.\n\n"
            "Keyboard shortcuts (click the calendar first so it has focus):\n"
            "• Click a block (without dragging) to select it -- it gets a "
            "highlighted outline.\n"
            "• Delete or Backspace: remove the selected block (same confirmation "
            "as the right-click menu's Delete).\n"
            "• Left/Right arrow: with a block selected, move it to the previous/"
            "next day; with nothing selected, go to the previous/next week.\n"
            "• Up/Down arrow: move the selected block's time a slot earlier/"
            "later.\n"
            "• Esc: cancel a drag in progress, un-arm an activity, or deselect "
            "the selected block.\n"
            "• Ctrl+Z (Cmd+Z on Mac): undo the last create/move/resize/delete/"
            "duplicate. Ctrl+Shift+Z or Ctrl+Y (Cmd+Shift+Z on Mac): redo.",
        )

    def _on_close(self):
        if self.timer_bar.is_running():
            answer = messagebox.askyesnocancel(
                "Timer is still running",
                "The timer is still running.\n\n"
                "Yes = stop it and log the time so far, then close\n"
                "No = close without logging it (the time is discarded)\n"
                "Cancel = don't close, go back to the timer",
            )
            if answer is None:
                return
            if answer:
                self.timer_bar.stop(prompt_notes=False)
        self.db.close()
        self.destroy()
