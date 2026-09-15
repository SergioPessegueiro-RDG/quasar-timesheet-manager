"""Sidebar: saved Activities list with add/edit/delete + quick-assign
arming, organized into collapsible Projects. Every Activity belongs to
exactly one Project -- there's no "ungrouped" state -- since a time block's
color always comes from its Activity's Project (see app/models.py)."""
import tkinter as tk
from tkinter import messagebox
from typing import Callable, Dict, List, Optional

from . import config, theme
from .db import Database
from .models import Activity, Project
from .widgets import CARD_RADIUS, RoundedButton, RoundedCard, ScrollArea

# Rows are built once per refresh and shown/hidden with pack, so typing
# in search never destroys the list. Closed tickets start collapsed.
# Packing chrome around an activity name (card pad, row pad, indent, dot).
_NAME_WRAP_CHROME_PX = 80
_SEARCH_HINT = "Search name or number…"


def qdm_haystack(activity: Activity) -> str:
    """Precomputed lowercase blob for live search (name, key, number)."""
    key = (activity.jira_key or "").strip()
    number = config.jira_key_number(key)
    return "\n".join((
        (activity.name or "").lower(),
        key.lower(),
        number.lower(),
        number.replace("-", "").lower(),
        "".join(c for c in key if c.isdigit()),
    ))


def qdm_matches(activity: Activity, query: str) -> bool:
    """True if `query` is empty or matches the QDM's name / Jira key / number."""
    q = (query or "").strip().lower()
    if not q:
        return True
    hay = qdm_haystack(activity)
    if q in hay:
        return True
    digits = "".join(c for c in q if c.isdigit())
    return bool(digits) and digits in hay


def qdm_combo_rows(activities: List[Activity]):
    """Names, search haystacks, and 'KEY  name' labels for a QDM dropdown."""
    names: List[str] = []
    hays: List[str] = []
    labels: List[str] = []
    for act in activities:
        names.append(act.name)
        hays.append(qdm_haystack(act))
        key = (act.jira_key or "").strip()
        labels.append(f"{key}  {act.name}" if key else act.name)
    return names, hays, labels


