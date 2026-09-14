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
from .models import Activity
from .widgets import RoundedButton, RoundedCombobox, ScrollArea, show_saved_toast

# Shown at the end of the Jira Project dropdown as a way to add a value
# that isn't in the known-projects list yet (see TimeBlockPanel.load's
# known_jira_projects param) rather than allowing free text that's easy
# to typo.
_NEW_JIRA_PROJECT_OPTION = "+ New Project…"


class TimeBlockPanel(tk.Frame):
    def __init__(self, master, family: str, on_close: Callable[[], None]):
        super().__init__(master, bg=theme.PANEL_BG)
        self.family = family
        self.on_close = on_close
        self.on_save: Optional[Callable[[dict], bool]] = None
        self.on_delete: Optional[Callable[[], None]] = None
        self.activities: List[Activity] = []
        self.activities_by_id = {}
        self.day_labels: List[str] = []
        self.day_key_by_label = {}
        self.day_label_by_key = {}
        self.time_options: List[str] = []
        self.known_jira_projects: List[str] = []
        self._previous_jira_project = ""

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

        ttk.Label(frm, text="Notes").grid(row=row, column=0, sticky="nw", pady=4)
        self.notes_text = tk.Text(frm, width=34, height=5, font=(self.family, 10),
                                   relief="flat", highlightthickness=1,
                                   highlightbackground=theme.BORDER_STRONG, highlightcolor=theme.ACCENT,
                                   bg=theme.FIELD_BG, fg=theme.TEXT_PRIMARY,
                                   insertbackground=theme.TEXT_PRIMARY,
                                   padx=6, pady=4)
        self.notes_text.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
        row += 1

        self.error_label = ttk.Label(frm, text="", foreground=theme.DANGER)
        self.error_label.grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1

        # Rebuilt on every load() so the Delete button only appears when
        # editing an existing block, not when creating a new one.
        self.btns = ttk.Frame(frm)
        self.btns.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(16, 0))

    # ------------------------------------------------------------------
    def _on_activity_changed(self, _event=None):
        # Only the Jira Issue Key comes from the chosen QDM -- Jira
        # Project/Issue Type no longer live on a QDM at all (they're
        # this app's fixed defaults, or this block's own choice below),
        # so switching QDMs leaves whatever was already picked for those
        # alone instead of blanking it out.
        name = self.activity_var.get()
        for a in self.activities:
            if a.name == name:
                self.jira_key_number_var.set(config.jira_key_number(a.jira_key))
                break

    def _selected_activity(self) -> Optional[Activity]:
        name = self.activity_var.get()
        for a in self.activities:
            if a.name == name:
                return a
        return None

    def _refresh_jira_project_values(self):
        self.jira_project_combo.config(values=self.known_jira_projects + [_NEW_JIRA_PROJECT_OPTION])

    def _on_jira_project_changed(self, _event=None):
        if self.jira_project_var.get() != _NEW_JIRA_PROJECT_OPTION:
            self._previous_jira_project = self.jira_project_var.get()
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
            self.jira_project_var.set(self._previous_jira_project)
            return
        if name.lower() not in {p.lower() for p in self.known_jira_projects}:
            self.known_jira_projects.append(name)
            self._refresh_jira_project_values()
        self.jira_project_var.set(name)
        self._previous_jira_project = name

    # ------------------------------------------------------------------
    def load(self, activities: List[Activity], day_options,
              initial_day_idx: Optional[int], initial_start: Optional[str],
              initial_end: Optional[str], initial_activity_id: Optional[int],
              initial_notes: str, on_save: Callable[[dict], bool],
              on_delete: Optional[Callable[[], None]], start_hour: int, end_hour: int,
              slot_minutes: int, known_jira_projects: List[str],
              initial_jira_project: str = "", is_new: bool = True):
        self.activities = activities
        self.activities_by_id = {a.id: a for a in activities}
        self.on_save = on_save
        self.on_delete = on_delete

        self.heading.config(text="New Time Block" if is_new else "Edit Time Block")

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
        self.start_combo.config(values=self.time_options)
        self.end_combo.config(values=self.time_options)

        self.error_label.config(text="")
        self.notes_text.delete("1.0", "end")

        self.activity_var.set("")
        self.activity_combo.set("")
        self.jira_key_number_var.set("")
        if initial_activity_id is not None and initial_activity_id in self.activities_by_id:
            act = self.activities_by_id[initial_activity_id]
            self.activity_var.set(act.name)
            self.activity_combo.set(act.name)
            self.jira_key_number_var.set(config.jira_key_number(act.jira_key))

        self.known_jira_projects = list(known_jira_projects) or [config.DEFAULT_JIRA_PROJECT]
        effective_jira_project = (initial_jira_project or "").strip() or config.DEFAULT_JIRA_PROJECT
        if effective_jira_project.lower() not in {p.lower() for p in self.known_jira_projects}:
            # A stored override that isn't in the known-projects list for
            # whatever reason (e.g. old data) -- show it anyway rather
            # than silently swapping in something this block doesn't
            # actually use.
            self.known_jira_projects.append(effective_jira_project)
        self._refresh_jira_project_values()
        self.jira_project_var.set(effective_jira_project)
        self._previous_jira_project = effective_jira_project

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

        self.activity_combo.focus_set()

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
            "notes": self.notes_text.get("1.0", "end").strip(),
            "jira_project": jira_project,
            # No longer editable here (see the comment near the fields
            # above) -- Jira Issue Type comes from app/config.py's fixed
            # default at export time instead.
            "issue_type": None,
        }
        assert self.on_save is not None
        ok = self.on_save(result)
        if ok is not False:
            show_saved_toast(self)
            self.on_close()

    def _cancel(self):
        self.on_close()

    def _delete(self):
        cb = self.on_delete
        self.on_close()
        if cb:
            cb()
