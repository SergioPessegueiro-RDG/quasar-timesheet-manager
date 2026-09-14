"""
The "Summary" tab: total hours for a chosen week or month, shown as two
breakdowns side by side -- one by Project, one by QDM (this app's name
for what's elsewhere still called an Activity, matching the
Activities-sidebar-turned-"QDM's" rename) -- using the tab's full width.
This used to be a single list you toggled between Activity/Project
grouping to see; both breakdowns are now always visible together. By
Project is a pie chart, each slice labeled with its own name and
percentage directly on the chart (see _draw_pie) -- Projects are a small,
fairly stable set (everything gets grouped into one), so a handful of
wedges reads faster than a list once there's more than a couple. By QDM
is a bar chart instead (see _build_bar_row): QDM's aren't grouped the
same way, so there can easily be far more of them than Projects, and a
pie's fixed 360 degrees split that many ways stops being readable long
before a plain list of full-width bars does. Both breakdowns keep a
scrolling area below (or, for the bar chart, AS) their rows -- for the
pie, it's a secondary reference (exact hours, or a slice too thin to
label) capped to a small height so the chart itself gets most of the
column.

A third permanent tab alongside "Timesheet" and "Template" (never hidden,
same as Template -- see app/main_window.py's _build_body) rather than one
of the hide-after-use panels in app/panels.py, since -- like the calendar
tabs -- it's something you come back to and re-navigate rather than a
one-shot dialog.

Totals are computed directly from TimeEntry rows' own denormalized
activity_name/color snapshot (the same fields app/export_csv.py reads).
The per-Activity/QDM breakdown groups by activity_id where available so a
renamed activity's entries stay grouped together, falling back to
grouping by name alone for entries whose activity has since been deleted
(activity_id is NULL) -- exactly like the Timesheet calendar and CSV
export already treat a deleted activity's existing entries. The
per-Project breakdown looks up each entry's activity's current
project_id and groups by that instead -- an entry whose activity was
itself deleted has no live activity to look that up from, so it falls
back to its own preserved activity_name/color, the same as the
per-Activity/QDM fallback (there's no way to know which Project it used
to belong to once the activity itself is gone).
"""
import math
import tkinter as tk
from datetime import date, timedelta

from . import config, theme
from .db import Database
from .widgets import CARD_RADIUS, RoundedButton, RoundedCard, ScrollArea