class Sidebar(tk.Frame):
    def __init__(self, master, db: Database, on_change: Callable[[], None],
                 open_activity_panel: Callable[..., None],
                 open_project_panel: Callable[..., None],
                 collapsed: bool = False,
                 on_toggle_collapse: Optional[Callable[[], None]] = None,
                 on_jira_sync: Optional[Callable[[], None]] = None,
                 content_width: Optional[int] = None, **kwargs):
        kwargs.setdefault("bg", theme.APP_BG)
        kwargs.setdefault("highlightthickness", 0)
        super().__init__(master, **kwargs)
        self.db = db
        self.on_change = on_change  # called whenever armed activity or list/project state changes
        self.open_activity_panel = open_activity_panel
        self.open_project_panel = open_project_panel
        self.on_jira_sync = on_jira_sync
        # Collapsed is fixed for this instance's whole lifetime -- toggling
        # it (see on_toggle_collapse, called from main_window.py) triggers
        # a full window rebuild rather than switching this Sidebar between
        # modes in place, since the column width that holds it is owned by
        # MainWindow's grid, not by this widget. That keeps _render_rows
        # below simple: it only ever needs to render the one mode this
        # instance was built for.
        self.collapsed = collapsed
        self.on_toggle_collapse = on_toggle_collapse
        self.armed_activity_id: Optional[int] = None
        self.family = theme.resolve_font_family()
        self._activities: List[Activity] = []
        self._projects: List[Project] = []
        self._sidebar_px = content_width or config.DEFAULT_SIDEBAR_WIDTH_PX
        self._name_labels: List[tk.Label] = []
        self._calendar = None
        self._row_press: Optional[dict] = None
        self._arm_job = None
        self._search_placeholder_on = False
        self.search_var = tk.StringVar()
        self._search_job = None
        self._filter_items: List[dict] = []
        self._empty_search = None
        self._activity_rows_complete = False
        self._rows_include_collapsed_children = False
        # Project ids whose closed-ticket subsection is currently expanded.
        # In-memory only: closed lists start collapsed every session so a
        # large backlog doesn't paint itself on launch.
        self._show_closed_for: set = set()

        card = RoundedCard(
            self, bg=theme.PANEL_BG,
            radius=6 if self.collapsed else CARD_RADIUS,
            pad=6 if self.collapsed else None, outline=False)
        side_pad = (6, 2) if self.collapsed else (12, 8)
        card.pack(fill="both", expand=True, padx=side_pad, pady=10 if self.collapsed else 12)
        inner = card.body

        # list_container/canvas stay None in collapsed mode -- there's no
        # ScrollArea there (see below), so nothing sets them.
        self.list_container: Optional[ScrollArea] = None
        self.canvas: Optional[tk.Canvas] = None

        if self.collapsed:
            # A narrow icon rail instead of the full card: a round expand
            # button, a rotated "QDM's" label so it's obvious what's
            # hidden, then one small color dot per Project (no
            # Activities, no scrolling) as a quick visual reminder of
            # what's hidden. self.list_frame still exists here (holding
            # just the dots) so refresh()/_render_rows() below don't need
            # two separate call paths for external callers like
            # MainWindow._on_sidebar_change.
            #
            # This went through two earlier designs before landing here.
            # First, a bare 3-character "»" button was "extremely
            # difficult" to hit on the ~40px rail. Then a full-width
            # Accent-styled button was bigger but, combined with the
            # padding bug described above, rendered as a clipped sliver
            # instead of an actual button. This version fixes the real
            # spacing bug AND -- per the user's own pick from three
            # mocked-up options -- combines the compact round icon button
            # from one option with the rotated text label from another,
            # so there's both a proper-sized click target and a label
            # that reads at a glance. The whole rail (not just the
            # button) still re-expands on click, all with a hand cursor,
            # so hitting the exact button is never required.
            top_row = tk.Frame(inner, bg=theme.PANEL_BG)
            top_row.pack(pady=(2, 10))
            expand_btn = tk.Canvas(top_row, width=32, height=32, bg=theme.PANEL_BG,
                                    highlightthickness=0, cursor="hand2")
            expand_btn.create_oval(0, 0, 32, 32, fill=theme.ACCENT, outline="")
            expand_btn.create_line(12, 9, 20, 16, 12, 23, fill="#FFFFFF", width=3,
                                    capstyle="round", joinstyle="round")
            expand_btn.pack()
            if self.on_toggle_collapse is not None:
                expand_btn.bind("<Button-1>", lambda e: self.on_toggle_collapse())

            # A Canvas (not a Label) so the text can be drawn rotated --
            # Tk Labels have no rotation option, but Canvas.create_text
            # does via `angle=`. Sized just wide enough for the rotated
            # text's height (its *width* once rotated 90 degrees) plus a
            # little breathing room either side.
            label_canvas = tk.Canvas(inner, width=22, height=74, bg=theme.PANEL_BG,
                                      highlightthickness=0, cursor="hand2")
            label_canvas.create_text(11, 37, text="QDM's", angle=90,
                                      fill=theme.TEXT_SECONDARY,
                                      font=(self.family, 10, "bold"))
            label_canvas.pack(pady=(0, 12))

            self.list_frame = tk.Frame(inner, bg=theme.PANEL_BG)
            self.list_frame.pack(fill="both", expand=True)
            if self.on_toggle_collapse is not None:
                for widget in (self, inner, top_row, label_canvas, self.list_frame):
                    widget.configure(cursor="hand2")
                    widget.bind("<Button-1>", lambda e: self.on_toggle_collapse())
        else:
            header = tk.Frame(inner, bg=theme.PANEL_BG)
            header.pack(fill="x", pady=(0, 8))
            tk.Label(header, text="QDM's", font=(self.family, 13, "bold"),
                     bg=theme.PANEL_BG, fg=theme.TEXT_PRIMARY).pack(side="left")
            if self.on_toggle_collapse is not None:
                RoundedButton(header, text="«", width=3, style="Nav.TButton", compact=True,
                              command=self.on_toggle_collapse).pack(side="right")
            if self.on_jira_sync is not None:
                RoundedButton(header, text="Sync", style="Accent.TButton",
                              command=self.on_jira_sync).pack(side="right", padx=(0, 6))

            search_row = tk.Frame(inner, bg=theme.PANEL_BG)
            search_row.pack(fill="x", pady=(0, 10))
            # shrink=True: this card is packed fill="x", so `.body` must
            # pack (not place) or the field collapses to 0px tall.
            search_card = RoundedCard(
                search_row, bg=theme.FIELD_BG, radius=10, pad=10, outline=False,
                shrink=True)
            search_card.pack(side="left", fill="x", expand=True)
            self.search_entry = tk.Entry(
                search_card.body, textvariable=self.search_var, font=(self.family, 10),
                relief="flat", bd=0, bg=theme.FIELD_BG, fg=theme.TEXT_MUTED,
                insertbackground=theme.TEXT_PRIMARY, highlightthickness=0)
            self.search_entry.pack(fill="x", ipady=2)
            self._search_placeholder_on = True
            self.search_var.set(_SEARCH_HINT)
            self.search_entry.bind("<FocusIn>", self._search_focus_in)
            self.search_entry.bind("<FocusOut>", self._search_focus_out)
            self.search_var.trace_add("write", lambda *_: self._on_search_changed())

            # ScrollArea is itself a RoundedCard with a scrolling interior
            # built in -- see app/widgets.py. Outline off and page-colored
            # so the list sits on the window, not inside another panel.
            scroll_area = ScrollArea(inner, bg=theme.PANEL_BG, outline=False, pad=0)
            scroll_area.pack(fill="both", expand=True)
            self.list_container = scroll_area
            self.canvas = scroll_area.canvas
            self.list_frame = scroll_area.content

        self.refresh()

    def set_calendar(self, calendar):
        """The grid this sidebar drops QDMs onto (Timesheet or Template)."""
        self._calendar = calendar

    def _search_query(self) -> str:
        if self.collapsed or self._search_placeholder_on:
            return ""
        return (self.search_var.get() or "").strip()

    def _search_focus_in(self, _event=None):
        if self._search_placeholder_on:
            self._search_placeholder_on = False
            self.search_var.set("")
            self.search_entry.configure(fg=theme.TEXT_PRIMARY)

    def _search_focus_out(self, _event=None):
        if not (self.search_var.get() or "").strip():
            self._search_placeholder_on = True
            self.search_var.set(_SEARCH_HINT)
            self.search_entry.configure(fg=theme.TEXT_MUTED)

    def _on_search_changed(self):
        if self.collapsed or self.list_container is None:
            return
        job = self._search_job
        if job is not None:
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
        self._search_job = self.after(50, self._flush_search)

    def _flush_search(self):
        self._search_job = None
        q = bool(self._search_query().strip())
        # Collapsed groups skip activity widgets until needed. Rebuild
        # only when that set of widgets must change — not on every letter.
        if q and not self._activity_rows_complete:
            self._render_rows()
            return
        if (not q) and self._rows_include_collapsed_children:
            self._render_rows()
            return
        self._apply_filter()

    def _track(self, widget, pack, **meta):
        item = {"widget": widget, "pack": pack}
        item.update(meta)
        self._filter_items.append(item)
        return item

    def _apply_filter(self):
        """Show/hide already-built rows. Typing must not destroy widgets."""
        if self.collapsed or not self._filter_items:
            return
        q = self._search_query().lower()
        collapsed_ids = {p.id for p in self._projects if p.collapsed}
        matching_projects = set()
        if q:
            for item in self._filter_items:
                if item["kind"] == "activity":
                    matched = q in item["haystack"]
                    item["_match"] = matched
                    if matched:
                        matching_projects.add(item["project_id"])
        else:
            for item in self._filter_items:
                if item["kind"] == "activity":
                    item["_match"] = True

        any_activity = False
        for item in self._filter_items:
            kind = item["kind"]
            pid = item.get("project_id")
            vis = False
            if kind == "project":
                vis = (not q) or (pid in matching_projects)
            elif kind == "activity":
                if q:
                    vis = item.get("_match", False)
                else:
                    vis = pid not in collapsed_ids
                    if item["closed"]:
                        vis = vis and pid in self._show_closed_for
                any_activity = any_activity or vis
            elif kind == "closed_toggle":
                vis = (not q) and pid not in collapsed_ids
                self._sync_closed_arrow(item)
            elif kind == "extra":
                vis = (not q) and pid not in collapsed_ids
                if item.get("closed"):
                    vis = vis and pid in self._show_closed_for
            elif kind == "hint":
                vis = (not q) and pid not in collapsed_ids
            elif kind == "empty_search":
                vis = False
            item["_vis"] = vis

        if q:
            for item in self._filter_items:
                if item["kind"] == "empty_search":
                    item["_vis"] = not any_activity

        for item in self._filter_items:
            item["widget"].pack_forget()
        for item in self._filter_items:
            if item.get("_vis"):
                item["widget"].pack(**item["pack"])

    def _sync_closed_arrow(self, item):
        canvas = item.get("arrow")
        if canvas is None:
            return
        expanded = item["project_id"] in self._show_closed_for
        canvas.delete("all")
        if expanded:
            points = (2, 3, 10, 3, 6, 9)
        else:
            points = (3, 2, 3, 10, 9, 6)
        canvas.create_polygon(*points, fill=theme.TEXT_MUTED, outline="")

    # ------------------------------------------------------------------
    def get_armed_activity(self) -> Optional[Activity]:
        if self.armed_activity_id is None:
            return None
        return self.db.get_activity(self.armed_activity_id)

    def clear_armed(self):
        self.armed_activity_id = None
        self._render_rows()
        self.on_change()

    # ------------------------------------------------------------------
    def refresh(self):
        self._activities = self.db.list_activities()
        self._projects = self.db.list_projects()
        self._render_rows()

    def set_content_width(self, sidebar_px: int):
        """Keep QDM names wrapping to the current sash width without
        rebuilding every row on each drag pixel."""
        if self.collapsed:
            return
        sidebar_px = int(sidebar_px)
        if sidebar_px == int(self._sidebar_px):
            return
        self._sidebar_px = sidebar_px
        for lbl in self._name_labels:
            self._sync_wraplength(lbl)

    def _name_wraplength(self, extra_chrome: int = 0) -> int:
        return max(60, int(self._sidebar_px) - _NAME_WRAP_CHROME_PX - extra_chrome)

    def _attach_wraplength(self, lbl: tk.Label, extra_chrome: int = 0):
        """Wrap to the label's allocated width so the count/arrow/dot on
        the same row cannot clip the last letters of a long name."""
        lbl.configure(wraplength=self._name_wraplength(extra_chrome))
        lbl.bind("<Configure>", lambda e, widget=lbl: self._sync_wraplength(widget, e.width))
        self._name_labels.append(lbl)

    def _sync_wraplength(self, lbl: tk.Label, width: Optional[int] = None):
        try:
            allocated = int(width) if width else int(lbl.winfo_width())
        except tk.TclError:
            return
        wrap = max(40, allocated - 4) if allocated > 1 else self._name_wraplength()
        try:
            current = int(float(lbl.cget("wraplength") or 0))
        except (tk.TclError, TypeError, ValueError):
            current = 0
        if current != wrap:
            try:
                lbl.configure(wraplength=wrap)
            except tk.TclError:
                pass

    def _render_rows(self):
        self._name_labels = []
        self._filter_items = []
        self._empty_search = None
        for child in self.list_frame.winfo_children():
            child.destroy()

        if self.collapsed:
            self._activity_rows_complete = True
            self._rows_include_collapsed_children = False
            self._render_collapsed_rail()
            return

        searching = bool(self._search_query().strip())
        skipped_collapsed = False
        self._rows_include_collapsed_children = False

        if not self._activities or not any(not a.is_closed() for a in self._activities):
            empty = tk.Label(self.list_frame, text="No QDM's yet. Sync from Jira, or File → New QDM.",
                      fg=theme.TEXT_MUTED, bg=theme.PANEL_BG,
                      font=(self.family, 9), justify="left")
            empty.pack(pady=14, padx=8)
            self._attach_wraplength(empty)
            self._activity_rows_complete = True
            self._rows_include_collapsed_children = False
            self.list_container.bind_wheel_recursive(self.list_frame)
            return

        by_project: Dict[int, List[Activity]] = {}
        for act in self._activities:
            if act.project_id is not None:
                by_project.setdefault(act.project_id, []).append(act)

        for project in self._projects:
            assert project.id is not None
            members = by_project.get(project.id, [])
            if not members:
                # No ticket assigned to you in this group — hide the empty
                # epic/project shell (leftover after a sync archived them).
                continue
            open_acts = [a for a in members if not a.is_closed()]
            closed_acts = [a for a in members if a.is_closed()]
            if not open_acts:
                # Closed-only groups used to show "(0, 5 closed)" with
                # nothing to expand into. Hide them; closed tickets still
                # sit under a group that has open work assigned to you.
                continue
            self._render_project_header(project, len(open_acts), len(closed_acts))
            show_children = searching or not project.collapsed
            if not show_children:
                skipped_collapsed = True
                continue
            if project.collapsed:
                self._rows_include_collapsed_children = True
            for act in open_acts:
                self._render_activity_row(act)
            if closed_acts:
                self._render_closed_toggle(project, len(closed_acts))
                if searching or project.id in self._show_closed_for:
                    for act in closed_acts:
                        self._render_activity_row(act)

        empty = tk.Label(self.list_frame, text="No QDMs match that search.",
                         fg=theme.TEXT_MUTED, bg=theme.PANEL_BG,
                         font=(self.family, 9), justify="left")
        self._empty_search = empty
        self._track(empty, dict(pady=14, padx=8), kind="empty_search")
        self._attach_wraplength(empty)

        self._activity_rows_complete = not skipped_collapsed
        self._apply_filter()
        # Belt-and-braces alongside ScrollArea's own auto-rebind-on-resize
        # (see widgets.ScrollArea._on_content_configure) -- collapsing/
        # expanding a project can leave the list's overall height unchanged
        # (one project's rows appear as another's disappear), which
        # wouldn't otherwise trigger a re-bind of the freshly-created rows.
        self.list_container.bind_wheel_recursive(self.list_frame)

    def _chip(self, bg: str, pack: dict, **meta):
        """One rounded project/QDM row inside the sidebar card."""
        card = RoundedCard(
            self.list_frame, bg=bg, radius=10, pad=10,
            outline=False, shrink=True, outer_bg=theme.PANEL_BG)
        self._track(card, pack, **meta)
        inner = tk.Frame(card.body, bg=bg)
        inner.pack(fill="both", expand=True)
        return card, inner

    def _render_collapsed_rail(self):
        """Collapsed mode's whole "list": one small color dot per Project,
        no Activities -- just enough to remember what's hidden while the
        rail is narrow. Clicking any dot re-expands, same as the round
        button and "QDM's" label above it (and the rest of the rail)."""
        for project in self._projects:
            if not any(a.project_id == project.id and not a.is_closed()
                       for a in self._activities):
                continue
            dot = tk.Canvas(self.list_frame, width=12, height=12, bg=theme.PANEL_BG,
                             highlightthickness=0, cursor="hand2")
            theme.place_rounded_rect(
                dot, 1, 1, 11, 11, radius=5,
                fill=theme.block_fill(project.color), outline="",
                background=theme.PANEL_BG, full=True)
            dot.pack(pady=4)
            if self.on_toggle_collapse is not None:
                dot.bind("<Button-1>", lambda e: self.on_toggle_collapse())

    def _render_activity_row(self, act: Activity):
        armed = act.id == self.armed_activity_id
        closed = act.is_closed()
        row_bg = theme.ACCENT_SOFT if armed else theme.HEADER_BG
        card, row = self._chip(
            row_bg, dict(fill="x", padx=(16, 6), pady=(2, 2)),
            kind="activity", activity=act, project_id=act.project_id,
            closed=closed, haystack=qdm_haystack(act))

        # Every Activity lives inside a Project, so it's always indented
        # under that Project's header to keep the grouping visually obvious.
        spacer = tk.Frame(row, bg=row_bg, width=8)
        spacer.pack(side="left", fill="y")

        accent_bar = tk.Frame(row, bg=(theme.ACCENT if armed else row_bg), width=3)
        accent_bar.pack(side="left", fill="y")

        content = tk.Frame(row, bg=row_bg)
        content.pack(side="left", fill="both", expand=True, padx=(9, 8), pady=8)

        top_line = tk.Frame(content, bg=row_bg)
        top_line.pack(fill="x")
        # This dot shows act.color -- which is really its Project's color,
        # joined in by Database.list_activities() (see app/models.py's
        # Activity docstring) -- as a reminder of which Project it belongs
        # to even when its Project header has scrolled out of view.
        dot = tk.Canvas(top_line, width=10, height=10, bg=row_bg, highlightthickness=0)
        theme.place_rounded_rect(
            dot, 0, 0, 10, 10, radius=5,
            fill=theme.block_fill(act.color), outline="",
            background=row_bg, full=True)
        dot.pack(side="left", padx=(0, 7))
        if armed:
            name_fg = theme.ACCENT
        elif closed:
            name_fg = theme.TEXT_MUTED
        else:
            name_fg = theme.TEXT_PRIMARY
        weight = "bold" if armed else "normal"
        slant = "overstrike" if closed else ""
        font_style = f"{weight} {slant}".strip()
        name_lbl = tk.Label(
            top_line, text=act.name, bg=row_bg, fg=name_fg, anchor="w",
            justify="left", font=(self.family, 10, font_style))
        name_lbl.pack(side="left", fill="x", expand=True)
        self._attach_wraplength(name_lbl)
        if closed:
            badge = tk.Label(
                top_line, text=(act.jira_status or "Closed").upper(),
                bg=row_bg, fg=theme.TEXT_MUTED, anchor="e",
                font=(self.family, 7, "bold"))
            badge.pack(side="right", padx=(6, 0))

        meta_bits = []
        if closed and act.jira_status:
            meta_bits.append(act.jira_status)
        elif closed:
            meta_bits.append("Closed")
        if act.jira_key:
            meta_bits.append(act.jira_key)
        if act.issue_type:
            meta_bits.append(act.issue_type)
        if act.default_duration_minutes:
            meta_bits.append(f"{act.default_duration_minutes} min")
        if meta_bits:
            meta_lbl = tk.Label(content, text="   ·   ".join(meta_bits), bg=row_bg,
                     fg=theme.TEXT_MUTED if closed else theme.TEXT_SECONDARY,
                     anchor="w", justify="left", font=(self.family, 8))
            meta_lbl.pack(fill="x", pady=(2, 0))
            self._attach_wraplength(meta_lbl)

        def bind_all(widget):
            widget.bind("<ButtonPress-1>", lambda e, a=act: self._on_qdm_press(e, a))
            widget.bind("<B1-Motion>", lambda e, a=act: self._on_qdm_motion(e, a))
            widget.bind("<ButtonRelease-1>", lambda e, a=act: self._on_qdm_release(e, a))
            widget.bind("<Double-Button-1>", lambda e, a=act: self._edit_activity(a))
            widget.bind("<Button-3>", lambda e, a=act: self._context_menu(e, a))

        clickable = [card, row, content, top_line, dot] + content.winfo_children() + top_line.winfo_children()
        for widget in clickable:
            bind_all(widget)
        if not armed:
            self._bind_hover(row, row_bg, theme.SURFACE, card=card)

    def _set_tree_bg(self, widget, bg: str):
        try:
            if widget.winfo_class() in ("Frame", "Label", "Canvas"):
                widget.configure(bg=bg)
        except tk.TclError:
            return
        for child in widget.winfo_children():
            self._set_tree_bg(child, bg)

    def _bind_hover(self, row, rest_bg: str, hover_bg: str, card=None):
        if rest_bg == hover_bg:
            return

        def enter(_event=None):
            self._set_tree_bg(row, hover_bg)
            if card is not None:
                card.set_fill(hover_bg)

        def leave(event):
            try:
                w = event.widget.winfo_containing(event.x_root, event.y_root)
            except tk.TclError:
                w = None
            cur = w
            while cur is not None:
                if cur is row or cur is card:
                    return
                cur = getattr(cur, "master", None)
            self._set_tree_bg(row, rest_bg)
            if card is not None:
                card.set_fill(rest_bg)

        def bind(widget):
            widget.bind("<Enter>", enter, add="+")
            widget.bind("<Leave>", leave, add="+")
            for child in widget.winfo_children():
                bind(child)

        bind(row)
        if card is not None:
            card.bind("<Enter>", enter, add="+")
            card.bind("<Leave>", leave, add="+")

    def _render_closed_toggle(self, project: Project, closed_count: int):
        expanded = project.id in self._show_closed_for
        row = tk.Frame(self.list_frame, bg=theme.PANEL_BG, cursor="hand2")

        arrow_canvas = tk.Canvas(row, width=12, height=12, bg=theme.PANEL_BG,
                                  highlightthickness=0, cursor="hand2")
        arrow_canvas.pack(side="left", padx=(0, 4), pady=3)
        if expanded:
            points = (2, 3, 10, 3, 6, 9)
        else:
            points = (3, 2, 3, 10, 9, 6)
        arrow_canvas.create_polygon(*points, fill=theme.TEXT_MUTED, outline="")

        label = tk.Label(
            row, text=f"Closed ({closed_count})", bg=theme.PANEL_BG,
            fg=theme.TEXT_MUTED, font=(self.family, 9, "italic"),
            cursor="hand2")
        label.pack(side="left")
        self._track(row, dict(fill="x", padx=(28, 6), pady=(4, 2)),
                    kind="closed_toggle", project_id=project.id, arrow=arrow_canvas)

        def toggle(_event=None, p=project):
            pid = p.id
            if pid in self._show_closed_for:
                self._show_closed_for.discard(pid)
            else:
                self._show_closed_for.add(pid)
            self._render_rows()

        for widget in (row, arrow_canvas, label):
            widget.bind("<Button-1>", toggle)

    def _render_project_header(self, project: Project, open_count: int, closed_count: int = 0):
        card, row = self._chip(
            theme.HEADER_BG, dict(fill="x", padx=6, pady=(6, 3)),
            kind="project", project_id=project.id)
        content = tk.Frame(row, bg=theme.HEADER_BG)
        content.pack(side="left", fill="both", expand=True, padx=(6, 8), pady=7)

        # A small vector-drawn triangle rather than a Unicode arrow glyph
        # (▾/▸) -- same reasoning as theme.draw_logo_mark/
        # draw_theme_swatch elsewhere in the app: glyph availability isn't
        # guaranteed across every OS/font combination, but a couple of
        # drawn lines always render the same everywhere.
        arrow_canvas = tk.Canvas(content, width=12, height=12, bg=theme.HEADER_BG,
                                  highlightthickness=0, cursor="hand2")
        arrow_canvas.pack(side="left", padx=(0, 2))
        if project.collapsed:
            points = (3, 2, 3, 10, 9, 6)   # pointing right
        else:
            points = (2, 3, 10, 3, 6, 9)   # pointing down
        arrow_canvas.create_polygon(*points, fill=theme.TEXT_SECONDARY, outline="")
        # Only the disclosure arrow toggles collapse -- toggling round-trips
        # through the database and refreshes every sidebar/calendar sharing
        # it (see _toggle_project), so keeping that off the rest of the row
        # means a double-click there reliably opens Edit instead of racing
        # a rebuild triggered by the first click.
        arrow_canvas.bind("<Button-1>", lambda e, p=project: self._toggle_project(p))

        # A small color swatch so the Project's color -- what every one of
        # its Activities' time blocks actually shows -- is visible right on
        # its own header, not just inferred from its Activities' dots.
        swatch = tk.Canvas(content, width=10, height=10, bg=theme.HEADER_BG, highlightthickness=0)
        theme.place_rounded_rect(
            swatch, 0, 0, 10, 10, radius=5,
            fill=theme.block_fill(project.color), outline="",
            background=theme.HEADER_BG, full=True)
        swatch.pack(side="left", padx=(0, 6))

        count_bits = []
        if open_count:
            count_bits.append(str(open_count))
        if closed_count:
            count_bits.append(f"{closed_count} closed")
        if count_bits:
            count_lbl = tk.Label(content, text=f"({', '.join(count_bits)})",
                                 bg=theme.HEADER_BG, fg=theme.TEXT_MUTED,
                                 font=(self.family, 8))
            # Stay on the first line when the name wraps underneath.
            count_lbl.pack(side="right", padx=(6, 0), anchor="n")
        name_lbl = tk.Label(
            content, text=project.name, bg=theme.HEADER_BG, fg=theme.TEXT_PRIMARY,
            anchor="w", justify="left", font=(self.family, 10, "bold"))
        name_lbl.pack(side="left", fill="x", expand=True)
        # Arrow + swatch + "(4)" live on this row, so the first wraplength
        # guess has to leave room for them; <Configure> then tightens to
        # the label's real allocated width.
        self._attach_wraplength(name_lbl, extra_chrome=56)

        def bind_edit_and_menu(widget):
            widget.bind("<Double-Button-1>", lambda e, p=project: self._edit_project(p))
            widget.bind("<Button-3>", lambda e, p=project: self._project_context_menu(e, p))

        for widget in [card, row, content] + content.winfo_children():
            bind_edit_and_menu(widget)
        self._bind_hover(row, theme.HEADER_BG, theme.SURFACE, card=card)

    # ------------------------------------------------------------------
    # Activities
    # ------------------------------------------------------------------
    def _arm(self, activity: Activity):
        self.armed_activity_id = None if self.armed_activity_id == activity.id else activity.id
        self._render_rows()
        self.on_change()

    def _on_qdm_press(self, event, activity: Activity):
        self._row_press = {
            "activity": activity,
            "x": event.x_root,
            "y": event.y_root,
            "moved": False,
            "dropping": False,
        }

    def _on_qdm_motion(self, event, activity: Activity):
        press = self._row_press
        if press is None or press["activity"].id != activity.id or press["dropping"]:
            return
        dx = event.x_root - press["x"]
        dy = event.y_root - press["y"]
        if abs(dx) <= config.DRAG_THRESHOLD_PX and abs(dy) <= config.DRAG_THRESHOLD_PX:
            return
        press["moved"] = True
        press["dropping"] = True
        if self._calendar is not None:
            self._calendar.begin_qdm_drop(activity, event)

    def _on_qdm_release(self, event, activity: Activity):
        press = self._row_press
        self._row_press = None
        if press is None or press.get("dropping") or press.get("moved"):
            return
        self._cancel_pending_arm()
        self._arm_job = self.after(220, lambda a=activity: self._commit_arm(a))

    def _commit_arm(self, activity: Activity):
        self._arm_job = None
        self._arm(activity)

    def _cancel_pending_arm(self):
        if self._arm_job is not None:
            try:
                self.after_cancel(self._arm_job)
            except tk.TclError:
                pass
            self._arm_job = None

    def _context_menu(self, event, activity: Activity):
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Edit…", command=lambda: self._edit_activity(activity))
        menu.add_command(label="Delete", command=lambda: self._delete_activity(activity))
        menu.tk_popup(event.x_root, event.y_root)

    def _add_activity(self):
        if not self._projects:
            # ActivityPanel's Project dropdown needs at least one option --
            # this only happens if every Project was ever deleted along
            # with its Activities, since a fresh install always seeds a
            # couple and deleting a Project otherwise falls back to
            # "General" rather than leaving zero.
            self.db.get_or_create_general_project()
            self._projects = self.db.list_projects()

        def on_save(result):
            self.db.add_activity(Activity(None, **result))
            self.on_change()
            return True

        self.open_activity_panel(activity=None, on_save=on_save, on_delete=None)

    def _edit_activity(self, activity: Activity):
        self._cancel_pending_arm()
        def on_save(result):
            activity.name = result["name"]
            activity.jira_key = result["jira_key"]
            activity.default_duration_minutes = result["default_duration_minutes"]
            activity.project_id = result["project_id"]
            activity.jira_project = result["jira_project"]
            activity.issue_type = result["issue_type"]
            self.db.update_activity(activity)
            self.on_change()
            return True

        def on_delete():
            self._delete_activity(activity)

        self.open_activity_panel(activity=activity, on_save=on_save, on_delete=on_delete)

    def _delete_activity(self, activity: Activity):
        assert activity.id is not None
        answer = messagebox.askyesnocancel(
            "Delete activity",
            f"Delete “{activity.name}”?\n\n"
            f"Yes = delete activity but keep its existing time blocks\n"
            f"No = also delete all of its time blocks\n"
            f"Cancel = don't delete anything",
        )
        if answer is None:
            return
        delete_entries = not answer  # answer True("Yes")->keep entries, False("No")->delete entries
        self.db.delete_activity(activity.id, delete_entries=delete_entries)
        if self.armed_activity_id == activity.id:
            self.armed_activity_id = None
        self.refresh()
        self.on_change()

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------
    def _toggle_project(self, project: Project):
        assert project.id is not None
        self.db.set_project_collapsed(project.id, not project.collapsed)
        self.on_change()

    def _project_context_menu(self, event, project: Project):
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Collapse" if not project.collapsed else "Expand",
                          command=lambda: self._toggle_project(project))
        menu.add_command(label="Edit…", command=lambda: self._edit_project(project))
        menu.add_command(label="Delete", command=lambda: self._delete_project(project))
        menu.tk_popup(event.x_root, event.y_root)

    def _add_project(self):
        def on_save(result):
            self.db.add_project(Project(None, result["name"], result["color"]))
            self.on_change()
            return True

        self.open_project_panel(project=None, on_save=on_save, on_delete=None)

    def _edit_project(self, project: Project):
        def on_save(result):
            project.name = result["name"]
            project.color = result["color"]
            self.db.update_project(project)
            self.on_change()
            return True

        def on_delete():
            self._delete_project(project)

        self.open_project_panel(project=project, on_save=on_save, on_delete=on_delete)

    def _delete_project(self, project: Project):
        assert project.id is not None
        answer = messagebox.askyesnocancel(
            "Delete project",
            f"Delete the “{project.name}” project?\n\n"
            f"Yes = delete the project but keep its activities (they move to “General”)\n"
            f"No = also delete all activities inside it (their time blocks are kept)\n"
            f"Cancel = don't delete anything",
        )
        if answer is None:
            return
        delete_activities = not answer  # "Yes" -> keep activities, "No" -> delete them
        self.db.delete_project(project.id, delete_activities=delete_activities)
        if self.armed_activity_id is not None and self.db.get_activity(self.armed_activity_id) is None:
            self.armed_activity_id = None
        self.refresh()
        self.on_change()
