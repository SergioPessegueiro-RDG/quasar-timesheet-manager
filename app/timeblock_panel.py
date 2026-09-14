"""
Embedded (non-popup) Time Block editor.

This used to be a separate pop-up window (a tk.Toplevel). On some macOS/Tk
combinations, pop-up windows can ignore explicit on-screen positioning
entirely and open wherever the window manager feels like -- no amount of
centering code fixes that, because the OS repositions the window after Tk
already placed it. The only fully reliable fix is to not open a separate
window at all: this panel lives inside the main window itself, shown as a
second tab ("Time Block") that appears next to the "Timesheet" tab only
while a block is being added or edited, and disappears again afterwards.
"""
import tkinter as tk
from tkinter import simpledialog, ttk
from typing import Callable, List, Optional

from . import config, theme
from .jira_client import preferred_close_transition
from .models import Activity
from .widgets import RoundedButton, RoundedCombobox, ScrollArea, show_saved_toast

# Shown at the end of the Jira Project dropdown as a way to add a value
# that isn't in the known-projects list yet (see TimeBlockPanel.load's
# known_jira_projects param) rather than allowing free text that's easy
# to typo.
_NEW_JIRA_PROJECT_OPTION = "+ New Project…"
_KEEP_JIRA_STATUS = "Don't change status"


def activity_matching_jira_key(activities: List[Activity], typed: str) -> Optional[Activity]:
    """Find the QDM whose Jira Issue Key matches what was typed in the
    number-only field (with or without the QDM- prefix). Exact match only,
    so typing "12" does not steal "QDM-123"."""
    typed = (typed or "").strip()
    if not typed:
        return None
    full = config.jira_key_from_number(typed)
    if not full:
        return None
    want = full.strip().upper()
    want_num = config.jira_key_number(full).strip().upper()
    for activity in activities:
        stored = (activity.jira_key or "").strip()
        if not stored:
            continue
        if stored.upper() == want:
            return activity
        if config.jira_key_number(stored).strip().upper() == want_num:
            return activity
    return None


def activity_matching_jira_project(activities: List[Activity], project: str) -> Optional[Activity]:
    """If exactly one QDM uses this Jira Project, return it -- used when
    the project dropdown is the thing the user changed and we can still
    pick a QDM unambiguously. Many QDMs share one Jira project, so this
    returns None whenever more than one (or zero) match."""
    needle = (project or "").strip().lower()
    if not needle or project == _NEW_JIRA_PROJECT_OPTION:
        return None
    matches = [
        activity for activity in activities
        if (activity.jira_project or "").strip().lower() == needle
    ]
    if len(matches) == 1:
        return matches[0]
    return None


