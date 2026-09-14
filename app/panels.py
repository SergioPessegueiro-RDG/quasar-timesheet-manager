"""
Embedded (non-popup) panels: Duplicate, Add/Edit Activity, Add/Edit Project,
Settings, Export.

These used to be separate pop-up windows (tk.Toplevel dialogs). Like the
time-block editor (see app/timeblock_panel.py), they're now tabs that show
up next to "Timesheet" in the main window's tab bar only while in use, and
hide again afterwards -- this sidesteps macOS/Tk setups where pop-up
windows can ignore explicit on-screen positioning and open off in a corner
no matter what the app asks for, since there's no separate window to
mis-position in the first place.

The one exception is the OS's native color picker (colorchooser.askcolor,
used from ProjectPanel) -- that's the operating system's own dialog, not
one of ours, positioned by the OS the same way a file-open dialog is, so it
isn't affected by the pop-up-window bug this refactor works around.
"""
import tkinter as tk
from datetime import date, datetime, timedelta
from tkinter import colorchooser, filedialog, messagebox, ttk
from typing import Callable, Dict, List, Optional, Union

from . import config, theme
from .models import Activity, Project, TemplateEntry, TimeEntry
from .version import APP_VERSION
from .widgets import (
    RoundedButton, RoundedCard, RoundedCheckbutton, RoundedCombobox, RoundedEntry,
    ScrollArea, show_saved_toast,
)

EntryLike = Union[TimeEntry, TemplateEntry]

# Shown read-only under Settings -> Keyboard Shortcuts (see SettingsPanel
# below). Kept as one list here rather than scattered across whichever
# file actually binds each one, so this stays the single place to update
# if a shortcut is ever added, changed, or removed -- see
# main_window.py._bind_global_shortcuts (undo/redo), calendar_view.py's
# _build_widgets (everything calendar-related), and ProjectPanel's own
# Name field below (Enter to save) for where they're actually wired up.
_SHORTCUTS = [
    ("Ctrl+Z  (or Cmd+Z on Mac)", "Undo the last calendar change"),
    ("Ctrl+Y or Ctrl+Shift+Z  (or Cmd+Shift+Z / Cmd+Y on Mac)", "Redo"),
    ("Left / Right arrow", "Move the selected block a day earlier/later -- or, "
                            "with nothing selected, go to the previous/next week"),
    ("Up / Down arrow", "Move the selected block earlier/later by one time slot"),
    ("Delete or Backspace", "Delete the selected block"),
    ("Esc", "Cancel a drag in progress, un-arm a queued activity, or deselect a block"),
    ("Ctrl+Click a block", "Instantly duplicate it into its own exact time slot "
                            "(drag the copy afterward to retime it)"),
    ("Enter (in a form field)", "Save the current Add/Edit Project, QDM, or "
                                 "Time Block form without needing to click Save"),
]


def _scroll_body(master, **kwargs) -> tk.Frame:
    """Every embedded panel's content goes inside one of these instead of
    packing straight into the panel Frame -- a plain ScrollArea with no
    visible border (outline=False) and no inset (pad=0), so nothing looks
    different from before, but content that doesn't fit the window (a lot
    of fields stacked up, or the Settings tab's theme previews) can
    still be scrolled to and its Save/Cancel/Delete buttons are never
    stranded off the bottom of an un-maximized window."""
    kwargs.setdefault("bg", theme.PANEL_BG)
    kwargs.setdefault("outline", False)
    kwargs.setdefault("pad", 0)
    area = ScrollArea(master, **kwargs)
    area.pack(fill="both", expand=True)
    # Stashed so _rebind_wheel (below) can find the owning ScrollArea from
    # any descendant widget without every panel needing to keep its own
    # reference around.
    area.content._scroll_area = area  # type: ignore[attr-defined]
    return area.content


def _configure_half_width_columns(outer: tk.Frame):
    """Split `outer` into two equal-weight grid columns. Most callers only
    use column 0 (real content) and leave column 1 as a spacer; Settings
    puts its Keyboard Shortcuts section in column 1 instead (see
    SettingsPanel.__init__) rather than leaving it empty. Either way,
    because the two columns always share outer's width equally, whatever
    sits in a column (gridded with sticky="new") grows with the window --
    to about half its width when there's a spacer, less than half when
    there's real content on both sides -- and shrinks back down toward
    its own natural minimum on a narrow one, rather than bunching up in
    the top-left corner with the rest of the tab left bare, which is what
    a plain pack(anchor="w") gives you regardless of how wide the window
    is."""
    outer.columnconfigure(0, weight=1)
    outer.columnconfigure(1, weight=1)


def _settings_card(parent, row: int, pady=(0, 16)):
    """A SURFACE rounded card for one Settings section."""
    card = RoundedCard(parent, bg=theme.SURFACE, radius=14, outline=False, shrink=True)
    card.grid(row=row, column=0, columnspan=2, sticky="ew", pady=pady)
    inner = tk.Frame(card.body, bg=theme.SURFACE)
    inner.pack(fill="both", expand=True, padx=2, pady=2)
    inner.columnconfigure(0, weight=1)
    return inner


def _settings_heading(parent, text: str, family: str, row: int = 0):
    tk.Label(parent, text=text, font=(family, 12, "bold"),
             bg=theme.SURFACE, fg=theme.TEXT_PRIMARY).grid(
        row=row, column=0, columnspan=2, sticky="w", pady=(0, 6))


def _settings_hint(parent, text: str, family: str, row: int, pady=(0, 10)):
    tk.Label(parent, text=text, fg=theme.TEXT_MUTED, bg=theme.SURFACE,
             justify="left", wraplength=420, font=(family, 9)).grid(
        row=row, column=0, columnspan=2, sticky="w", pady=pady)


def _rebind_wheel(widget):
    """Call after destroying and recreating a widget's children inside a
    panel built on _scroll_body (a Save/Cancel/Delete button row, a
    checkbox list, etc). ScrollArea normally re-attaches its direct mouse-
    wheel binding (see ScrollArea.bind_wheel_recursive in app/widgets.py)
    to newly-added widgets automatically, the next time its `.content`
    frame's own size changes -- but re-opening one of these panels for a
    different record often rebuilds an identically-sized row (the same
    Save/Cancel/Delete buttons every time), which never fires that resize,
    so the freshly-created widgets would otherwise be missed. This walks up
    to the owning ScrollArea and re-binds `widget` and its children
    explicitly instead of depending on a resize happening at all."""
    w = widget
    while w is not None:
        area = getattr(w, "_scroll_area", None)
        if area is not None:
            area.bind_wheel_recursive(widget)
            return
        w = getattr(w, "master", None)