def _last_day_of_month(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


class SummaryPanel(tk.Frame):
    def __init__(self, master, db: Database, family: str, **kwargs):
        kwargs.setdefault("bg", theme.APP_BG)
        kwargs.setdefault("highlightthickness", 0)
        super().__init__(master, **kwargs)
        self.db = db
        self.family = family
        self.mode = "week"  # or "month"
        self.anchor = date.today()

        self._build_widgets()
        self.refresh()

    # ------------------------------------------------------------------
    def _build_widgets(self):
        inner = tk.Frame(self, bg=theme.APP_BG)
        inner.pack(fill="both", expand=True, padx=16, pady=16)

        card = RoundedCard(inner, bg=theme.PANEL_BG, radius=18, pad=18, outline=False)
        card.pack(fill="both", expand=True)
        body = card.body

        nav = tk.Frame(body, bg=theme.PANEL_BG)
        nav.pack(fill="x", pady=(0, 10))

        RoundedButton(nav, text="‹", width=3, style="Nav.TButton", compact=True,
                      command=self._prev).pack(side="left")
        RoundedButton(nav, text="Today", style="Nav.TButton",
                      command=self._today).pack(side="left", padx=6)
        RoundedButton(nav, text="›", width=3, style="Nav.TButton", compact=True,
                      command=self._next).pack(side="left")

        self.period_label = tk.Label(nav, text="", font=(self.family, 12, "bold"),
                                      bg=theme.PANEL_BG, fg=theme.TEXT_PRIMARY)
        self.period_label.pack(side="left", padx=16)

        # A two-button segmented toggle (the active one drawn in the accent
        # style, same convention as the color swatches and theme swatches
        # elsewhere) rather than a Combobox -- there are only ever two
        # choices, so a toggle reads faster than a dropdown.
        toggle_box = tk.Frame(nav, bg=theme.PANEL_BG)
        toggle_box.pack(side="right")
        self.week_btn = RoundedButton(toggle_box, text="Week", command=lambda: self._set_mode("week"))
        self.week_btn.pack(side="left")
        self.month_btn = RoundedButton(toggle_box, text="Month", command=lambda: self._set_mode("month"))
        self.month_btn.pack(side="left", padx=(6, 0))

        # Both breakdowns side by side, using the tab's full width,
        # instead of a Project/QDM toggle that hid one behind the other.
        columns = tk.Frame(body, bg=theme.PANEL_BG)
        columns.pack(fill="both", expand=True, pady=(4, 0))

        self._project = self._build_breakdown(columns, "By Project", chart="pie")
        self._project["frame"].pack(side="left", fill="both", expand=True, padx=(0, 10))

        divider = tk.Frame(columns, bg=theme.BORDER, width=1)
        divider.pack(side="left", fill="y")

        # By QDM is a bar chart, not a pie -- someone can easily have far
        # more distinct QDM's than Projects (QDM's aren't grouped the way
        # Projects group them), and a pie's 360 degrees split that many
        # ways stops being readable long before a plain list of bars does,
        # since every QDM still gets its own full-width row here instead
        # of an ever-thinner wedge.
        self._qdm = self._build_breakdown(columns, "By QDM", chart="bar")
        self._qdm["frame"].pack(side="left", fill="both", expand=True, padx=(10, 0))

    def _build_breakdown(self, parent, heading: str, chart: str) -> dict:
        """Builds one column's heading, chart, scrolling rows, and total
        line, returning the handful of widgets refresh() needs to update
        as a dict -- kept local rather than as a pile of self.project_x/
        self.qdm_x attributes, since the two columns are otherwise built
        the same way and only ever refreshed together. `chart` is "pie"
        (By Project) or "bar" (By QDM) -- see _refresh_breakdown/
        _draw_pie/_build_bar_row for where that actually branches."""
        frame = tk.Frame(parent, bg=theme.PANEL_BG)

        tk.Label(frame, text=heading, font=(self.family, 11, "bold"), bg=theme.PANEL_BG,
                 fg=theme.TEXT_PRIMARY).pack(anchor="w", pady=(0, 8))

        total_label = tk.Label(frame, text="", font=(self.family, 9, "bold"),
                                bg=theme.PANEL_BG, fg=theme.TEXT_SECONDARY)
        total_label.pack(side="bottom", anchor="w", pady=(8, 0))

        # outline=False -- the outer `card` in _build_widgets already
        # draws the one border for the whole nav+columns box; without
        # this, ScrollArea's own (same-colored, so invisible) rounded
        # corners would still draw a second border right underneath it.
        pie_canvas = None
        if chart == "pie":
            # Packed from the bottom up first (pack computes every
            # child's space before drawing any of them, so call order
            # doesn't affect the final top-to-bottom layout): a
            # fixed-height legend, leaving the rest of the column for the
            # pie canvas below. Each slice already carries its own name +
            # percentage (see _draw_pie), so this scrolling legend is a
            # secondary reference -- the exact hours, or a slice too thin
            # to label -- rather than the primary way to read the chart,
            # which is why it gets a modest capped height instead of
            # splitting the column evenly with the chart; that's what
            # lets the pie itself grow to fill most of the column.
            legend_area = ScrollArea(frame, bg=theme.PANEL_BG, outline=False, pad=0, height=150)
            legend_area.pack(side="bottom", fill="x")
            pie_canvas = tk.Canvas(frame, bg=theme.PANEL_BG, highlightthickness=0)
            pie_canvas.pack(fill="both", expand=True, pady=(0, 10))
        else:
            # Bar chart: the scrollable rows below ARE the chart (name,
            # hours/percentage, and its own proportional bar all in one
            # row -- see _build_bar_row), so there's no separate canvas
            # and this area gets the full column height instead of a
            # capped strip underneath one.
            legend_area = ScrollArea(frame, bg=theme.PANEL_BG, outline=False, pad=0)
            legend_area.pack(fill="both", expand=True, pady=(0, 10))

        return {
            "frame": frame,
            "chart": chart,
            "pie_canvas": pie_canvas,
            "legend_area": legend_area,
            "rows_frame": legend_area.content,
            "total_label": total_label,
        }

    # ------------------------------------------------------------------
    # Period navigation
    # ------------------------------------------------------------------
    def _period_range(self):
        if self.mode == "week":
            start = self.anchor - timedelta(days=self.anchor.weekday())
            # config.week_end_offset() is 4 for a plain Mon-Fri week, 6
            # once Settings' "Show weekends" is on -- keeps this tab's
            # weekly totals matching whatever the calendar itself shows,
            # instead of a Mon-Fri window baked in regardless.
            end = start + timedelta(days=config.week_end_offset())
        else:
            start = self.anchor.replace(day=1)
            end = _last_day_of_month(self.anchor.year, self.anchor.month)
        return start, end

    def _period_label_text(self, start: date, end: date) -> str:
        if self.mode == "week":
            return f"{start.strftime('%b %d')} – {end.strftime('%b %d, %Y')}"
        return start.strftime("%B %Y")

    def _prev(self):
        if self.mode == "week":
            self.anchor -= timedelta(days=7)
        else:
            self.anchor = self.anchor.replace(day=1) - timedelta(days=1)
        self.refresh()

    def _next(self):
        if self.mode == "week":
            self.anchor += timedelta(days=7)
        else:
            self.anchor = _last_day_of_month(self.anchor.year, self.anchor.month) + timedelta(days=1)
        self.refresh()

    def _today(self):
        self.anchor = date.today()
        self.refresh()

    def _set_mode(self, mode: str):
        if mode != self.mode:
            self.mode = mode
            self.refresh()

    # ------------------------------------------------------------------
    # Grouping
    # ------------------------------------------------------------------
    def _totals_by_activity(self, entries):
        totals = {}
        order = []
        for e in entries:
            key = e.activity_id if e.activity_id is not None else f"name:{e.activity_name}"
            if key not in totals:
                totals[key] = {"name": e.activity_name, "color": e.color, "minutes": 0}
                order.append(key)
            totals[key]["minutes"] += e.duration_minutes()
        return [totals[k] for k in order]

    def _totals_by_project(self, entries):
        activities_by_id = {a.id: a for a in self.db.list_activities()}
        projects_by_id = {p.id: p for p in self.db.list_projects()}
        totals = {}
        order = []
        for e in entries:
            act = activities_by_id.get(e.activity_id) if e.activity_id is not None else None
            proj = projects_by_id.get(act.project_id) if act is not None else None
            if proj is not None:
                key = ("project", proj.id)
                name, color = proj.name, proj.color
            else:
                # The activity (and therefore its project) no longer
                # exists -- fall back to the entry's own preserved
                # name/color, same as the per-QDM view does for a
                # deleted activity's entries.
                key = ("orphan", e.activity_id if e.activity_id is not None else f"name:{e.activity_name}")
                name, color = e.activity_name, e.color
            if key not in totals:
                totals[key] = {"name": name, "color": color, "minutes": 0}
                order.append(key)
            totals[key]["minutes"] += e.duration_minutes()
        return [totals[k] for k in order]

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def refresh(self):
        start, end = self._period_range()
        self.period_label.config(text=self._period_label_text(start, end))
        self.week_btn.config(style="Accent.TButton" if self.mode == "week" else "Secondary.TButton")
        self.month_btn.config(style="Accent.TButton" if self.mode == "month" else "Secondary.TButton")

        entries = self.db.list_time_entries_between(start.isoformat(), end.isoformat())

        project_rows = self._totals_by_project(entries)
        project_rows.sort(key=lambda r: -r["minutes"])
        qdm_rows = self._totals_by_activity(entries)
        qdm_rows.sort(key=lambda r: -r["minutes"])

        self._refresh_breakdown(self._project, project_rows, "project", "projects")
        self._refresh_breakdown(self._qdm, qdm_rows, "QDM", "QDM's")

    def _refresh_breakdown(self, section: dict, rows: list, singular: str, plural: str):
        grand_total = sum(r["minutes"] for r in rows)

        for child in section["rows_frame"].winfo_children():
            child.destroy()

        if not rows:
            tk.Label(section["rows_frame"], text="No time logged in this period.",
                     font=(self.family, 9), fg=theme.TEXT_MUTED, bg=theme.PANEL_BG,
                     wraplength=160, justify="left").pack(anchor="w", pady=14)
        elif section["chart"] == "bar":
            max_minutes = max(r["minutes"] for r in rows)
            for r in rows:
                self._build_bar_row(section["rows_frame"], r, max_minutes, grand_total)
        else:
            for r in rows:
                self._build_legend_row(section["rows_frame"], r, grand_total)
        section["legend_area"].refresh_scrollregion()

        if section["chart"] == "pie":
            # The pie canvas is a persistent widget (unlike the legend/bar
            # rows above, fully rebuilt every refresh), so its own
            # <Configure> binding needs re-pointing at the current
            # rows/total on every refresh -- plain bind() (no add="+")
            # replaces the previous callback rather than stacking another
            # one alongside it. Also draw it once right now for the size
            # it already has, since a Configure event won't fire again on
            # its own if the canvas hasn't actually resized since the
            # last refresh.
            canvas = section["pie_canvas"]
            canvas.bind("<Configure>", lambda event, c=canvas, rws=rows, gt=grand_total:
                        self._draw_pie(c, rws, gt))
            self._draw_pie(canvas, rows, grand_total)

        total_hours = grand_total / 60
        noun_word = singular if len(rows) == 1 else plural
        section["total_label"].config(text=f"Total: {total_hours:.1f}h across {len(rows)} {noun_word}")

    def _build_legend_row(self, parent, r: dict, grand_total: int):
        row = tk.Frame(parent, bg=theme.PANEL_BG)
        row.pack(fill="x", pady=4)

        swatch = tk.Canvas(row, width=12, height=12, bg=theme.PANEL_BG, highlightthickness=0)
        swatch.pack(side="left", padx=(0, 8))
        theme.place_rounded_rect(swatch, 1, 1, 11, 11, radius=3, fill=r["color"], outline="",
                                 background=theme.PANEL_BG)

        hours = r["minutes"] / 60
        pct = (r["minutes"] / grand_total * 100) if grand_total else 0
        tk.Label(row, text=f"{hours:.1f}h ({pct:.0f}%)", font=(self.family, 9),
                 bg=theme.PANEL_BG, fg=theme.TEXT_SECONDARY, width=13, anchor="e").pack(
            side="right", padx=(6, 0))

        # No fixed width here, same reasoning as the old bar rows this
        # replaces: a name longer than some fixed character count used to
        # render past its own allotted space instead of the row adapting.
        # wraplength (there's no bar to shrink anymore, so wrapping to a
        # second line is the equivalent move here) keeps a long name from
        # pushing this column wider than the other one.
        tk.Label(row, text=r["name"], font=(self.family, 9), bg=theme.PANEL_BG,
                 fg=theme.TEXT_PRIMARY, anchor="w", justify="left", wraplength=130).pack(
            side="left", fill="x", expand=True)

    def _build_bar_row(self, parent, r: dict, max_minutes: int, grand_total: int):
        """One row of the By QDM bar chart: name + hours/percentage on
        top, a proportional bar underneath. grid (not pack) for the two
        labels + the bar below them, so both labels stay DIRECT children
        of `row` in a predictable left-to-right, then-below layout --
        matters for tests that read a row's own text via
        row.winfo_children(), same as _build_legend_row's rows.

        The bar itself is a plain tk.Frame "track" with a colored tk.Frame
        placed inside it at relwidth=(this row's share of the largest
        row's minutes) -- place() recalculates that automatically on
        resize, no <Configure> math needed the way _draw_pie needs for
        the canvas-drawn pie. Every row gets its own full-width bar
        (floored at a thin sliver so even a tiny share stays visible)
        instead of a wedge that shrinks toward unreadable as more QDM's
        share the same 360 degrees -- the reason this column is a bar
        chart instead of a second pie in the first place.
        """
        row = tk.Frame(parent, bg=theme.PANEL_BG)
        row.pack(fill="x", pady=6)
        row.columnconfigure(0, weight=1)

        tk.Label(row, text=r["name"], font=(self.family, 9), bg=theme.PANEL_BG,
                 fg=theme.TEXT_PRIMARY, anchor="w", justify="left", wraplength=200).grid(
            row=0, column=0, sticky="w")

        hours = r["minutes"] / 60
        pct = (r["minutes"] / grand_total * 100) if grand_total else 0
        tk.Label(row, text=f"{hours:.1f}h ({pct:.0f}%)", font=(self.family, 9),
                 bg=theme.PANEL_BG, fg=theme.TEXT_SECONDARY, anchor="e").grid(
            row=0, column=1, sticky="e", padx=(6, 0))

        track = tk.Frame(row, bg=theme.BORDER, height=14)
        track.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        frac = (r["minutes"] / max_minutes) if max_minutes else 0
        tk.Frame(track, bg=r["color"]).place(x=0, y=0, relwidth=max(0.02, frac), relheight=1)

    # Slices this thin (in degrees -- 14 degrees is a bit under 4% of the
    # full circle) can't fit a legible name + percentage without running
    # into their neighbors, so they keep just their color on the ring and
    # stay nameable via the legend below instead.
    _MIN_LABEL_SWEEP_DEGREES = 14.0

    def _draw_pie(self, canvas: tk.Canvas, rows: list, grand_total: int):
        canvas.delete("all")
        w, h = canvas.winfo_width(), canvas.winfo_height()
        if w <= 1 or h <= 1:
            return
        size = max(0, min(w, h) - 8)
        if size <= 0:
            return
        x0 = (w - size) / 2
        y0 = (h - size) / 2
        x1, y1 = x0 + size, y0 + size
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        radius = size / 2
        label_font = (self.family, 11, "bold")
        # Where inside the slice the label sits -- close enough to the
        # center that even a fairly narrow slice's arc is wide enough at
        # that radius to hold two short lines of text, but not so close
        # that every label bunches up on top of each other in the middle.
        label_radius = radius * 0.62
        label_width = max(60, int(radius * 0.9))

        if not rows or grand_total <= 0:
            # Empty state -- a plain muted ring rather than a blank canvas.
            canvas.create_oval(x0, y0, x1, y1, outline=theme.BORDER, width=2)
            return

        # theme.block_text_color picks per-slice, not from the active app
        # theme -- slice colors are whatever Projects were assigned,
        # independent of light/dark theme, so contrast has to be judged
        # per-slice. Shared with calendar_view.py's time-block labels,
        # which have the identical problem.
        label_color = theme.block_text_color

        def draw_label(mid_angle_deg: float, name: str, pct: float, color: str):
            rad = math.radians(mid_angle_deg)
            lx = cx + label_radius * math.cos(rad)
            ly = cy - label_radius * math.sin(rad)
            canvas.create_text(lx, ly, text=f"{name}\n{pct:.0f}%", fill=label_color(color),
                                font=label_font, justify="center", width=label_width)

        # Standard math convention (0=east, 90=north, same as
        # theme.rounded_rect uses) -- 90 is 12 o'clock, the usual pie
        # chart starting point. Each slice sweeps clockwise (negative
        # extent) proportional to its share of the period's total time,
        # largest slice first since `rows` is already sorted that way.
        angle = 90.0
        for r in rows:
            sweep = 360.0 * (r["minutes"] / grand_total)
            if sweep <= 0:
                continue
            if sweep >= 359.99:
                # The one entry in this period is 100% of it --
                # create_arc's pieslice style doesn't draw a clean full
                # circle at extent=360, so draw a plain filled oval
                # instead for this (fairly common, e.g. a period with
                # just one project) case.
                canvas.create_oval(x0, y0, x1, y1, fill=r["color"], outline=theme.PANEL_BG, width=2)
                canvas.create_text(cx, cy, text=f'{r["name"]}\n100%', fill=label_color(r["color"]),
                                    font=label_font, justify="center", width=label_width)
                break
            canvas.create_arc(x0, y0, x1, y1, start=angle, extent=-sweep,
                               fill=r["color"], outline=theme.PANEL_BG, width=2, style="pieslice")
            if sweep >= self._MIN_LABEL_SWEEP_DEGREES:
                pct = r["minutes"] / grand_total * 100
                draw_label(angle - sweep / 2, r["name"], pct, r["color"])
            angle -= sweep