class TimeBlockPanel(tk.Frame):
    def __init__(self, master, family: str, on_close: Callable[[], None]):
        super().__init__(master, bg=theme.PANEL_BG)
        self.family = family
        self.on_close = on_close
        self.on_save: Optional[Callable[[dict], bool]] = None
        self.on_delete: Optional[Callable[[], None]] = None
        self.on_cancel: Optional[Callable[[], None]] = None
        self._require_notes = False
        self.on_fetch_transitions: Optional[Callable] = None
        self._transitions = []
        self._transition_by_label = {}
        self._status_fetch_job = None
        self._status_fetch_seq = 0
        self.activities: List[Activity] = []
        self.activities_by_id = {}
        self.day_labels: List[str] = []
        self.day_key_by_label = {}
        self.day_label_by_key = {}
        self.time_options: List[str] = []
        self.known_jira_projects: List[str] = []
        self._previous_jira_project = ""
        self._syncing = False

        # Wrapped in a borderless ScrollArea (see panels._scroll_body's
        # docstring for the same rationale) so this panel's Save/Cancel/
        # Delete row is always reachable even on a shorter window.
        self._scroll = ScrollArea(self, bg=theme.PANEL_BG, outline=False, pad=0)
        self._scroll.pack(fill="both", expand=True)
        outer = tk.Frame(self._scroll.content, bg=theme.PANEL_BG)
        outer.pack(fill="both", expand=True, padx=28, pady=24)

        self.heading = tk.Label(outer, text="Time Block", font=(self.family, 14, "bold"),
                                 bg=theme.PANEL_BG, fg=theme.TEXT_PRIMARY)
        self.heading.pack(anchor="w", pady=(0, 16))

        frm = ttk.Frame(outer)
        frm.pack(anchor="w")

        row = 0
        ttk.Label(frm, text="QDM").grid(row=row, column=0, sticky="w", pady=4)
        self.activity_var = tk.StringVar()
        self.activity_combo = RoundedCombobox(frm, textvariable=self.activity_var,
                                               state="readonly", width=30)
        self.activity_combo.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
        self.activity_combo.bind("<<ComboboxSelected>>", self._on_activity_changed)
        self.activity_combo.bind("<Return>", lambda e: self._save())
        self.activity_var.trace_add("write", lambda *_: self._on_activity_changed())
        row += 1

        ttk.Label(frm, text="Jira Issue Key").grid(row=row, column=0, sticky="w", pady=4)
        # Every key in this app starts with the same fixed "QDM-" prefix
        # (see app/config.py's JIRA_KEY_PREFIX), so this only asks for the
        # number after it -- the static "QDM-" label makes what's being
        # typed (and what the full key will be) obvious at a glance.
        key_row = tk.Frame(frm, bg=theme.PANEL_BG)
        key_row.grid(row=row, column=1, columnspan=2, sticky="w", pady=4)
        tk.Label(key_row, text=config.JIRA_KEY_PREFIX, font=(self.family, 10, "bold"),
                 bg=theme.PANEL_BG, fg=theme.TEXT_SECONDARY).pack(side="left")
        self.jira_key_number_var = tk.StringVar()
        jira_key_entry = ttk.Entry(key_row, textvariable=self.jira_key_number_var, width=10)
        jira_key_entry.pack(side="left")
        jira_key_entry.bind("<Return>", lambda e: self._save())
        self.jira_key_number_var.trace_add("write", lambda *_: self._on_jira_key_changed())
        row += 1

        # Labeled "Jira Project" (not "Project" or "QDM") so it isn't
        # confused with the QDM chosen above -- this is specifically the
        # Jira project this block's CSV export row goes under. A readonly
        # dropdown of Jira projects actually used somewhere in this
        # database (see Database.list_known_jira_projects), plus an
        # explicit way to add a new one -- not free text, since this is
        # almost always the same one value ("Quasar Delivery Management")
        # and a typo here would silently break that row's export.
        ttk.Label(frm, text="Jira Project").grid(row=row, column=0, sticky="w", pady=4)
        self.jira_project_var = tk.StringVar()
        self.jira_project_combo = RoundedCombobox(frm, textvariable=self.jira_project_var,
                                                   state="readonly", width=30)
        self.jira_project_combo.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
        self.jira_project_combo.bind("<<ComboboxSelected>>", self._on_jira_project_changed)
        self.jira_project_combo.bind("<Return>", lambda e: self._save())
        row += 1

        # No "Issue Type" field here any more -- it's always
        # config.DEFAULT_ISSUE_TYPE ("Sub-task") for this app, applied
        # automatically at export time (see app/export_csv.py), the same
        # reasoning that removed it from Settings.

        ttk.Label(frm, text="Day").grid(row=row, column=0, sticky="w", pady=4)
        self.day_var = tk.StringVar()
        self.day_combo = RoundedCombobox(frm, textvariable=self.day_var, state="readonly", width=30)
        self.day_combo.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
        self.day_combo.bind("<Return>", lambda e: self._save())
        row += 1

        ttk.Label(frm, text="Start / End").grid(row=row, column=0, sticky="w", pady=4)
        time_row = ttk.Frame(frm)
        time_row.grid(row=row, column=1, columnspan=2, sticky="w", pady=4)
        self.start_var = tk.StringVar()
        self.start_combo = RoundedCombobox(time_row, textvariable=self.start_var,
                                            state="readonly", width=9)
        self.start_combo.pack(side="left")
        self.start_combo.bind("<Return>", lambda e: self._save())
        ttk.Label(time_row, text="  to  ").pack(side="left")
        self.end_var = tk.StringVar()
        self.end_combo = RoundedCombobox(time_row, textvariable=self.end_var,
                                          state="readonly", width=9)
        self.end_combo.pack(side="left")
        self.end_combo.bind("<Return>", lambda e: self._save())
        row += 1

        self.notes_label = ttk.Label(frm, text="Notes")
        self.notes_label.grid(row=row, column=0, sticky="nw", pady=4)
        self.notes_text = tk.Text(frm, width=34, height=5, font=(self.family, 10),
                                   relief="flat", highlightthickness=1,
                                   highlightbackground=theme.BORDER_STRONG, highlightcolor=theme.ACCENT,
                                   bg=theme.FIELD_BG, fg=theme.TEXT_PRIMARY,
                                   insertbackground=theme.TEXT_PRIMARY,
                                   padx=6, pady=4)
        self.notes_text.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
        row += 1

        ttk.Label(frm, text="Jira Status").grid(row=row, column=0, sticky="w", pady=4)
        self.status_var = tk.StringVar(value=_KEEP_JIRA_STATUS)
        self.status_combo = RoundedCombobox(frm, textvariable=self.status_var,
                                             state="readonly", width=30)
        self.status_combo.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
        self.status_hint = tk.Label(
            frm,
            text="Optional. “Work Completed” closes the ticket in Jira; "
                 "when every QDM in that group is closed it leaves the sidebar.",
            bg=theme.PANEL_BG, fg=theme.TEXT_MUTED, justify="left", wraplength=360,
            font=(self.family, 8))
        self.status_hint.grid(row=row + 1, column=1, columnspan=2, sticky="w", pady=(0, 4))
        row += 2

        self.error_label = ttk.Label(frm, text="", foreground=theme.DANGER)
        self.error_label.grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1

        # Rebuilt on every load() so the Delete button only appears when
        # editing an existing block, not when creating a new one.
        self.btns = ttk.Frame(frm)
        self.btns.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(16, 0))

    # ------------------------------------------------------------------
    def _apply_activity_fields(self, act: Activity):
        """Copy this QDM's issue key and Jira project into the form."""
        self.jira_key_number_var.set(config.jira_key_number(act.jira_key))
        self._set_jira_project_from_activity(act)

    def _set_jira_project_from_activity(self, act: Activity):
        project = (act.jira_project or "").strip()
        if not project:
            return
        self._ensure_known_jira_project(project)
        self.jira_project_var.set(project)
        self._previous_jira_project = project

    def _ensure_known_jira_project(self, project: str):
        if project.lower() not in {p.lower() for p in self.known_jira_projects}:
            self.known_jira_projects.append(project)
            self._refresh_jira_project_values()

    def _on_activity_changed(self, _event=None):
        if self._syncing:
            return
        act = self._selected_activity()
        if act is None:
            return
        self._syncing = True
        try:
            self._apply_activity_fields(act)
        finally:
            self._syncing = False
        self._schedule_status_fetch()

    def _on_jira_key_changed(self, *_):
        if self._syncing:
            return
        act = activity_matching_jira_key(self.activities, self.jira_key_number_var.get())
        if act is not None:
            self._syncing = True
            try:
                self.activity_var.set(act.name)
                self.activity_combo.set(act.name)
                self._set_jira_project_from_activity(act)
            finally:
                self._syncing = False
        self._schedule_status_fetch()

    def _selected_activity(self) -> Optional[Activity]:
        name = self.activity_var.get()
        for a in self.activities:
            if a.name == name:
                return a
        return None

    def _refresh_jira_project_values(self):
        self.jira_project_combo.config(values=self.known_jira_projects + [_NEW_JIRA_PROJECT_OPTION])

    def _on_jira_project_changed(self, _event=None):
        if self._syncing:
            if self.jira_project_var.get() != _NEW_JIRA_PROJECT_OPTION:
                self._previous_jira_project = self.jira_project_var.get()
            return
        chosen = self.jira_project_var.get()
        if chosen != _NEW_JIRA_PROJECT_OPTION:
            self._previous_jira_project = chosen
            # Same two-way link as QDM ↔ issue key, but only when this
            # Jira project belongs to exactly one QDM. Shared projects
            # (the usual case) must not yank the QDM dropdown around.
            act = activity_matching_jira_project(self.activities, chosen)
            if act is None:
                return
            self._syncing = True
            try:
                self.activity_var.set(act.name)
                self.activity_combo.set(act.name)
                self.jira_key_number_var.set(config.jira_key_number(act.jira_key))
            finally:
                self._syncing = False
            return
        name = simpledialog.askstring(
            "New Jira Project",
            "Jira project name (e.g. \"Quasar Delivery Management\"):",
            parent=self,
        )
        name = (name or "").strip()
        if not name:
            # Cancelled, or left blank -- revert rather than leaving the
            # "+ New Project…" placeholder sitting there as if selected.
            self._syncing = True
            try:
                self.jira_project_var.set(self._previous_jira_project)
            finally:
                self._syncing = False
            return
        if name.lower() not in {p.lower() for p in self.known_jira_projects}:
            self.known_jira_projects.append(name)
            self._refresh_jira_project_values()
        self._syncing = True
        try:
            self.jira_project_var.set(name)
        finally:
            self._syncing = False
        self._previous_jira_project = name

    def _keep_status_label(self) -> str:
        act = self._selected_activity()
        current = (act.jira_status or "").strip() if act else ""
        if current:
            return f"Don't change ({current})"
        return _KEEP_JIRA_STATUS

    def _reset_status_combo(self, extra_values=None):
        keep = self._keep_status_label()
        values = [keep] + list(extra_values or ())
        self.status_combo.config(values=values)
        self.status_var.set(keep)
        self.status_combo.set(keep)
        self._transition_by_label = {}

    def _schedule_status_fetch(self):
        if self._status_fetch_job is not None:
            try:
                self.after_cancel(self._status_fetch_job)
            except tk.TclError:
                pass
            self._status_fetch_job = None
        self._status_fetch_job = self.after(200, self._fetch_status_now)

    def _fetch_status_now(self):
        self._status_fetch_job = None
        key = config.jira_key_from_number(self.jira_key_number_var.get())
        if not key or self.on_fetch_transitions is None:
            self._transitions = []
            self._reset_status_combo()
            return
        self._status_fetch_seq += 1
        seq = self._status_fetch_seq
        self._reset_status_combo(["Loading statuses…"])
        self.on_fetch_transitions(key, lambda trans, seq=seq: self._apply_transitions(seq, trans))

    def _apply_transitions(self, seq: int, transitions):
        if seq != self._status_fetch_seq:
            return
        self._transitions = list(transitions or [])
        keep = self._keep_status_label()
        labels = [keep]
        self._transition_by_label = {}
        close = preferred_close_transition(self._transitions)
        ordered = []
        if close is not None:
            ordered.append(close)
        ordered.extend(t for t in self._transitions if close is None or t.id != close.id)
        for trans in ordered:
            label = trans.label()
            if trans.closes_ticket() and "close" not in label.lower():
                label = f"{label} (close)"
            if label in self._transition_by_label:
                label = f"{label} [{trans.id}]"
            self._transition_by_label[label] = trans
            labels.append(label)
        self.status_combo.config(values=labels)
        self.status_var.set(keep)
        self.status_combo.set(keep)

    # ------------------------------------------------------------------
    def load(self, activities: List[Activity], day_options,
              initial_day_idx: Optional[int], initial_start: Optional[str],
              initial_end: Optional[str], initial_activity_id: Optional[int],
              initial_notes: str, on_save: Callable[[dict], bool],
              on_delete: Optional[Callable[[], None]], start_hour: int, end_hour: int,
              slot_minutes: int, known_jira_projects: List[str],
              initial_jira_project: str = "", is_new: bool = True,
              require_notes: bool = False,
              on_fetch_transitions: Optional[Callable] = None,
              on_cancel: Optional[Callable[[], None]] = None):
        self.activities = activities
        self.activities_by_id = {a.id: a for a in activities}
        self.on_save = on_save
        self.on_delete = on_delete
        self.on_cancel = on_cancel
        self.on_fetch_transitions = on_fetch_transitions
        self._require_notes = require_notes
        self.heading.config(text="New Time Block" if is_new else "Edit Time Block")
        if require_notes:
            self.notes_label.config(text="Description")
        else:
            self.notes_label.config(text="Notes")

        self.day_labels = [label for label, _key in day_options]
        self.day_key_by_label = {label: key for label, key in day_options}
        self.day_label_by_key = {key: label for label, key in day_options}
        self.day_combo.config(values=self.day_labels)

        activity_names = [a.name for a in activities] or ["(no QDM's yet)"]
        self.activity_combo.config(values=activity_names)

        self.time_options = []
        t = start_hour * 60
        end_total = end_hour * 60
        while t <= end_total:
            self.time_options.append(f"{t // 60:02d}:{t % 60:02d}")
            t += slot_minutes
        for extra in (initial_start, initial_end):
            stamp = (extra or "").strip()
            if stamp and stamp not in self.time_options:
                self.time_options.append(stamp)
        self.time_options.sort()
        self.start_combo.config(values=self.time_options)
        self.end_combo.config(values=self.time_options)

        self.error_label.config(text="")
        self.notes_text.delete("1.0", "end")

        self.known_jira_projects = list(known_jira_projects) or [config.DEFAULT_JIRA_PROJECT]

        self._syncing = True
        try:
            self.activity_var.set("")
            self.activity_combo.set("")
            self.jira_key_number_var.set("")
            act = None
            if initial_activity_id is not None and initial_activity_id in self.activities_by_id:
                act = self.activities_by_id[initial_activity_id]
                self.activity_var.set(act.name)
                self.activity_combo.set(act.name)
                self.jira_key_number_var.set(config.jira_key_number(act.jira_key))

            # Editing a block keeps its stored Jira Project. A new block
            # with a QDM already chosen (armed/dragged) uses that QDM's
            # project so the dropdown matches the issue key we just filled.
            effective_jira_project = (initial_jira_project or "").strip()
            if not effective_jira_project and act is not None:
                effective_jira_project = (act.jira_project or "").strip()
            if not effective_jira_project:
                effective_jira_project = config.DEFAULT_JIRA_PROJECT
            if effective_jira_project.lower() not in {p.lower() for p in self.known_jira_projects}:
                # A stored override that isn't in the known-projects list for
                # whatever reason (e.g. old data) -- show it anyway rather
                # than silently swapping in something this block doesn't
                # actually use.
                self.known_jira_projects.append(effective_jira_project)
            self._refresh_jira_project_values()
            self.jira_project_var.set(effective_jira_project)
            self._previous_jira_project = effective_jira_project
        finally:
            self._syncing = False

        default_day = self.day_labels[0] if self.day_labels else ""
        day_label = self.day_label_by_key.get(initial_day_idx, default_day)
        self.day_var.set(day_label)
        self.day_combo.set(day_label)

        start_value = initial_start or (self.time_options[0] if self.time_options else "")
        end_value = initial_end or (
            self.time_options[min(2, len(self.time_options) - 1)] if self.time_options else "")
        self.start_var.set(start_value)
        self.start_combo.set(start_value)
        self.end_var.set(end_value)
        self.end_combo.set(end_value)

        if initial_notes:
            self.notes_text.insert("1.0", initial_notes)

        for child in self.btns.winfo_children():
            child.destroy()
        if on_delete:
            RoundedButton(self.btns, text="Delete", style="Danger.TButton",
                          command=self._delete).pack(side="left")
        RoundedButton(self.btns, text="Cancel", style="Secondary.TButton",
                      command=self._cancel).pack(side="right")
        RoundedButton(self.btns, text="Save", style="Accent.TButton",
                      command=self._save).pack(side="right", padx=6)
        self._scroll.bind_wheel_recursive(self.btns)

        if require_notes:
            self.notes_text.focus_set()
        else:
            self.activity_combo.focus_set()
        self._schedule_status_fetch()

    # ------------------------------------------------------------------
    def _save(self):
        act = self._selected_activity()
        if act is None:
            self.error_label.config(text="Please choose a QDM.")
            return
        start = self.start_var.get()
        end = self.end_var.get()
        if start >= end:
            self.error_label.config(text="End time must be after start time.")
            return
        notes = self.notes_text.get("1.0", "end").strip()
        trans = self._transition_by_label.get(self.status_var.get())
        closing = bool(trans and trans.closes_ticket())
        if (self._require_notes or closing) and not notes:
            self.error_label.config(
                text="Add a description — Jira requires one on the worklog.")
            self.notes_text.focus_set()
            return

        jira_project = self.jira_project_var.get().strip()
        if not jira_project or jira_project == _NEW_JIRA_PROJECT_OPTION:
            jira_project = config.DEFAULT_JIRA_PROJECT

        result = {
            "activity_id": act.id,
            "activity_name": act.name,
            "jira_key": config.jira_key_from_number(self.jira_key_number_var.get()),
            "color": act.color,
            "day_idx": self.day_key_by_label[self.day_var.get()],
            "start_time": start,
            "end_time": end,
            "notes": notes,
            "jira_project": jira_project,
            "issue_type": None,
            "jira_transition_id": trans.id if trans else None,
            "jira_transition_to_name": (trans.to_name or trans.name) if trans else None,
            "jira_transition_to_category": trans.to_category if trans else None,
        }
        assert self.on_save is not None
        ok = self.on_save(result)
        if ok is not False:
            show_saved_toast(self)
            self.on_close()

    def _cancel(self):
        cb = self.on_cancel
        self.on_close()
        if cb:
            cb()

    def _delete(self):
        cb = self.on_delete
        self.on_close()
        if cb:
            cb()