# ---------------------------------------------------------------------------
# Duplicate panel -- copy a time block to other weekdays
# ---------------------------------------------------------------------------
class DuplicatePanel(tk.Frame):
    def __init__(self, master, family: str, on_close: Callable[[], None]):
        super().__init__(master, bg=theme.PANEL_BG)
        self.family = family
        self.on_close = on_close
        self.on_duplicate: Optional[Callable[[List[int]], None]] = None
        self.day_vars = {}

        body = _scroll_body(self)
        outer = tk.Frame(body, bg=theme.PANEL_BG)
        outer.pack(fill="both", expand=True, padx=28, pady=24)

        tk.Label(outer, text="Duplicate Time Block", font=(self.family, 14, "bold"),
                 bg=theme.PANEL_BG, fg=theme.TEXT_PRIMARY).pack(anchor="w", pady=(0, 6))

        self.subheading = tk.Label(outer, text="", font=(self.family, 10), justify="left",
                                    wraplength=380, bg=theme.PANEL_BG, fg=theme.TEXT_SECONDARY)
        self.subheading.pack(anchor="w", pady=(0, 14))

        self.days_frame = ttk.Frame(outer)
        self.days_frame.pack(anchor="w")

        quick_row = ttk.Frame(outer)
        quick_row.pack(anchor="w", pady=(10, 12))
        RoundedButton(quick_row, text="Select all", style="Secondary.TButton",
                      command=self._select_all).pack(side="left")
        RoundedButton(quick_row, text="Clear", style="Secondary.TButton",
                      command=self._clear_all).pack(side="left", padx=6)

        self.error_label = ttk.Label(outer, text="", foreground=theme.DANGER)
        self.error_label.pack(anchor="w")

        self.btns = ttk.Frame(outer)
        self.btns.pack(anchor="w", fill="x", pady=(16, 0))

    def load(self, source_entry: EntryLike, day_options, source_day_idx: int,
              on_duplicate: Callable[[List[int]], None]):
        self.on_duplicate = on_duplicate
        self.subheading.config(
            text=f"Duplicate “{source_entry.activity_name}” "
                 f"({source_entry.start_time}–{source_entry.end_time}) to:")

        for child in self.days_frame.winfo_children():
            child.destroy()
        self.day_vars = {}
        for label, day_idx in day_options:
            if day_idx == source_day_idx:
                continue  # the source day already has this block
            var = tk.BooleanVar(value=False)
            self.day_vars[day_idx] = var
            ttk.Checkbutton(self.days_frame, text=label, variable=var).pack(anchor="w", pady=2)
        _rebind_wheel(self.days_frame)

        self.error_label.config(text="")
        for child in self.btns.winfo_children():
            child.destroy()
        RoundedButton(self.btns, text="Cancel", style="Secondary.TButton",
                      command=self._cancel).pack(side="right")
        RoundedButton(self.btns, text="Duplicate", style="Accent.TButton",
                      command=self._duplicate).pack(side="right", padx=6)
        _rebind_wheel(self.btns)

    def _select_all(self):
        for var in self.day_vars.values():
            var.set(True)

    def _clear_all(self):
        for var in self.day_vars.values():
            var.set(False)

    def _duplicate(self):
        selected = [day_idx for day_idx, var in self.day_vars.items() if var.get()]
        if not selected:
            self.error_label.config(text="Pick at least one day.")
            return
        cb = self.on_duplicate
        self.on_close()
        assert cb is not None
        cb(selected)

    def _cancel(self):
        self.on_close()


# ---------------------------------------------------------------------------
# Activity add/edit panel (the loggable, draggable-onto-the-calendar leaf item)
# ---------------------------------------------------------------------------
# Shown at the end of the Activity tab's Project dropdown as a way to
# create a project without leaving this tab (see ActivityPanel's
# create_project param) rather than needing the separate Add Project tab.
_NEW_PROJECT_OPTION = "+ New Project…"


