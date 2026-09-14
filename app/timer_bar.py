"""Header timer bar: pick an activity, click Start, and it counts up in
real time; click Stop and it logs a time block for *today* automatically,
with its duration rounded to the nearest 15 minutes. This is the fast path
for "what am I doing right now" -- no dragging on the grid, no picking
exact start/end times by hand.

Lives in the top toolbar (see MainWindow._build_top_bar) rather than
either sidebar, because it always logs against today's real date
regardless of which tab (Timesheet/Template) or which week is currently
on screen. Packed on the same row as the title so it doesn't spend a
full-width card of padding on a laptop screen.
"""
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import messagebox
from typing import Callable, List, Optional

from . import theme
from .db import Database
from .models import Activity, TimeEntry
from .time_rounding import round_duration_minutes
from .widgets import RoundedButton, RoundedCombobox


class TimerBar(tk.Frame):
    def __init__(self, master, db: Database, get_activities: Callable[[], List[Activity]],
                 on_saved: Callable[[TimeEntry], None], family: str,
                 initial_state: Optional[dict] = None, **kwargs):
        bg = kwargs.pop("bg", None) or theme.PANEL_BG
        kwargs.setdefault("bg", bg)
        super().__init__(master, **kwargs)
        self.db = db
        self.get_activities = get_activities
        self.on_saved = on_saved
        self.family = family
        self._activities: List[Activity] = []
        self._activities_by_name = {}
        self.start_dt: Optional[datetime] = None
        self._tick_job: Optional[str] = None

        inner = tk.Frame(self, bg=bg)
        inner.pack(fill="x")

        self.activity_var = tk.StringVar()
        self.activity_combo = RoundedCombobox(inner, textvariable=self.activity_var,
                                               state="readonly", width=22, bg=bg)
        self.activity_combo.pack(side="left", padx=(0, 8))

        self.toggle_btn = RoundedButton(inner, text="Start Timer", style="Accent.TButton",
                                         command=self._toggle, bg=bg)
        self.toggle_btn.pack(side="left")

        # A small drawn dot rather than a colored emoji/glyph for the
        # "recording" indicator -- same reasoning as the logo mark and the
        # folder disclosure arrow elsewhere in this app: a couple of drawn
        # pixels render identically everywhere, a font glyph might not.
        self.dot = tk.Canvas(inner, width=10, height=10, bg=bg, highlightthickness=0)
        self.dot.pack(side="left", padx=(10, 4))

        self.elapsed_label = tk.Label(inner, text="", font=(self.family, 10, "bold"),
                                       bg=bg, fg=theme.TEXT_PRIMARY, width=8,
                                       anchor="w")
        self.elapsed_label.pack(side="left")

        self.status_label = tk.Label(inner, text="", font=(self.family, 9),
                                      bg=bg, fg=theme.TEXT_SECONDARY)
        self.status_label.pack(side="left", padx=(8, 0))

        self.refresh_activities()

        if initial_state is not None:
            self._resume(initial_state)
        else:
            self._render_idle()

    # ------------------------------------------------------------------
    def refresh_activities(self):
        self._activities = self.get_activities()
        self._activities_by_name = {a.name: a for a in self._activities}
        self.activity_combo.config(values=[a.name for a in self._activities])
        # A placeholder rather than leaving the combobox showing nothing
        # at all -- an empty readonly Combobox is easy to mistake for an
        # inert/disabled control instead of one that needs a click. Only
        # set when nothing has been picked yet (activity_var starts as ""
        # and this only runs once as a result); a real selection is never
        # overwritten, including across later refreshes. "Select QDM"
        # itself is never a real activity name, so _selected_activity()
        # below correctly treats it the same as the old blank state --
        # _start()'s existing "Choose an activity" guard already covers
        # trying to start the timer without a real one picked.
        if not self.activity_var.get():
            self.activity_var.set("Select QDM")

    def _selected_activity(self) -> Optional[Activity]:
        return self._activities_by_name.get(self.activity_var.get())

    # ------------------------------------------------------------------
    def is_running(self) -> bool:
        return self.start_dt is not None

    def get_state(self) -> Optional[dict]:
        """Captures enough to resume across a theme-toggle rebuild, which
        destroys and recreates every widget in the window (this one
        included) -- see MainWindow._apply_theme_and_rebuild. Returns None
        if no timer is running, i.e. there's nothing to resume."""
        if self.start_dt is None:
            return None
        return {"activity_name": self.activity_var.get(), "start_dt": self.start_dt}

    def _resume(self, state: dict):
        self.start_dt = state["start_dt"]
        name = state.get("activity_name") or ""
        if name in self._activities_by_name:
            self.activity_var.set(name)
            self.activity_combo.set(name)
        self.activity_combo.config(state="disabled")
        self._render_running()
        self._tick()

    # ------------------------------------------------------------------
    def _toggle(self):
        if self.is_running():
            self._stop()
        else:
            self._start()

    def stop(self):
        """Public entry point for stopping the timer from outside this
        widget (e.g. MainWindow._on_close asking to log time-so-far before
        the app exits)."""
        if self.is_running():
            self._stop()

    def _start(self):
        if self._selected_activity() is None:
            messagebox.showwarning("Choose an activity", "Pick an activity before starting the timer.")
            return
        self.status_label.config(text="")
        self.start_dt = datetime.now()
        self.activity_combo.config(state="disabled")
        self._render_running()
        self._tick()

    def _stop(self):
        assert self.start_dt is not None
        start_dt = self.start_dt
        end_dt = datetime.now()
        act = self._selected_activity()

        self.start_dt = None
        if self._tick_job is not None:
            self.after_cancel(self._tick_job)
            self._tick_job = None
        self.activity_combo.config(state="readonly")
        self._render_idle()

        elapsed_minutes = (end_dt - start_dt).total_seconds() / 60
        duration = round_duration_minutes(elapsed_minutes)
        if act is None or duration <= 0:
            self.status_label.config(text="Timer stopped -- nothing logged.")
            return

        date_str = start_dt.date().isoformat()
        start_str = start_dt.strftime("%H:%M")
        end_str = (start_dt + timedelta(minutes=duration)).strftime("%H:%M")

        # Overlapping an existing block is fine -- the calendar renders
        # overlapping blocks side by side rather than rejecting them (see
        # CalendarGrid._layout_day_entries), so there's no need to ask
        # first here either.
        entry = TimeEntry(None, act.id, act.name, act.jira_key, act.color, date_str,
                           start_str, end_str, "", act.jira_project, act.issue_type)
        self.db.add_time_entry(entry)
        self.status_label.config(text=f"Logged {duration} min to {act.name}.")
        self.on_saved(entry)

    # ------------------------------------------------------------------
    def _render_idle(self):
        self.toggle_btn.config(text="Start Timer", style="Accent.TButton")
        self.dot.delete("all")
        self.elapsed_label.config(text="")

    def _render_running(self):
        self.toggle_btn.config(text="Stop Timer", style="Danger.TButton")
        self.dot.delete("all")
        self.dot.create_oval(0, 0, 10, 10, fill=theme.DANGER, outline="")

    def _tick(self):
        if self.start_dt is None:
            return
        total_seconds = int((datetime.now() - self.start_dt).total_seconds())
        h, rem = divmod(total_seconds, 3600)
        m, s = divmod(rem, 60)
        self.elapsed_label.config(text=f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}")
        self._tick_job = self.after(1000, self._tick)