class ActivityPanel(tk.Frame):
    def __init__(self, master, family: str, on_close: Callable[[], None],
                 get_projects: Callable[[], List[Project]],
                 create_project: Callable[[str], Project]):
        super().__init__(master, bg=theme.PANEL_BG)
        self.family = family
        self.on_close = on_close
        self.get_projects = get_projects
        self.create_project = create_project
        self.on_save: Optional[Callable[[dict], bool]] = None
        self.on_delete: Optional[Callable[[], None]] = None
        self.project_id_by_label: Dict[str, Optional[int]] = {}

        body = _scroll_body(self)
        outer = tk.Frame(body, bg=theme.PANEL_BG)
        outer.pack(fill="both", expand=True, padx=28, pady=24)
        _configure_half_width_columns(outer)

        self.heading = tk.Label(outer, text="Add QDM", font=(self.family, 20, "bold"),
                                 bg=theme.PANEL_BG, fg=theme.TEXT_PRIMARY)
        self.heading.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 24))

        frm = ttk.Frame(outer)
        frm.grid(row=1, column=0, sticky="new")
        frm.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(frm, text="Name", style="Big.TLabel").grid(row=row, column=0, sticky="w", pady=10)
        self.name_var = tk.StringVar()
        name_entry = ttk.Entry(frm, textvariable=self.name_var, width=32, style="Big.TEntry")
        name_entry.grid(row=row, column=1, sticky="ew", pady=10)
        name_entry.bind("<Return>", lambda e: self._save())
        row += 1

        ttk.Label(frm, text="Jira Issue Key", style="Big.TLabel").grid(row=row, column=0, sticky="w", pady=10)
        # Every key in this app starts with the same fixed "QDM-" prefix
        # (see app/config.py's JIRA_KEY_PREFIX), so this only asks for the
        # number after it -- the static "QDM-" label makes what's being
        # typed (and what the full key will be) obvious at a glance.
        key_row = tk.Frame(frm, bg=theme.PANEL_BG)
        key_row.grid(row=row, column=1, sticky="w", pady=10)
        tk.Label(key_row, text=config.JIRA_KEY_PREFIX, font=(self.family, 12, "bold"),
                 bg=theme.PANEL_BG, fg=theme.TEXT_SECONDARY).pack(side="left")
        self.jira_key_number_var = tk.StringVar()
        jira_key_entry = ttk.Entry(key_row, textvariable=self.jira_key_number_var, width=10,
                                    style="Big.TEntry")
        jira_key_entry.pack(side="left")
        jira_key_entry.bind("<Return>", lambda e: self._save())
        row += 1

        ttk.Label(frm, text="Project", style="Big.TLabel").grid(row=row, column=0, sticky="w", pady=10)
        self.project_var = tk.StringVar()
        self.project_combo = RoundedCombobox(frm, textvariable=self.project_var,
                                              state="readonly", width=30, style="Big.TCombobox")
        self.project_combo.grid(row=row, column=1, sticky="ew", pady=10)
        self.project_combo.bind("<<ComboboxSelected>>", self._on_project_changed)
        self.project_combo.bind("<Return>", lambda e: self._save())
        row += 1

        # Hidden unless "+ New Project..." is selected above (see
        # _on_project_changed) -- lets a user create a project without
        # leaving this tab, via the create_project callback.
        self.new_project_label = ttk.Label(frm, text="New Project Name", style="Big.TLabel")
        self.new_project_label.grid(row=row, column=0, sticky="w", pady=10)
        self.new_project_name_var = tk.StringVar()
        self.new_project_entry = ttk.Entry(frm, textvariable=self.new_project_name_var, width=32,
                                            style="Big.TEntry")
        self.new_project_entry.grid(row=row, column=1, sticky="ew", pady=10)
        self.new_project_entry.bind("<Return>", lambda e: self._save())
        self._set_new_project_field_visible(False)
        row += 1

        # Every Activity belongs to exactly one Project -- there's no
        # "(No project)" option -- since a time block's color always comes
        # from its Activity's Project rather than being set here.
        tk.Label(frm, text="Color comes from the Project this activity belongs to -- set it "
                            "from the Project's own edit panel.",
                 fg=theme.TEXT_MUTED, bg=theme.PANEL_BG, justify="left", wraplength=420,
                 font=(self.family, 9)).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 14))
        row += 1

        # Jira Project and Issue Type are deliberately NOT editable
        # per-activity (or anywhere else in the UI) -- this app always
        # exports into the same fixed Jira project and issue type (see
        # app/config.py's DEFAULT_JIRA_PROJECT/DEFAULT_ISSUE_TYPE). Export
        # still fills them in automatically (see app/export_csv.py's
        # build_row) -- a time block can still override either one
        # individually if it ever needs to differ, via its own Time Block
        # tab.

        ttk.Label(frm, text="Default Duration (min)", style="Big.TLabel").grid(
            row=row, column=0, sticky="w", pady=10)
        self.duration_var = tk.StringVar()
        duration_entry = ttk.Entry(frm, textvariable=self.duration_var, width=10, style="Big.TEntry")
        duration_entry.grid(row=row, column=1, sticky="w", pady=10)
        duration_entry.bind("<Return>", lambda e: self._save())
        row += 1

        self.error_label = ttk.Label(frm, text="", foreground=theme.DANGER, style="Big.TLabel")
        self.error_label.grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        self.btns = ttk.Frame(frm)
        self.btns.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(24, 0))

    def load(self, activity: Optional[Activity], on_save: Callable[[dict], bool],
              on_delete: Optional[Callable[[], None]] = None):
        self.on_save = on_save
        self.on_delete = on_delete
        self.heading.config(text="Edit QDM" if activity else "Add QDM")

        projects = self.get_projects()
        labels = [p.name for p in projects]
        self.project_id_by_label = {p.name: p.id for p in projects}
        self.project_combo.config(values=labels + [_NEW_PROJECT_OPTION])
        current_project_id = activity.project_id if activity else (projects[0].id if projects else None)
        current_label = next((p.name for p in projects if p.id == current_project_id),
                              (labels[0] if labels else ""))
        self.project_var.set(current_label)
        self.project_combo.set(current_label)
        self.new_project_name_var.set("")
        self._set_new_project_field_visible(False)

        self.name_var.set(activity.name if activity else "")
        self.jira_key_number_var.set(config.jira_key_number(activity.jira_key) if activity else "")
        self.duration_var.set(
            str(activity.default_duration_minutes) if activity and activity.default_duration_minutes else "")
        self.error_label.config(text="")

        for child in self.btns.winfo_children():
            child.destroy()
        if on_delete:
            RoundedButton(self.btns, text="Delete", style="Danger.TButton",
                          command=self._delete).pack(side="left")
        RoundedButton(self.btns, text="Cancel", style="Secondary.TButton",
                      command=self._cancel).pack(side="right")
        RoundedButton(self.btns, text="Save", style="Accent.TButton",
                      command=self._save).pack(side="right", padx=6)
        _rebind_wheel(self.btns)

    def _set_new_project_field_visible(self, visible: bool):
        if visible:
            self.new_project_label.grid()
            self.new_project_entry.grid()
        else:
            self.new_project_label.grid_remove()
            self.new_project_entry.grid_remove()

    def _on_project_changed(self, _event=None):
        self._set_new_project_field_visible(self.project_var.get() == _NEW_PROJECT_OPTION)

    def _save(self):
        name = self.name_var.get().strip()
        if not name:
            self.error_label.config(text="Name is required.")
            return
        duration = None
        raw_dur = self.duration_var.get().strip()
        if raw_dur:
            try:
                duration = int(raw_dur)
                if duration <= 0:
                    raise ValueError
            except ValueError:
                self.error_label.config(text="Default duration must be a positive whole number of minutes.")
                return

        selected_project = self.project_var.get()
        if selected_project == _NEW_PROJECT_OPTION:
            new_project_name = self.new_project_name_var.get().strip()
            if not new_project_name:
                self.error_label.config(text="Enter a name for the new project.")
                return
            project_id = self.create_project(new_project_name).id
        else:
            project_id = self.project_id_by_label.get(selected_project)
            if project_id is None:
                self.error_label.config(text="Choose a project.")
                return

        result = {
            "name": name,
            "jira_key": config.jira_key_from_number(self.jira_key_number_var.get()),
            "default_duration_minutes": duration,
            "project_id": project_id,
            # No longer editable per-activity (see the comment near the
            # fields above) -- Jira Project/Issue Type come from
            # app/config.py's fixed defaults at export time instead.
            "jira_project": None,
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


# ---------------------------------------------------------------------------
# Project add/edit panel (collapsible groups in the sidebar; owns color)
# ---------------------------------------------------------------------------
class ProjectPanel(tk.Frame):
    def __init__(self, master, family: str, on_close: Callable[[], None]):
        super().__init__(master, bg=theme.PANEL_BG)
        self.family = family
        self.on_close = on_close
        self.on_save: Optional[Callable[[dict], bool]] = None
        self.on_delete: Optional[Callable[[], None]] = None
        self.selected_color = tk.StringVar(value=config.DEFAULT_PROJECT_COLORS[0])

        body = _scroll_body(self)
        outer = tk.Frame(body, bg=theme.PANEL_BG)
        outer.pack(fill="both", expand=True, padx=28, pady=24)
        _configure_half_width_columns(outer)

        self.heading = tk.Label(outer, text="Add Project", font=(self.family, 20, "bold"),
                                 bg=theme.PANEL_BG, fg=theme.TEXT_PRIMARY)
        self.heading.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        self.subheading = tk.Label(
            outer, text="Projects group your activities in the sidebar and set the color "
                        "every one of their time blocks shows.",
            font=(self.family, 10), justify="left", wraplength=420,
            bg=theme.PANEL_BG, fg=theme.TEXT_SECONDARY)
        self.subheading.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 20))

        frm = ttk.Frame(outer)
        frm.grid(row=2, column=0, sticky="new")
        frm.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(frm, text="Name *", style="Big.TLabel").grid(row=row, column=0, sticky="w", pady=10)
        self.name_var = tk.StringVar()
        entry = ttk.Entry(frm, textvariable=self.name_var, width=32, style="Big.TEntry")
        entry.grid(row=row, column=1, sticky="ew", pady=10)
        entry.bind("<Return>", lambda e: self._save())
        row += 1

        ttk.Label(frm, text="Color", style="Big.TLabel").grid(row=row, column=0, sticky="w", pady=10)
        color_frame = ttk.Frame(frm)
        color_frame.grid(row=row, column=1, sticky="w", pady=10)
        self.swatch = tk.Canvas(color_frame, width=32, height=32, highlightthickness=1,
                                 highlightbackground=theme.BORDER_STRONG, bg=theme.PANEL_BG)
        self.swatch.pack(side="left", padx=(0, 10))
        self._draw_swatch()
        RoundedButton(color_frame, text="Choose…", style="Secondary.TButton",
                      command=self._pick_color).pack(side="left")
        row += 1

        palette = ttk.Frame(frm)
        palette.grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 12))
        for c in config.DEFAULT_PROJECT_COLORS:
            sw = tk.Canvas(palette, width=24, height=24, bg=c, highlightthickness=1,
                            highlightbackground=theme.BORDER_STRONG, cursor="hand2")
            sw.pack(side="left", padx=3)
            sw.bind("<Button-1>", lambda e, col=c: self._set_color(col))
        row += 1

        # This project's color is the ONLY place a color gets set -- every
        # time block for every activity in this project just inherits it
        # (and stays in sync if it's changed here later, see
        # Sidebar._edit_project -> db.update_project), rather than each
        # activity or block having its own color.
        tk.Label(frm, text="This color is used for every time block for every activity in this project.",
                 fg=theme.TEXT_MUTED, bg=theme.PANEL_BG, justify="left", wraplength=420,
                 font=(self.family, 9)).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 14))
        row += 1

        self.error_label = ttk.Label(frm, text="", foreground=theme.DANGER, style="Big.TLabel")
        self.error_label.grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        self.btns = ttk.Frame(frm)
        self.btns.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(24, 0))

    def load(self, project: Optional[Project], on_save: Callable[[dict], bool],
              on_delete: Optional[Callable[[], None]] = None):
        self.on_save = on_save
        self.on_delete = on_delete
        self.heading.config(text="Edit Project" if project else "Add Project")
        self.name_var.set(project.name if project else "")
        self.selected_color.set(project.color if project else config.DEFAULT_PROJECT_COLORS[0])
        self._draw_swatch()
        self.error_label.config(text="")

        for child in self.btns.winfo_children():
            child.destroy()
        if on_delete:
            RoundedButton(self.btns, text="Delete", style="Danger.TButton",
                          command=self._delete).pack(side="left")
        RoundedButton(self.btns, text="Cancel", style="Secondary.TButton",
                      command=self._cancel).pack(side="right")
        RoundedButton(self.btns, text="Save", style="Accent.TButton",
                      command=self._save).pack(side="right", padx=6)
        _rebind_wheel(self.btns)

    def _draw_swatch(self):
        self.swatch.delete("all")
        theme.place_rounded_rect(self.swatch, 2, 2, 24, 24, radius=5,
                                 fill=self.selected_color.get(), outline="",
                                 background=self.swatch.cget("bg"))

    def _set_color(self, color):
        self.selected_color.set(color)
        self._draw_swatch()

    def _pick_color(self):
        # Native OS color picker -- not one of our windows, unaffected by
        # the pop-up-positioning bug this file otherwise works around.
        rgb, hexcode = colorchooser.askcolor(color=self.selected_color.get(), parent=self)
        if hexcode:
            self._set_color(hexcode)

    def _save(self):
        name = self.name_var.get().strip()
        if not name:
            self.error_label.config(text="Name is required.")
            return
        assert self.on_save is not None
        ok = self.on_save({"name": name, "color": self.selected_color.get()})
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


# ---------------------------------------------------------------------------
# Settings panel (Display Name, work hours, theme)
# ---------------------------------------------------------------------------
class SettingsPanel(tk.Frame):
    def __init__(self, master, family: str, on_close: Callable[[], None]):
        super().__init__(master, bg=theme.PANEL_BG)
        self.family = family
        self.on_close = on_close
        self.on_save: Optional[Callable[[str, str, int, int, bool], None]] = None
        self.theme_var = tk.StringVar(value=theme.DEFAULT_THEME_ID)
        self.theme_swatch_canvases: Dict[str, tk.Canvas] = {}

        # Custom palette's four seed colors (background, panel, text,
        # accent) -- staged as a plain dict (nothing binds to it via
        # textvariable; _pick_custom_color/_draw_custom_swatches below
        # read and write it directly) and pushed live into
        # theme.set_custom_seeds() on every pick, so the "Custom" grid
        # card always previews the current picks -- same live-preview
        # approach ProjectPanel's own color picker uses.
        # _custom_seeds_on_load stashes what they were when this panel was
        # last load()-ed, so _cancel() can restore them if the user tweaks
        # colors here and then backs out without saving.
        self.custom_seeds: Dict[str, str] = theme.get_custom_seeds()
        self._custom_seeds_on_load: Dict[str, str] = dict(self.custom_seeds)
        self.custom_swatch_canvases: Dict[str, tk.Canvas] = {}
        self.custom_controls_frame: Optional[tk.Frame] = None
        self.glass_alpha_frame: Optional[tk.Frame] = None
        self._glass_alpha_on_load = theme.get_glass_alpha()

        # The theme preview grid plus every field below it can run
        # taller than a smaller (non-maximized) window -- see _scroll_body.
        body = _scroll_body(self)
        outer = tk.Frame(body, bg=theme.PANEL_BG)
        outer.pack(fill="both", expand=True, padx=28, pady=24)
        _configure_half_width_columns(outer)

        tk.Label(outer, text="Settings", font=(self.family, 20, "bold"),
                 bg=theme.PANEL_BG, fg=theme.TEXT_PRIMARY).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 24))

        # Left column: Display Name, Work Hours, Theme -- the settings
        # someone actually edits. Right column: Keyboard Shortcuts --
        # reference material glanced at rather than changed, so it doesn't
        # need to sit above the fold underneath everything else; putting
        # it beside the settings instead uses the space
        # _configure_half_width_columns opened up on the right rather than
        # leaving it empty.
        self._card_bg = theme.SURFACE

        left = tk.Frame(outer, bg=theme.PANEL_BG)
        left.grid(row=1, column=0, sticky="new", padx=(0, 36))
        left.columnconfigure(0, weight=1)

        right = tk.Frame(outer, bg=theme.PANEL_BG)
        right.grid(row=1, column=1, sticky="new")
        right.columnconfigure(0, weight=1)

        # `left` and `right` split `outer` into two equal-*weight* columns
        # (_configure_half_width_columns), but neither one's actual
        # content can shrink to fit a narrower share of that split: the
        # theme preview grid below is a fixed matrix of swatch cards, and
        # the Keyboard Shortcuts table is two fixed-wraplength label
        # columns. Below some window width, evenly splitting the space
        # forces both columns narrower than that fixed content needs --
        # the content itself doesn't get smaller, it just spills past its
        # own column's boundary into the other one, which is what showed
        # up as the shortcuts table visually overlapping the theme grid.
        # _reflow_settings_columns watches outer's actual rendered width
        # (which tracks the window's width -- see ScrollArea's own
        # canvas-forced-width binding in widgets.py) and switches
        # Keyboard Shortcuts from beside the settings fields to below
        # them, full width, the moment there isn't room for both at their
        # natural size -- ScrollArea's existing vertical scrolling then
        # handles the extra height exactly like it already does for a
        # tall left column on its own.
        self._settings_left = left
        self._settings_right = right
        self._settings_outer = outer
        self._settings_stacked = False
        outer.bind("<Configure>", self._reflow_settings_columns)
        outer.after_idle(self._reflow_settings_columns)

        name_card = _settings_card(left, 0)
        _settings_heading(name_card, "Display Name", self.family)
        _settings_hint(name_card, "Appears in every exported row.", self.family, 1)
        self.display_name_var = tk.StringVar()
        RoundedEntry(name_card, textvariable=self.display_name_var, width=36,
                     bg=theme.SURFACE).grid(row=2, column=0, sticky="ew")

        jira_card = _settings_card(left, 1)
        _settings_heading(jira_card, "Jira", self.family)
        _settings_hint(
            jira_card,
            "Paste an API token (Atlassian account → Security → API tokens). "
            "Site URL can be https://your-company.atlassian.net or any "
            "dashboard / board link — extra path is stripped. Sync pulls "
            "open QDMs assigned to you. The token never leaves this machine.",
            self.family, 1)
        jira_fields = tk.Frame(jira_card, bg=theme.SURFACE)
        jira_fields.grid(row=2, column=0, columnspan=2, sticky="ew")
        jira_fields.columnconfigure(1, weight=1)

        tk.Label(jira_fields, text="Site URL", bg=theme.SURFACE, fg=theme.TEXT_PRIMARY,
                 font=(self.family, 10)).grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.jira_url_var = tk.StringVar()
        RoundedEntry(jira_fields, textvariable=self.jira_url_var, width=36,
                     bg=theme.SURFACE).grid(row=0, column=1, sticky="ew", padx=(12, 0), pady=(0, 6))
        tk.Label(jira_fields, text="https://your-company.atlassian.net  — a dashboard link is fine too",
                 fg=theme.TEXT_MUTED, bg=theme.SURFACE, font=(self.family, 8)).grid(
            row=1, column=1, sticky="w", padx=(12, 0), pady=(0, 10))

        tk.Label(jira_fields, text="Email", bg=theme.SURFACE, fg=theme.TEXT_PRIMARY,
                 font=(self.family, 10)).grid(row=2, column=0, sticky="w", pady=(0, 6))
        self.jira_email_var = tk.StringVar()
        RoundedEntry(jira_fields, textvariable=self.jira_email_var, width=36,
                     bg=theme.SURFACE).grid(row=2, column=1, sticky="ew", padx=(12, 0), pady=(0, 12))

        tk.Label(jira_fields, text="API token", bg=theme.SURFACE, fg=theme.TEXT_PRIMARY,
                 font=(self.family, 10)).grid(row=3, column=0, sticky="w", pady=(0, 6))
        token_row = tk.Frame(jira_fields, bg=theme.SURFACE)
        token_row.grid(row=3, column=1, sticky="ew", padx=(12, 0), pady=(0, 12))
        token_row.columnconfigure(0, weight=1)
        self.jira_token_var = tk.StringVar()
        self.jira_token_entry = RoundedEntry(
            token_row, textvariable=self.jira_token_var, width=28, show="•", bg=theme.SURFACE)
        self.jira_token_entry.pack(side="left", fill="x", expand=True)
        self._jira_token_visible = False
        RoundedButton(token_row, text="Show", style="Secondary.TButton",
                      command=self._toggle_jira_token, bg=theme.SURFACE).pack(side="left", padx=(8, 0))

        tk.Label(jira_fields, text="Project key", bg=theme.SURFACE, fg=theme.TEXT_PRIMARY,
                 font=(self.family, 10)).grid(row=4, column=0, sticky="w", pady=(0, 6))
        self.jira_project_key_var = tk.StringVar(value="QDM")
        RoundedEntry(jira_fields, textvariable=self.jira_project_key_var, width=12,
                     bg=theme.SURFACE).grid(row=4, column=1, sticky="w", padx=(12, 0), pady=(0, 12))

        jira_btns = tk.Frame(jira_fields, bg=theme.SURFACE)
        jira_btns.grid(row=5, column=0, columnspan=2, sticky="w", pady=(0, 4))
        RoundedButton(jira_btns, text="Test connection", style="Secondary.TButton",
                      command=self._test_jira, bg=theme.SURFACE).pack(side="left")
        RoundedButton(jira_btns, text="Sync QDMs now", style="Accent.TButton",
                      command=self._sync_jira, bg=theme.SURFACE).pack(side="left", padx=(8, 0))
        self.jira_status_label = tk.Label(jira_fields, text="", fg=theme.TEXT_SECONDARY,
                                           bg=theme.SURFACE, font=(self.family, 9),
                                           wraplength=420, justify="left")
        self.jira_status_label.grid(row=6, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.on_test_jira: Optional[Callable[[dict], None]] = None
        self.on_sync_jira: Optional[Callable[[dict], None]] = None

        hours_card = _settings_card(left, 2)
        _settings_heading(hours_card, "Work Hours", self.family)
        _settings_hint(
            hours_card,
            "Which hours the calendar grid shows, and whether it includes "
            "Saturday/Sunday. Applies to the Timesheet and Template tabs alike.",
            self.family, 1)
        hours_row = tk.Frame(hours_card, bg=theme.SURFACE)
        hours_row.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 14))
        self._start_hour_values = list(range(0, 24))
        self._end_hour_values = list(range(1, 25))
        start_labels = [self._format_hour(h) for h in self._start_hour_values]
        end_labels = [self._format_hour(h) for h in self._end_hour_values]

        tk.Label(hours_row, text="From", bg=theme.SURFACE, fg=theme.TEXT_PRIMARY,
                 font=(self.family, 11)).pack(side="left")
        self.work_start_var = tk.StringVar()
        self.work_start_combo = RoundedCombobox(hours_row, textvariable=self.work_start_var,
                                                 values=start_labels, state="readonly", width=8,
                                                 style="Big.TCombobox", bg=theme.SURFACE)
        self.work_start_combo.pack(side="left", padx=(8, 20))

        tk.Label(hours_row, text="To", bg=theme.SURFACE, fg=theme.TEXT_PRIMARY,
                 font=(self.family, 11)).pack(side="left")
        self.work_end_var = tk.StringVar()
        self.work_end_combo = RoundedCombobox(hours_row, textvariable=self.work_end_var,
                                               values=end_labels, state="readonly", width=8,
                                               style="Big.TCombobox", bg=theme.SURFACE)
        self.work_end_combo.pack(side="left", padx=(8, 0))

        self.show_weekends_var = tk.BooleanVar(value=False)
        RoundedCheckbutton(hours_card, text="Show weekends (Saturday & Sunday)",
                           variable=self.show_weekends_var, bg=theme.SURFACE).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(0, 12))

        self.hide_timer_var = tk.BooleanVar(value=False)
        RoundedCheckbutton(hours_card, text="Hide the Timer bar",
                           variable=self.hide_timer_var, bg=theme.SURFACE).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(0, 2))
        tk.Label(hours_card, text="Frees up space above the calendar. Turn back on here any time.",
                 fg=theme.TEXT_MUTED, bg=theme.SURFACE, justify="left", wraplength=420,
                 font=(self.family, 9)).grid(row=5, column=0, columnspan=2, sticky="w")

        heading_card = _settings_card(left, 3)
        _settings_heading(heading_card, "Heading", self.family)
        _settings_hint(
            heading_card,
            "Standard keeps a compact title next to the timer. Compact shrinks "
            "it further. Hidden removes the name -- the calendar gets that space.",
            self.family, 1, pady=(0, 8))
        self.header_style_row = tk.Frame(heading_card, bg=theme.SURFACE)
        self.header_style_row.grid(row=2, column=0, columnspan=2, sticky="w")
        self.header_style_choice = "standard"
        self.header_style_buttons: Dict[str, RoundedButton] = {}
        for key, label in (("standard", "Standard"), ("compact", "Compact"), ("hidden", "Hidden")):
            btn = RoundedButton(self.header_style_row, text=label, style="Secondary.TButton",
                                 command=lambda k=key: self._select_header_style(k),
                                 bg=theme.SURFACE)
            btn.pack(side="left", padx=(0, 6))
            self.header_style_buttons[key] = btn

        theme_card = _settings_card(left, 4, pady=(0, 0))
        _settings_heading(theme_card, "Theme", self.family)
        self.theme_description_label = tk.Label(
            theme_card, text="", fg=theme.TEXT_MUTED, bg=theme.SURFACE, justify="left",
            wraplength=480, font=(self.family, 9))
        self.theme_description_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 10))

        self.theme_grid = tk.Frame(theme_card, bg=theme.SURFACE)
        self.theme_grid.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 12))
        self._build_theme_grid()

        self.custom_controls_frame = tk.Frame(theme_card, bg=theme.SURFACE)
        self.custom_controls_frame.grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 16))
        self._build_custom_controls()

        self.glass_alpha_frame = tk.Frame(theme_card, bg=theme.SURFACE)
        self.glass_alpha_frame.grid(row=4, column=0, columnspan=2, sticky="ew")
        theme_card.columnconfigure(0, weight=1)
        self._build_glass_alpha_controls()

        shortcuts_card = RoundedCard(right, bg=theme.SURFACE, radius=14, outline=False, shrink=True)
        shortcuts_card.grid(row=0, column=0, sticky="ew")
        shortcuts_inner = tk.Frame(shortcuts_card.body, bg=theme.SURFACE)
        shortcuts_inner.pack(fill="both", expand=True, padx=2, pady=2)
        tk.Label(shortcuts_inner, text="Keyboard Shortcuts", font=(self.family, 12, "bold"),
                 bg=theme.SURFACE, fg=theme.TEXT_PRIMARY).pack(anchor="w", pady=(0, 10))
        shortcuts = tk.Frame(shortcuts_inner, bg=theme.SURFACE)
        shortcuts.pack(fill="x")
        for i, (keys, description) in enumerate(_SHORTCUTS):
            tk.Label(shortcuts, text=keys, font=(self.family, 10, "bold"),
                     bg=theme.SURFACE, fg=theme.TEXT_PRIMARY, anchor="nw",
                     justify="left", wraplength=260).grid(
                row=i, column=0, sticky="nw", padx=(0, 16), pady=5)
            tk.Label(shortcuts, text=description, font=(self.family, 10),
                     bg=theme.SURFACE, fg=theme.TEXT_SECONDARY, anchor="nw",
                     justify="left", wraplength=360).grid(row=i, column=1, sticky="nw", pady=5)

        tk.Label(right, text=f"QUASAR Timesheet Manager v{APP_VERSION}",
                 font=(self.family, 9), bg=theme.PANEL_BG, fg=theme.TEXT_MUTED).grid(
            row=1, column=0, sticky="w", pady=(16, 0))
        tk.Label(right, text="Alex Rae  ·  Sérgio Pessegueiro",
                 font=(self.family, 9), bg=theme.PANEL_BG, fg=theme.TEXT_MUTED).grid(
            row=2, column=0, sticky="w", pady=(2, 0))

        # row=20 (not row=2) so this stays below Keyboard Shortcuts either
        # way -- beside the settings fields at row=1 (the normal, wide-
        # window layout) or stacked below them at row=2 (see
        # _reflow_settings_columns) -- without needing to move this row
        # to match whichever layout is currently active. An unused grid
        # row number doesn't reserve any space, so this doesn't add a gap.
        btns = tk.Frame(outer, bg=theme.PANEL_BG)
        btns.grid(row=20, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        RoundedButton(btns, text="Cancel", style="Secondary.TButton", command=self._cancel).pack(side="right")
        RoundedButton(btns, text="Save", style="Accent.TButton", command=self._save).pack(side="right", padx=6)

    def _toggle_jira_token(self):
        self._jira_token_visible = not self._jira_token_visible
        self.jira_token_entry.configure(show="" if self._jira_token_visible else "•")

    def jira_fields(self) -> dict:
        return {
            "site_url": self.jira_url_var.get().strip(),
            "email": self.jira_email_var.get().strip(),
            "api_token": self.jira_token_var.get().strip(),
            "project_key": (self.jira_project_key_var.get().strip() or "QDM"),
        }

    def _test_jira(self):
        if self.on_test_jira is not None:
            self.on_test_jira(self.jira_fields())

    def _sync_jira(self):
        if self.on_sync_jira is not None:
            self.on_sync_jira(self.jira_fields())

    def _select_header_style(self, key: str):
        """Click handler for the Standard/Compact/Hidden segmented row --
        same selected/unselected style convention as the tab bar and
        SummaryPanel's Week/Month toggle (Accent for the chosen one,
        Secondary for the rest)."""
        self.header_style_choice = key
        for btn_key, btn in self.header_style_buttons.items():
            btn.config(style="Accent.TButton" if btn_key == key else "Secondary.TButton")

    def _reflow_settings_columns(self, event=None):
        """Bound to outer's <Configure> (plus one after_idle call so a
        Settings tab opened directly at a narrow window size starts
        stacked instead of waiting for the next resize) -- see the long
        comment where self._settings_left/right/outer are set, in
        __init__, for why this exists at all."""
        left = self._settings_left
        right = self._settings_right
        outer = self._settings_outer
        # 36 matches the padx gap __init__ gives `left` in the side-by-
        # side layout -- the actual space that gap needs, so it's part of
        # "how much width do both columns need together".
        needed = left.winfo_reqwidth() + right.winfo_reqwidth() + 36
        available = outer.winfo_width()
        # available <= 1 means outer hasn't been given a real size by its
        # own parent yet (still mid-construction) -- wait for the next
        # <Configure> instead of guessing from a meaningless width.
        if available <= 1:
            return
        should_stack = available < needed
        if should_stack == self._settings_stacked:
            return
        self._settings_stacked = should_stack
        if should_stack:
            # Collapse column 1's weight to 0 too -- otherwise, even with
            # `right` moved out of it, outer's two equal-weight columns
            # would still split the width evenly between them, leaving
            # `left` (now the only thing at row 1) stuck at half-width
            # with an empty gap beside it instead of using the space
            # `right` just vacated.
            outer.columnconfigure(1, weight=0)
            left.grid_configure(padx=0, pady=(0, 24))
            right.grid_configure(row=2, column=0, columnspan=2, pady=(0, 0))
        else:
            outer.columnconfigure(1, weight=1)
            left.grid_configure(padx=(0, 36), pady=0)
            right.grid_configure(row=1, column=1, columnspan=1, pady=0)

    def _build_theme_grid(self):
        # Sectioned like TickTick's Appearance picker: a heading whenever
        # the category changes, then a 4-column grid of preview cards.
        # "System" first, then the curated palettes in THEME_ORDER, then
        # "Custom". Built once (not rebuilt on every load()); only the
        # selection ring and description text change after that, via
        # _refresh_theme_selection().
        cols = 4
        ids = list(theme.THEME_ORDER) + [theme.CUSTOM_THEME_ID]
        grid_row = 0
        col = 0
        prev_cat = None
        for theme_id in ids:
            cat = theme.get_theme(theme_id).get("category") or ""
            if cat != prev_cat:
                if col != 0:
                    grid_row += 1
                    col = 0
                heading = tk.Label(self.theme_grid, text=cat, font=(self.family, 10, "bold"),
                                    bg=self._card_bg, fg=theme.TEXT_SECONDARY, anchor="w")
                heading.grid(row=grid_row, column=0, columnspan=cols, sticky="w",
                             padx=6, pady=(14 if prev_cat else 0, 4))
                grid_row += 1
                prev_cat = cat
            cell = tk.Frame(self.theme_grid, bg=self._card_bg)
            cell.grid(row=grid_row, column=col, padx=6, pady=6)

            canvas = tk.Canvas(cell, width=132, height=88, bg=self._card_bg, highlightthickness=0,
                                cursor="hand2")
            canvas.pack()
            canvas.bind("<Button-1>", lambda e, tid=theme_id: self._select_theme(tid))
            self.theme_swatch_canvases[theme_id] = canvas

            label = tk.Label(cell, text=theme.get_theme(theme_id)["label"], font=(self.family, 9),
                              bg=self._card_bg, fg=theme.TEXT_PRIMARY, cursor="hand2")
            label.pack(pady=(4, 0))
            label.bind("<Button-1>", lambda e, tid=theme_id: self._select_theme(tid))
            col += 1
            if col >= cols:
                col = 0
                grid_row += 1

    def _build_custom_controls(self):
        """Four color pickers (Background, Panel, Text, Accent) that make
        up the "Custom" palette -- populates self.custom_controls_frame,
        which _refresh_theme_selection shows/hides depending on whether
        "Custom" is the selected card above. Picking a color updates
        self.custom_seeds, pushes it into theme.set_custom_seeds() right
        away (so the Custom grid card's own preview updates live, exactly
        like ProjectPanel's color picker updates its own swatch), and
        redraws that one card."""
        fields = [
            ("app_bg", "Background"), ("panel_bg", "Panel"),
            ("text_primary", "Text"), ("accent", "Accent"),
        ]
        for i, (key, label) in enumerate(fields):
            cell = tk.Frame(self.custom_controls_frame, bg=self._card_bg)
            cell.grid(row=0, column=i, padx=(0, 20), sticky="w")
            tk.Label(cell, text=label, font=(self.family, 9),
                     bg=self._card_bg, fg=theme.TEXT_SECONDARY).pack(anchor="w")
            picker_row = tk.Frame(cell, bg=self._card_bg)
            picker_row.pack(anchor="w", pady=(2, 0))
            canvas = tk.Canvas(picker_row, width=28, height=28, bg=self._card_bg,
                                highlightthickness=0, cursor="hand2")
            canvas.pack(side="left", padx=(0, 8))
            canvas.bind("<Button-1>", lambda e, k=key: self._pick_custom_color(k))
            self.custom_swatch_canvases[key] = canvas
            RoundedButton(picker_row, text="Choose…", style="Secondary.TButton",
                          command=lambda k=key: self._pick_custom_color(k),
                          bg=self._card_bg).pack(side="left")
        self._draw_custom_swatches()

    def _build_glass_alpha_controls(self):
        """Slider for Frosted / Picom / Hypr — how see-through the window
        is. Floor stays high enough that text remains readable."""
        tk.Label(self.glass_alpha_frame, text="Translucency",
                 font=(self.family, 10, "bold"), bg=self._card_bg,
                 fg=theme.TEXT_PRIMARY).pack(anchor="w")
        tk.Label(self.glass_alpha_frame,
                 text="How much of the desktop shows through. Solid is fully opaque; "
                      "the left end stays readable — never fully transparent.",
                 fg=theme.TEXT_MUTED, bg=self._card_bg, justify="left", wraplength=480,
                 font=(self.family, 9)).pack(anchor="w", pady=(2, 8))
        row = tk.Frame(self.glass_alpha_frame, bg=self._card_bg)
        row.pack(fill="x")
        tk.Label(row, text="See-through", font=(self.family, 8),
                 bg=self._card_bg, fg=theme.TEXT_MUTED).pack(side="left")
        self.glass_alpha_label = tk.Label(row, text="", font=(self.family, 9, "bold"),
                                          bg=self._card_bg, fg=theme.TEXT_PRIMARY)
        self.glass_alpha_label.pack(side="right")
        tk.Label(row, text="Solid", font=(self.family, 8),
                 bg=self._card_bg, fg=theme.TEXT_MUTED).pack(side="right", padx=(0, 12))
        lo = int(round(theme.WINDOW_ALPHA_MIN * 100))
        hi = int(round(theme.WINDOW_ALPHA_MAX * 100))
        self.glass_alpha_var = tk.DoubleVar(value=theme.get_glass_alpha() * 100)
        self.glass_alpha_scale = tk.Scale(
            self.glass_alpha_frame, from_=lo, to=hi, orient="horizontal",
            showvalue=0, resolution=1, length=360,
            bg=self._card_bg, fg=theme.TEXT_PRIMARY, highlightthickness=0,
            troughcolor=theme.FIELD_BG, activebackground=theme.ACCENT,
            sliderrelief="flat", bd=0, command=self._on_glass_alpha)
        self.glass_alpha_scale.pack(fill="x", pady=(4, 0))
        self._sync_glass_alpha_label(theme.get_glass_alpha())

    def _sync_glass_alpha_label(self, alpha: float):
        pct = int(round(alpha * 100))
        self.glass_alpha_label.config(text=f"{pct}% opaque")

    def _on_glass_alpha(self, value):
        alpha = theme.set_glass_alpha(float(value) / 100.0)
        self._sync_glass_alpha_label(alpha)
        try:
            theme.apply_window_opacity(self.winfo_toplevel(), self.theme_var.get())
        except tk.TclError:
            pass

    def _draw_custom_swatches(self):
        for key, canvas in self.custom_swatch_canvases.items():
            canvas.delete("all")
            theme.place_rounded_rect(canvas, 2, 2, 26, 26, radius=6,
                                     fill=self.custom_seeds[key], outline=theme.BORDER_STRONG,
                                     background=canvas.cget("bg"))

    def _pick_custom_color(self, key: str):
        # Native OS color picker, same as ProjectPanel's -- not one of our
        # windows, unaffected by the pop-up-positioning bug this file
        # otherwise works around.
        rgb, hexcode = colorchooser.askcolor(color=self.custom_seeds.get(key), parent=self)
        if not hexcode:
            return
        self.custom_seeds[key] = hexcode
        theme.set_custom_seeds(**self.custom_seeds)
        self._draw_custom_swatches()
        custom_canvas = self.theme_swatch_canvases.get(theme.CUSTOM_THEME_ID)
        if custom_canvas is not None:
            theme.draw_theme_swatch(custom_canvas, theme.CUSTOM_THEME_ID,
                                     selected=(self.theme_var.get() == theme.CUSTOM_THEME_ID))
        if self.theme_var.get() == theme.CUSTOM_THEME_ID:
            self.theme_description_label.config(text=theme.get_theme(theme.CUSTOM_THEME_ID)["description"])

    def _select_theme(self, theme_id: str):
        self.theme_var.set(theme_id)
        self._refresh_theme_selection()

    def _refresh_theme_selection(self):
        selected = self.theme_var.get()
        for theme_id, canvas in self.theme_swatch_canvases.items():
            theme.draw_theme_swatch(canvas, theme_id, selected=(theme_id == selected))
        self.theme_description_label.config(text=theme.get_theme(selected)["description"])
        if self.custom_controls_frame is not None:
            if selected == theme.CUSTOM_THEME_ID:
                self.custom_controls_frame.grid()
            else:
                self.custom_controls_frame.grid_remove()
        if self.glass_alpha_frame is not None:
            if theme.is_glass_theme(selected):
                self.glass_alpha_frame.grid()
            else:
                self.glass_alpha_frame.grid_remove()

    @staticmethod
    def _format_hour(hour_0_23: int) -> str:
        """'9 AM', '5 PM', etc. -- same format calendar_view.py's hour
        gridline labels use. Only ever called with 0-23 (an End-hour
        value of 24 is normalized to 0 -- "12 AM" -- by the caller, same
        "end of day wraps to midnight" convention a 24-hour clock uses)."""
        return datetime.strptime(str(hour_0_23 % 24), "%H").strftime("%I %p").lstrip("0")

    def load(self, display_name: str, current_theme_id: str, work_start_hour: int,
              work_end_hour: int, show_weekends: bool, show_timer_bar: bool, header_style: str,
              on_save: Callable, jira_site_url: str = "", jira_email: str = "",
              jira_api_token: str = "", jira_project_key: str = "QDM",
              on_test_jira: Optional[Callable[[dict], None]] = None,
              on_sync_jira: Optional[Callable[[dict], None]] = None):
        self.on_save = on_save
        self.on_test_jira = on_test_jira
        self.on_sync_jira = on_sync_jira
        self.display_name_var.set(display_name)
        self.jira_url_var.set(jira_site_url)
        self.jira_email_var.set(jira_email)
        self.jira_token_var.set(jira_api_token)
        self.jira_project_key_var.set(jira_project_key or "QDM")
        if jira_api_token:
            self.jira_status_label.config(text="Token saved locally. Test the connection, then Sync QDMs.")
        else:
            self.jira_status_label.config(text="No token yet — paste one above, or set QUASAR_JIRA_TOKEN.")
        self.theme_var.set(theme.resolve_theme_id(current_theme_id))
        self.custom_seeds = theme.get_custom_seeds()
        self._custom_seeds_on_load = dict(self.custom_seeds)
        self._draw_custom_swatches()
        self._glass_alpha_on_load = theme.get_glass_alpha()
        if hasattr(self, "glass_alpha_var"):
            self.glass_alpha_var.set(self._glass_alpha_on_load * 100)
            self._sync_glass_alpha_label(self._glass_alpha_on_load)
        self._refresh_theme_selection()

        start_idx = self._start_hour_values.index(work_start_hour) if work_start_hour in self._start_hour_values else 9
        end_idx = self._end_hour_values.index(work_end_hour) if work_end_hour in self._end_hour_values else 7
        self.work_start_combo.current(start_idx)
        self.work_end_combo.current(end_idx)
        self.show_weekends_var.set(show_weekends)
        self.hide_timer_var.set(not show_timer_bar)
        self._select_header_style(header_style if header_style in self.header_style_buttons else "standard")

    def _save(self):
        assert self.on_save is not None
        start_idx = self.work_start_combo.current()
        end_idx = self.work_end_combo.current()
        work_start_hour = self._start_hour_values[start_idx if start_idx >= 0 else 9]
        work_end_hour = self._end_hour_values[end_idx if end_idx >= 0 else 7]
        if work_end_hour <= work_start_hour:
            messagebox.showwarning(
                "Invalid Work Hours",
                "The “To” time has to be later than the “From” time.")
            return
        self.on_save(
            self.display_name_var.get().strip(),
            self.theme_var.get(),
            work_start_hour,
            work_end_hour,
            self.show_weekends_var.get(),
            not self.hide_timer_var.get(),
            self.header_style_choice,
            self.jira_fields(),
        )
        show_saved_toast(self)
        self.on_close()

    def _cancel(self):
        # Revert any custom-color edits made this time the panel was open
        # but never saved -- otherwise an abandoned tweak would still show
        # up as "current" the next time Settings is reopened, even though
        # it was never actually applied or persisted.
        if self.custom_seeds != self._custom_seeds_on_load:
            theme.set_custom_seeds(**self._custom_seeds_on_load)
        if abs(theme.get_glass_alpha() - self._glass_alpha_on_load) > 0.001:
            theme.set_glass_alpha(self._glass_alpha_on_load)
            try:
                theme.apply_window_opacity(self.winfo_toplevel())
            except tk.TclError:
                pass
        self.on_close()


# ---------------------------------------------------------------------------
# Backup panel -- back up the whole database to a file, or restore from one
# ---------------------------------------------------------------------------
class BackupPanel(tk.Frame):
    """Its own tab (reached from File -> Backup & Restore…) rather than a
    section inside Settings -- it was tried there first, but the Settings
    tab's Display Name/Jira defaults/theme-grid content already fills
    a normal-sized window, and Backup & Restore's own buttons plus Save/
    Cancel ended up pushed entirely off the bottom with no way to scroll to
    them. A separate tab keeps both panels comfortably short."""

    def __init__(self, master, family: str, on_close: Callable[[], None],
                 on_backup: Callable[[str], None], on_restore: Callable[[str], None]):
        super().__init__(master, bg=theme.PANEL_BG)
        self.family = family
        self.on_close = on_close
        self.on_backup = on_backup
        self.on_restore = on_restore

        body = _scroll_body(self)
        outer = tk.Frame(body, bg=theme.PANEL_BG)
        outer.pack(fill="both", expand=True, padx=28, pady=24)

        tk.Label(outer, text="Backup & Restore", font=(self.family, 14, "bold"),
                 bg=theme.PANEL_BG, fg=theme.TEXT_PRIMARY).pack(anchor="w", pady=(0, 16))

        tk.Label(outer, text="Back up everything -- projects, activities, time blocks, the "
                              "template, and your settings -- to a single file, or restore "
                              "from a backup made earlier. Restoring replaces everything "
                              "currently in the app, so back up first if you're at all unsure.",
                 fg=theme.TEXT_MUTED, bg=theme.PANEL_BG, justify="left", wraplength=440,
                 font=(self.family, 9)).pack(anchor="w", pady=(0, 20))

        # Immediate actions, not staged fields to Save/Cancel -- each opens a
        # native file dialog (positioned by the OS, same as
        # colorchooser.askcolor in ProjectPanel above, so the embedded-tab-
        # instead-of-popup rationale in this module's docstring doesn't
        # apply here) and takes effect the moment a file is chosen.
        btn_row = ttk.Frame(outer)
        btn_row.pack(anchor="w", pady=(0, 24))
        RoundedButton(btn_row, text="Back Up Data…", style="Accent.TButton",
                      command=self._backup).pack(side="left")
        RoundedButton(btn_row, text="Restore from Backup…", style="Secondary.TButton",
                      command=self._restore).pack(side="left", padx=(8, 0))

        close_row = ttk.Frame(outer)
        close_row.pack(anchor="w", fill="x")
        RoundedButton(close_row, text="Close", style="Secondary.TButton",
                      command=self.on_close).pack(side="right")

    def _backup(self):
        default_name = f"free-timesheet-backup-{date.today().isoformat()}.db"
        path = filedialog.asksaveasfilename(
            title="Back Up Data", defaultextension=".db", initialfile=default_name,
            filetypes=[("Timesheet Backup", "*.db"), ("All files", "*.*")])
        if not path:
            return
        self.on_backup(path)

    def _restore(self):
        path = filedialog.askopenfilename(
            title="Restore from Backup",
            filetypes=[("Timesheet Backup", "*.db"), ("All files", "*.*")])
        if not path:
            return
        if not messagebox.askyesno(
                "Restore from Backup",
                "This replaces every project, activity, time block, and template entry "
                "currently in the app with what's in this backup file. This can't be "
                "undone. Continue?",
                icon="warning"):
            return
        self.on_restore(path)


# ---------------------------------------------------------------------------
# Export panel (choose date range)
# ---------------------------------------------------------------------------
class ExportPanel(tk.Frame):
    def __init__(self, master, family: str, on_close: Callable[[], None]):
        super().__init__(master, bg=theme.PANEL_BG)
        self.family = family
        self.on_close = on_close
        self.on_export: Optional[Callable[[str, str], None]] = None
        self.on_push: Optional[Callable[[str, str], None]] = None
        self.week_start: Optional[date] = None

        body = _scroll_body(self)
        outer = tk.Frame(body, bg=theme.PANEL_BG)
        outer.pack(fill="both", expand=True, padx=28, pady=24)

        tk.Label(outer, text="Log hours to Jira", font=(self.family, 16, "bold"),
                 bg=theme.PANEL_BG, fg=theme.TEXT_PRIMARY).pack(anchor="w", pady=(0, 6))
        tk.Label(outer, text="Push worklogs straight to each QDM. CSV export is still here "
                             "as a fallback. Only blocks with a Jira Issue Key are sent.",
                 font=(self.family, 10), bg=theme.PANEL_BG, fg=theme.TEXT_SECONDARY,
                 wraplength=520, justify="left").pack(anchor="w", pady=(0, 16))

        frm = ttk.Frame(outer)
        frm.pack(anchor="w")

        ttk.Label(frm, text="Export range", font=(self.family, 10, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 8))
        self.scope_var = tk.StringVar(value="week")
        self.week_radio = ttk.Radiobutton(frm, text="", variable=self.scope_var, value="week",
                                           command=self._toggle)
        self.week_radio.grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(frm, text="Custom date range", variable=self.scope_var, value="custom",
                        command=self._toggle).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        ttk.Label(frm, text="From (YYYY-MM-DD)").grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.from_var = tk.StringVar()
        self.from_entry = ttk.Entry(frm, textvariable=self.from_var, width=14, state="disabled")
        self.from_entry.grid(row=3, column=1, sticky="w", pady=(10, 0))

        ttk.Label(frm, text="To (YYYY-MM-DD)").grid(row=4, column=0, sticky="w")
        self.to_var = tk.StringVar()
        self.to_entry = ttk.Entry(frm, textvariable=self.to_var, width=14, state="disabled")
        self.to_entry.grid(row=4, column=1, sticky="w")

        self.error_label = ttk.Label(frm, text="", foreground=theme.DANGER)
        self.error_label.grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))

        btns = ttk.Frame(frm)
        btns.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        RoundedButton(btns, text="Cancel", style="Secondary.TButton", command=self._cancel).pack(side="right")
        RoundedButton(btns, text="Export CSV…", style="Secondary.TButton", command=self._export).pack(
            side="right", padx=6)
        RoundedButton(btns, text="Push to Jira", style="Accent.TButton", command=self._push).pack(
            side="right", padx=(0, 6))

    def load(self, week_start: date, on_export: Callable[[str, str], None],
              on_push: Optional[Callable[[str, str], None]] = None):
        self.week_start = week_start
        self.on_export = on_export
        self.on_push = on_push
        self.scope_var.set("week")
        # config.week_end_offset() is 4 for a plain Mon-Fri week, 6 once
        # Settings' "Show weekends" is on -- matches whatever the
        # calendar itself currently shows instead of a Mon-Fri window
        # baked in regardless.
        week_end = week_start + timedelta(days=config.week_end_offset())
        self.week_radio.config(text=f"Current week ({week_start.strftime('%b %d')} - "
                                     f"{week_end.strftime('%b %d, %Y')})")
        self.from_var.set(week_start.isoformat())
        self.to_var.set(week_end.isoformat())
        self.from_entry.config(state="disabled")
        self.to_entry.config(state="disabled")
        self.error_label.config(text="")

    def _toggle(self):
        state = "normal" if self.scope_var.get() == "custom" else "disabled"
        self.from_entry.config(state=state)
        self.to_entry.config(state=state)

    def _selected_range(self) -> Optional[tuple]:
        assert self.week_start is not None
        if self.scope_var.get() == "week":
            start = self.week_start.isoformat()
            end = (self.week_start + timedelta(days=config.week_end_offset())).isoformat()
            return start, end
        start = self.from_var.get().strip()
        end = self.to_var.get().strip()
        try:
            date.fromisoformat(start)
            date.fromisoformat(end)
        except ValueError:
            self.error_label.config(text="Dates must be in YYYY-MM-DD format.")
            return None
        if start > end:
            self.error_label.config(text="'From' date must be before 'To' date.")
            return None
        return start, end

    def _export(self):
        selected = self._selected_range()
        if selected is None:
            return
        start, end = selected
        cb = self.on_export
        self.on_close()
        assert cb is not None
        cb(start, end)

    def _push(self):
        selected = self._selected_range()
        if selected is None:
            return
        start, end = selected
        cb = self.on_push
        if cb is None:
            self.error_label.config(text="Push to Jira isn't wired up in this build.")
            return
        self.on_close()
        cb(start, end)

    def _cancel(self):
        self.on_close()
