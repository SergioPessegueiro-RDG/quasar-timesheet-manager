"""
The weekly Mon-Fri calendar grid: a Tkinter Canvas that supports
click-drag creation of time blocks, drag-to-resize, drag-to-move
(including across days), right-click edit/duplicate/delete, and per-day
totals.

This same class also powers the permanent "Template" tab (template_mode=
True) -- a recurring Mon-Fri week of blocks that never expires and can be
copied onto any real week. Both modes share all the drawing/drag/resize/
duplicate machinery below; the only difference is what a "day" and an
"entry" are backed by:
  - normal mode:   a day is a real date; entries are TimeEntry rows keyed
                    by that date, read/written via db.*_time_entry*.
  - template mode: a day is just a weekday (0=Monday..4=Friday, same as
                    the day index used for columns either way); entries
                    are TemplateEntry rows keyed by day_of_week, read/
                    written via db.*_template_entry*.
A handful of small helper methods (_entry_day_idx, _make_entry, _db_*,
_day_options) are the only places that branch on template_mode -- every
other method (drawing, dragging, hit-testing) works the same in both modes.
"""
import tkinter as tk
import tkinter.font as tkfont
from datetime import date, datetime, timedelta
from tkinter import messagebox
from typing import Callable, Dict, List, Optional, Tuple, Union

from . import config, theme
from .calendar_feed import CalendarEvent, format_meeting_details
from .db import Database
from .jira_sync import entry_needs_push
from .models import Activity, TemplateEntry, TimeEntry
from .widgets import CARD_RADIUS, HorizontalVectorScrollbar, RoundedButton, RoundedCard, VectorScrollbar

EntryLike = Union[TimeEntry, TemplateEntry]


def _minutes_total(start_hour: Optional[int] = None, end_hour: Optional[int] = None) -> int:
    """Visible grid length in minutes. Defaults to Settings' work hours;
    CalendarGrid passes an expanded window when a logged block sits
    outside that range so the block stays on screen."""
    start = config.START_HOUR if start_hour is None else start_hour
    end = config.END_HOUR if end_hour is None else end_hour
    return max(0, (end - start) * 60)

# Tk's event.state bitmask for the Control key. This bit is consistent
# across X11/Windows/macOS Tk builds (unlike, say, the Alt/Option bit,
# which varies), so it's safe to check directly rather than binding a
# separate <Control-Button-1> sequence -- Tk only fires the *most specific*
# matching binding for a given click, so a plain <Button-1> binding is what
# actually needs to see the Control state to tell the two cases apart.
CONTROL_STATE_MASK = 0x4


def _minute_to_hhmm(minute_of_day: int, start_hour: Optional[int] = None) -> str:
    start = config.START_HOUR if start_hour is None else start_hour
    total = start * 60 + minute_of_day
    hour, minute = divmod(total, 60)
    hour = min(24, max(0, hour))
    if hour == 24:
        return "24:00"
    return f"{hour:02d}:{minute:02d}"


def _hhmm_to_minute(hhmm: str, start_hour: Optional[int] = None) -> int:
    start = config.START_HOUR if start_hour is None else start_hour
    h, m = (int(x) for x in hhmm.split(":"))
    return h * 60 + m - start * 60


def _now_line_delay_ms(now: Optional[datetime] = None) -> int:
    """Milliseconds until the now-line should next move.

    The indicator is minute-resolution (see _draw_now_line), so the next
    useful redraw is the start of the following minute. A 250ms floor
    avoids a 0ms tight loop if a tick fires a few microseconds before
    the minute rolls over.
    """
    now = now or datetime.now()
    remaining_ms = (60 - now.second) * 1000 - now.microsecond // 1000
    return max(250, remaining_ms)


def display_hours_for_entries(
    entries: List[EntryLike],
    base_start: int,
    base_end: int,
) -> Tuple[int, int]:
    """Widen Settings' work-hour window just enough to show every block.

    `base_end` is exclusive (17 = last slot ends at 5pm). Settings stay
    put; only this week's timesheet/template canvas grows.
    """
    start = max(0, min(23, int(base_start)))
    end = max(start + 1, min(24, int(base_end)))
    for entry in entries:
        try:
            sh, sm = (int(x) for x in str(entry.start_time).split(":"))
            eh, em = (int(x) for x in str(entry.end_time).split(":"))
        except (TypeError, ValueError, AttributeError):
            continue
        start = min(start, max(0, min(23, sh)))
        start_mins = sh * 60 + sm
        end_mins = eh * 60 + em
        if end_mins <= start_mins:
            end = 24
        else:
            exclusive = eh if em == 0 else min(24, eh + 1)
            end = max(end, exclusive)
    if end <= start:
        end = min(24, start + 1)
    return start, end


def format_day_total_hours(minutes: float) -> str:
    """Per-day total under the grid: `0h` when empty, else one decimal."""
    if minutes <= 0:
        return "0h"
    return f"{minutes / 60:.1f}h"


def wrap_block_text(text: str, chars_per_line: int) -> List[str]:
    """Word-wrap a one-line string so calendar blocks can show a long
    QDM name / notes instead of cutting them off with an ellipsis."""
    text = " ".join((text or "").split())
    if not text:
        return []
    width = max(4, int(chars_per_line))
    lines: List[str] = []
    current = ""
    for word in text.split(" "):
        trial = word if not current else f"{current} {word}"
        if len(trial) <= width:
            current = trial
            continue
        if current:
            lines.append(current)
        while len(word) > width:
            lines.append(word[:width])
            word = word[width:]
        current = word
    if current:
        lines.append(current)
    return lines


def _ellipsis(text: str, chars_per_line: int) -> str:
    """Force a trailing … so a clipped wrap-line still reads as 'more'.

    wrap_block_text already fits each chunk to `chars_per_line`, so a
    naive 'trim if too long' would never add the mark — that's why tall
    blocks used to drop leftover notes with no hint.
    """
    text = (text or "").strip().rstrip("…").rstrip()
    width = max(4, int(chars_per_line))
    budget = max(1, width - 1)
    if len(text) > budget:
        text = text[:budget].rstrip()
    return text + "…"


def _pack_block_lines(
    sections: List[Tuple[List[str], bool]],
    remaining: int,
    chars_per_line: int,
    time_line: str = "",
) -> List[Tuple[str, bool]]:
    """Fill `remaining` lines from (wrapped-parts, bold) sections.

    If any later wrap-line is dropped, the last visible line gets an
    ellipsis and the time range is omitted — the block's position already
    shows when it is.
    """
    chosen: List[Tuple[str, bool]] = []
    leftover = False
    for parts, bold in sections:
        for i, part in enumerate(parts):
            if remaining <= 0:
                leftover = True
                break
            if remaining == 1 and i < len(parts) - 1:
                chosen.append((_ellipsis(part, chars_per_line), bold))
                remaining = 0
                leftover = True
                break
            chosen.append((part, bold))
            remaining -= 1
        if leftover:
            break
    if leftover:
        if chosen and not chosen[-1][0].endswith("…"):
            text, bold = chosen[-1]
            chosen[-1] = (_ellipsis(text, chars_per_line), bold)
    elif remaining > 0 and time_line:
        chosen.append((time_line, False))
    return chosen


# Stacked "Not synced" under the title needs about this much height;
# 15-minute slots (often ~22–34px) fall short, so the caption shares
# the title line instead of drawing on top of it.
_UNSENT_STACK_MIN_HEIGHT = 32
_UNSENT_STACK_RESERVE = 16


def _unsent_label_reserve(block_height: float) -> float:
    """Space to keep at the bottom of a block for a stacked Not synced caption.

    Short slots skip the reserve so the title still fits; the caption
    then sits on the title line, right-aligned.
    """
    if block_height >= _UNSENT_STACK_MIN_HEIGHT:
        return _UNSENT_STACK_RESERVE
    return 0


class CalendarGrid(tk.Frame):
    # Cap on how many undo/redo steps are kept per grid instance -- enough
    # for any real editing session without the two stacks (each entry is a
    # small dict of plain strings/numbers) growing unbounded over a long
    # one.
    UNDO_LIMIT = 50

    def __init__(self, master, db: Database, get_armed_activity: Callable[[], Optional[Activity]],
                 clear_armed_activity: Callable[[], None],
                 open_time_block: Optional[Callable[..., None]] = None,
                 open_duplicate: Optional[Callable[..., None]] = None,
                 initial_week_start: Optional[date] = None,
                 template_mode: bool = False,
                 on_week_change: Optional[Callable[[date], None]] = None,
                 get_overlay_events: Optional[Callable[[date], List[CalendarEvent]]] = None,
                 **kwargs):
        kwargs.setdefault("bg", theme.APP_BG)
        kwargs.setdefault("highlightthickness", 0)
        super().__init__(master, **kwargs)
        self.db = db
        self.get_armed_activity = get_armed_activity
        self.clear_armed_activity = clear_armed_activity
        self.open_time_block = open_time_block
        self.open_duplicate = open_duplicate
        self.template_mode = template_mode
        self.on_week_change = on_week_change
        self.get_overlay_events = get_overlay_events
        self.family = theme.resolve_font_family()

        if initial_week_start is not None:
            self.week_start = initial_week_start - timedelta(days=initial_week_start.weekday())
        else:
            today = date.today()
            self.week_start = today - timedelta(days=today.weekday())  # Monday

        # Dynamic grid dimensions -- recomputed on every canvas resize so
        # the grid stretches to fill its window. These starting values are
        # also the lower bound (MIN_*) the grid won't shrink past.
        self.gutter_width = config.GUTTER_WIDTH_PX
        self.header_height = config.HEADER_HEIGHT_PX
        self.day_width = config.DAY_WIDTH_PX
        self.slot_height = config.SLOT_HEIGHT_PX
        self.px_per_min = self.slot_height / config.SLOT_MINUTES
        # Visible hour window. Starts as Settings' work hours, then
        # widens whenever a logged block sits outside that range.
        self._view_start_hour = config.START_HOUR
        self._view_end_hour = config.END_HOUR
        # Manual zoom (the -/100%/+ controls in the nav row below): a
        # multiplier applied on top of the normal shrink-to-fit day
        # width/slot height, re-clamped to the same MIN/MAX bounds
        # afterwards -- so it can't make blocks unreadably small or
        # absurdly large, just lets someone dial in a size they prefer
        # within that range instead of always taking whatever the window's
        # current size happens to compute. Not persisted -- resets to
        # 100% next launch, same as any other in-session view preference.
        self._zoom_mult = 1.0
        self._zoom_min = 0.7
        self._zoom_max = 1.3
        self._zoom_step = 0.15
        # The scroll host's own last <Configure> size -- _zoom_in/
        # _zoom_out reuse this to recompute the grid without waiting for
        # an actual resize event.
        self._last_viewport_size = (0, 0)
        # While the QDM sash is dragged, skip the full grid wipe-and-redraw
        # that normally runs on every viewport <Configure>. One relayout
        # on mouse-up is enough; doing it per pixel is what made the
        # calendar flash black.
        self._relayout_deferred = False
        # Live window-resize coalescing -- a full refresh() per Configure
        # pixel feels like the grid is stuttering. Apply the first layout
        # immediately, then fold further events into one redraw.
        self._resize_job: Optional[str] = None
        self._pending_viewport: Optional[Tuple[int, int]] = None
        self._grid_painted = False
        self._entry_paint_job: Optional[str] = None
        self._now_line_job: Optional[str] = None
        self._hover_slot: Optional[Tuple[int, int]] = None
        self._qdm_drop_binds: dict = {}
        self._qdm_ghost = None

        # Which block (by id), if any, is currently keyboard-selected -- set
        # by clicking a block without dragging it, or by dragging one to a
        # new spot (see _finish_entry_drag). Saving a brand-new Time Block
        # does not select it. Drives the highlighted outline
        # in _draw_entry, the Delete-key shortcut, and arrow-key nudging
        # (see _on_left_key/_on_right_key/_on_up_key/_on_down_key in
        # main_window.py). Cleared whenever the selected block no longer
        # exists (see refresh()) or on Escape (see _cancel_drag).
        self.selected_entry_id: Optional[int] = None
        self._overlay_drawn: List[CalendarEvent] = []

        # Undo/redo history for this grid only -- the Timesheet and
        # Template tabs each have their own CalendarGrid instance and their
        # own independent history, since they operate on entirely different
        # rows (TimeEntry vs TemplateEntry). See _push_undo/undo/redo/
        # _apply_command below for how a single command dict (add/remove/
        # update) can represent every kind of edit this grid makes --
        # create, move, resize, delete, and duplicate all reduce to one of
        # those three.
        self._undo_stack: List[dict] = []
        self._redo_stack: List[dict] = []

        self._build_widgets()
        self._drag_state = None
        # Fitted column width comes from _scroll_host <Configure>. Drawing
        # here at DAY_WIDTH_PX first, then again at the real size, is the
        # visible "calendar adjusting" jump on launch.
        self._emit_week_change()
        if not self.template_mode:
            self.bind("<Destroy>", self._on_grid_destroy, add="+")
            self._schedule_now_line()

    def _view_minutes_total(self) -> int:
        return _minutes_total(self._view_start_hour, self._view_end_hour)

    def _view_minute_to_hhmm(self, minute_of_day: int) -> str:
        return _minute_to_hhmm(minute_of_day, self._view_start_hour)

    def _view_hhmm_to_minute(self, hhmm: str) -> int:
        return _hhmm_to_minute(hhmm, self._view_start_hour)

    def _update_view_hours(self, entries: List[EntryLike],
                            extra: Optional[List[CalendarEvent]] = None) -> bool:
        combined: List[object] = list(entries)
        if extra:
            combined.extend(extra)
        start, end = display_hours_for_entries(
            combined, config.START_HOUR, config.END_HOUR)
        if (start, end) == (self._view_start_hour, self._view_end_hour):
            return False
        self._view_start_hour = start
        self._view_end_hour = end
        return True

    def _relayout_for_view_hours(self):
        """Rebuild slot count after the visible hour window changes."""
        self._grid_painted = False
        try:
            w, h = self._last_viewport_size
        except (TypeError, ValueError):
            w, h = 0, 0
        if w > 1 and h > 1:
            self._last_viewport_size = (0, 0)
            self._recompute_grid_dimensions(w, h)
            return
        self.refresh()

    # ------------------------------------------------------------------
    # Layout / widgets
    # ------------------------------------------------------------------
    def _build_widgets(self):
        card = RoundedCard(self, bg=theme.PANEL_BG, radius=CARD_RADIUS, outline=False)
        card.pack(fill="both", expand=True, padx=(8, 12), pady=(8, 12))
        inner = card.body
        body = inner

        nav = tk.Frame(body, bg=theme.PANEL_BG)
        nav.pack(fill="x", pady=(0, 10))
        self._nav_row = nav
        # Both toggled off (in that order) by _reflow_nav_row below,
        # before either one can start overlapping the always-needed prev/
        # Today/next + zoom controls on a narrow window -- see that
        # method's docstring.
        self.apply_template_btn: Optional[RoundedButton] = None
        self._apply_template_visible = True
        self._hint_visible = True

        if not self.template_mode:
            # Compact circular ‹ › — idle fill matches the card so they
            # read as glyphs, not as a square plate around a round button.
            RoundedButton(nav, text="‹", width=3, style="Nav.TButton", compact=True,
                          command=self._prev_week).pack(side="left")
            RoundedButton(nav, text="Today", style="Ghost.TButton",
                          command=self._go_today).pack(side="left", padx=6)
            RoundedButton(nav, text="›", width=3, style="Nav.TButton", compact=True,
                          command=self._next_week).pack(side="left")

        self.week_label = tk.Label(nav, text="", font=(self.family, 12, "bold"),
                                    bg=theme.PANEL_BG, fg=theme.TEXT_PRIMARY)
        self.week_label.pack(side="left", padx=16)

        self.hint_label = tk.Label(nav, text="", font=(self.family, 9), bd=0,
                                    bg=theme.PANEL_BG, fg=theme.TEXT_SECONDARY)

        # Zoom on the far right; Apply Template sits just left of it so
        # the week title can grow without clipping the chip.
        zoom_row = tk.Frame(nav, bg=theme.PANEL_BG)
        zoom_row.pack(side="right")
        self._zoom_row = zoom_row
        RoundedButton(zoom_row, text="−", width=3, style="Nav.TButton", compact=True,
                      command=self._zoom_out).pack(side="left")
        self.zoom_label = tk.Label(
            zoom_row, text="100%", font=(self.family, 9, "bold"),
            bg=theme.PANEL_BG, fg=theme.TEXT_SECONDARY, width=5, anchor="center")
        self.zoom_label.pack(side="left")
        RoundedButton(zoom_row, text="+", width=3, style="Nav.TButton", compact=True,
                      command=self._zoom_in).pack(side="left")

        if not self.template_mode:
            # Copies every block from the Template tab onto whatever week
            # this grid currently has open -- the fast way to fill in a
            # recurring week instead of re-creating the same meetings by
            # hand every time.
            self.apply_template_btn = RoundedButton(
                nav, text="Apply Template", style="Ghost.TButton",
                command=self._apply_template)
            self._pack_apply_template()

        # Below some width, Apply Template (and then the
        # hint text) would otherwise start overlapping the zoom controls
        # instead of everything just quietly not fitting -- pack() alone
        # doesn't shrink or wrap widgets that no longer fit their row, it
        # lets them collide. See _reflow_nav_row.
        nav.bind("<Configure>", self._reflow_nav_row)
        nav.after_idle(self._reflow_nav_row)

        canvas_width = self.gutter_width + len(config.DAY_NAMES) * self.day_width
        canvas_height = self.header_height + self._view_minutes_total() * self.px_per_min

        # width/height here are just the initial preferred size (used to
        # size the window on first launch). The real sizing happens in
        # _on_canvas_resize() below, bound to _scroll_host's <Configure>
        # (the actual available viewport) rather than self.canvas's own --
        # see the long comment on _scroll_host just below for why.
        # highlightthickness=0 so Tk doesn't draw a square focus ring
        # around the grid; the rounded card around this whole pane is the
        # only outer shape.
        canvas_holder = tk.Frame(body, bg=theme.PANEL_BG)
        canvas_holder.pack(fill="both", expand=True)
        canvas_holder.grid_rowconfigure(0, weight=1)
        canvas_holder.grid_columnconfigure(0, weight=1)

        # Below its MIN_DAY_WIDTH_PX/MIN_SLOT_HEIGHT_PX floor, the grid
        # used to have no way to reach content that no longer fit its
        # window -- it just silently clipped at the canvas edge. Fixed by
        # NOT scrolling self.canvas itself. Click/drag/hover go through
        # `_grid_xy` (host canvasx/canvasy) because a clipped nested
        # canvas reports viewport-relative event.x/y once `_scroll_host`
        # is panned — which is how slots above the now-line became
        # untargetable. self.canvas is a single fixed-size window inside
        # `_scroll_host`, which does the actual scrolling.
        self._scroll_host = tk.Canvas(canvas_holder, bg=theme.GRID_BG, highlightthickness=0)
        self._scroll_host.grid(row=0, column=0, sticky="nsew")

        self._vscroll = VectorScrollbar(canvas_holder, command=self._scroll_host.yview, bg=theme.GRID_BG)
        self._hscroll = HorizontalVectorScrollbar(canvas_holder, command=self._scroll_host.xview,
                                                    bg=theme.GRID_BG)
        # Not gridded here -- _update_scrollbars (called from
        # _on_canvas_resize) shows/hides each one depending on whether the
        # grid's current content actually overflows the viewport, so a
        # roomy window shows no scrollbar chrome at all.
        # Totals live in column 0 with the grid (not the full card width)
        # so they share the scrollbar's leftover gap instead of sitting
        # a few pixels right of each day.

        self.canvas = tk.Canvas(
            self._scroll_host, width=canvas_width, height=canvas_height + config.CANVAS_BOTTOM_PAD_PX,
            bg=theme.GRID_BG, highlightthickness=0,
        )
        self._canvas_window = self._scroll_host.create_window((0, 0), window=self.canvas, anchor="nw")
        # xscrollcommand goes through _on_grid_xscroll (not straight to
        # self._hscroll.set) so every horizontal scroll -- wheel, drag,
        # arrow-click, keyboard -- also keeps the totals row's own
        # scrolling strip (_totals_host, built below) in lockstep. See
        # its own comment for why the totals row needs this at all.
        self._scroll_host.configure(yscrollcommand=self._vscroll.set, xscrollcommand=self._on_grid_xscroll)
        self._scroll_host.bind("<Configure>", self._on_canvas_resize)

        # Mouse-wheel scrolling, as a convenience alongside the always-
        # functional scrollbars above (see VectorScrollbar's own docstring
        # on why this app never trusts wheel delivery alone). Plain wheel
        # scrolls vertically; Shift+wheel scrolls horizontally, the
        # standard convention this app's target platforms already use for
        # a horizontally-scrolling view.
        #
        # Tk 9 (this app's current macOS/Homebrew Python) stopped turning
        # two-finger trackpad swipes into <MouseWheel> at all -- they
        # arrive as <TouchpadScroll> instead (TIP 684). The sidebar's
        # ScrollArea already handles that; without the same bind here the
        # grid's wheel bindings never fire and the calendar feels stuck.
        wheel_targets = (self.canvas, self._scroll_host, self._vscroll, self._hscroll)
        for widget in wheel_targets:
            widget.bind("<MouseWheel>", self._on_mousewheel)
            widget.bind("<Shift-MouseWheel>", self._on_shift_mousewheel)
            widget.bind("<Button-4>", self._on_mousewheel)
            widget.bind("<Button-5>", self._on_mousewheel)
        self._bind_calendar_touchpad(wheel_targets)

        # Same column as _scroll_host (not packed under the scrollbar),
        # same coordinate space as the grid (gutter + i * day_width),
        # scrolled in lockstep via _on_grid_xscroll.
        self._totals_host = tk.Canvas(canvas_holder, bg=theme.PANEL_BG, highlightthickness=0,
                                       height=config.TOTALS_ROW_HEIGHT_PX)
        self._totals_host.grid(row=2, column=0, sticky="ew", pady=(10, 0))

        self.canvas.bind("<Button-1>", self._on_button1)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.canvas.bind("<B1-Motion>", self._on_motion_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_hover)
        self.canvas.bind("<Leave>", lambda e: self._clear_hover_preview())
        self.canvas.bind("<Button-3>", self._on_right_click)
        self.canvas.bind("<Escape>", lambda e: self._cancel_drag())
        # Deliberately NOT self.canvas.bind("<Configure>", ...) here -- see
        # the long comment where _scroll_host is built above for why only
        # ITS <Configure> (the real, stable available viewport) should
        # ever drive a recompute. self.canvas's own size is the *output*
        # of that math (_recompute_grid_dimensions calls
        # self.canvas.config(width=..., height=...) directly), so a
        # <Configure> binding here used to fire right back into
        # _on_canvas_resize with self.canvas's own just-set (and already
        # zoomed) size masquerading as the viewport -- corrupting
        # self._last_viewport_size with shrunk content instead of the
        # real window size. That compounded on every zoom press (85% did
        # far more than an actual 15% reduction, and going back to 100%
        # landed smaller than the original, since "100%" was now being
        # computed against a polluted, already-shrunken "viewport") and
        # could also throw off _update_scrollbars' overflow check enough
        # to hide the vertical scrollbar while real content still ran off
        # the bottom. Removed; self.canvas is resized only as a
        # consequence of _scroll_host's own <Configure>/zoom now.

        # Keyboard shortcuts, bound directly on the canvas (rather than
        # app-wide) so they only ever fire while the calendar itself has
        # focus -- which _on_button1 grabs on every click, and which
        # MainWindow also grabs whenever this tab becomes the visible one
        # (see MainWindow._on_tab_changed). Binding here instead of
        # globally means these never fight with a Notebook tab strip's own
        # Left/Right-arrow tab-switching, or with normal text editing in an
        # Entry/Combobox/Text field elsewhere in the app.
        #
        # Left/Right: move to the previous/next week (Timesheet tab only --
        # the Template tab isn't tied to any real week) when nothing is
        # selected, or shift the *selected* block a day earlier/later when
        # one is. Up/Down: shift the selected block's time earlier/later by
        # one slot. Delete/Backspace: remove the selected block (same
        # confirmation dialog as the right-click menu's Delete). All of
        # this composes with undo/redo (Ctrl/Cmd+Z, bound app-wide in
        # MainWindow since it doesn't have the same conflict potential).
        # (The "no selection -> change week" fallback inside these two
        # handlers is itself skipped in Template mode, since there's no
        # week to change there -- but nudging a *selected* block between
        # weekday columns still works in both modes.)
        self.canvas.bind("<Left>", self._on_left_key)
        self.canvas.bind("<Right>", self._on_right_key)
        self.canvas.bind("<Up>", self._on_up_key)
        self.canvas.bind("<Down>", self._on_down_key)
        self.canvas.bind("<Delete>", self._delete_selected_entry)
        self.canvas.bind("<BackSpace>", self._delete_selected_entry)

    def _reflow_nav_row(self, event=None):
        """Bound to the nav row's own <Configure> (plus one after_idle
        call so a tab opened directly at a narrow window starts already
        collapsed instead of waiting for the next resize -- same pattern
        as SettingsPanel's _reflow_settings_columns).

        Plain pack() doesn't shrink or wrap a widget that no longer fits
        its row -- it just lets it collide with whatever else is packed
        there, which is exactly what Apply Template (a Ghost chip) started
        doing to the zoom controls on a narrower window once those existed.
        Rather than let that overlap happen,
        this hides -- in order, least useful first -- the hint text, then
        Apply Template, the moment there genuinely isn't room for them
        alongside the prev/Today/next buttons, the week range, and the
        zoom controls, all of which always stay visible."""
        nav = self._nav_row
        available = nav.winfo_width()
        if available <= 1:
            return

        always_on = [w for w in nav.winfo_children()
                     if w is not self.apply_template_btn
                     and w is not self.hint_label
                     and w.winfo_ismapped()]
        core_w = sum(w.winfo_reqwidth() for w in always_on)

        if self.apply_template_btn is not None:
            fits_apply = available >= core_w + self.apply_template_btn.winfo_reqwidth() + 8
            if fits_apply != self._apply_template_visible:
                self._apply_template_visible = fits_apply
                if fits_apply:
                    self._pack_apply_template()
                else:
                    self.apply_template_btn.pack_forget()

        shown_w = core_w
        if self.apply_template_btn is not None and self._apply_template_visible:
            shown_w += self.apply_template_btn.winfo_reqwidth()
        has_hint = bool(self.hint_label.cget("text"))
        fits_hint = has_hint and available >= shown_w + self.hint_label.winfo_reqwidth() + 8
        if fits_hint != self._hint_visible:
            self._hint_visible = fits_hint
            if fits_hint:
                self._pack_hint()
            else:
                self.hint_label.pack_forget()

    def set_relayout_deferred(self, deferred: bool):
        """Freeze (or thaw) shrink-to-fit redraws. Used by the sidebar
        sash so dragging it doesn't wipe the grid on every mouse pixel."""
        if deferred:
            self._relayout_deferred = True
            if self._resize_job is not None:
                try:
                    self.after_cancel(self._resize_job)
                except tk.TclError:
                    pass
                self._resize_job = None
            return
        if not self._relayout_deferred:
            return
        self._relayout_deferred = False
        try:
            w = self._scroll_host.winfo_width()
            h = self._scroll_host.winfo_height()
        except (tk.TclError, AttributeError):
            return
        if w <= 1 or h <= 1:
            return
        self._pending_viewport = (w, h)
        self._apply_pending_resize()

    def _on_canvas_resize(self, event):
        """Bound to _scroll_host's <Configure> (the real available
        viewport -- see the long comment where _scroll_host is built).
        Recompute day-column width/slot height to fit it, then hand off
        to _recompute_grid_dimensions for the rest."""
        if event.width <= 1 or event.height <= 1:
            return
        if self._relayout_deferred:
            self._pending_viewport = (event.width, event.height)
            return
        size = (event.width, event.height)
        if size == self._last_viewport_size:
            return
        # A 1px Cocoa nudge (see MainWindow._nudge_to_force_repaint) must
        # not wipe-and-redraw the grid.
        if self._last_viewport_size != (0, 0):
            lw, lh = self._last_viewport_size
            if abs(event.width - lw) <= 2 and abs(event.height - lh) <= 2:
                return
        self._pending_viewport = size
        # First layout has to paint immediately so the grid isn't empty
        # for a frame. After that, coalesce Configure events so dragging
        # the window doesn't wipe-and-redraw the canvas on every pixel.
        if self._last_viewport_size == (0, 0):
            self._apply_pending_resize()
            return
        if self._resize_job is not None:
            try:
                self.after_cancel(self._resize_job)
            except tk.TclError:
                pass
        self._resize_job = self.after(32, self._apply_pending_resize)

    def _apply_pending_resize(self):
        self._resize_job = None
        size = self._pending_viewport
        if not size or size == self._last_viewport_size:
            return
        self._last_viewport_size = size
        self._recompute_grid_dimensions(size[0], size[1])

    def _recompute_grid_dimensions(self, viewport_w, viewport_h):
        """The actual grid-sizing math: shrink-to-fit `viewport_w` x
        `viewport_h` within MIN/MAX bounds to get the unzoomed ("100%")
        size, then scale that by the manual zoom multiplier (see
        config.zoom_clamp's own docstring -- it went through two wrong
        versions before this), then size self.canvas to whatever that
        content turns out to be (which may be smaller, equal to, or
        larger than the viewport -- larger is exactly the case
        _update_scrollbars below reveals a scrollbar for, instead of the
        old silent clipping).

        `viewport_w`/`viewport_h` MUST be the real, stable size of
        _scroll_host (the actual available viewport) -- never
        self.canvas's own size, which this method itself sets a few
        lines down. Feeding that back in here as if it were a fresh
        viewport is exactly the bug fixed by removing self.canvas's own
        <Configure> binding above: every zoom press would silently use
        the previous press's *output* as the next press's *input*,
        compounding well past the requested step and never landing back
        on the original size once you zoomed back to 100%.

        Split out from _on_canvas_resize so _zoom_in/_zoom_out can call
        this directly, reusing the last known viewport size, without
        needing an actual resize event to have just fired."""
        self._update_view_hours(self._db_list_entries(), extra=self._overlay_events())
        num_days = len(config.DAY_NAMES)
        num_slots = self._view_minutes_total() / config.SLOT_MINUTES

        available_h = max(0, viewport_h - self.header_height - config.CANVAS_BOTTOM_PAD_PX)
        raw_slot_height = available_h / num_slots if num_slots else config.SLOT_HEIGHT_PX
        new_slot_height = config.zoom_clamp(raw_slot_height, config.MIN_SLOT_HEIGHT_PX,
                                             config.MAX_SLOT_HEIGHT_PX, self._zoom_mult)
        content_h = self.header_height + num_slots * new_slot_height + config.CANVAS_BOTTOM_PAD_PX

        # The vertical scrollbar isn't mapped on the first layout, so the
        # host is ~12px too wide. Fitting to that width and then showing
        # the bar is the visible "calendar jumps left" on launch.
        available_w = max(0, viewport_w - self.gutter_width - self._vscroll_reserve_px(
            viewport_h, content_h))
        raw_day_width = available_w / num_days if num_days else config.DAY_WIDTH_PX
        new_day_width = config.zoom_clamp(raw_day_width, config.MIN_DAY_WIDTH_PX,
                                           config.MAX_DAY_WIDTH_PX, self._zoom_mult)

        content_w = self.gutter_width + num_days * new_day_width
        try:
            current_h = float(self.canvas.cget("height") or 0)
            current_w = float(self.canvas.cget("width") or 0)
        except (tk.TclError, TypeError, ValueError):
            current_h, current_w = 0.0, 0.0
        if (
            self._grid_painted
            and abs(new_day_width - self.day_width) < 0.51
            and abs(new_slot_height - self.slot_height) < 0.51
            and abs(content_h - current_h) < 0.51
            and abs(content_w - current_w) < 0.51
        ):
            self._update_scrollbars(viewport_w, viewport_h, content_w, content_h)
            return

        self.day_width = new_day_width
        self.slot_height = new_slot_height
        self.px_per_min = self.slot_height / config.SLOT_MINUTES

        self.canvas.config(width=content_w, height=content_h)
        self._scroll_host.itemconfigure(self._canvas_window, width=content_w, height=content_h)
        self._scroll_host.config(scrollregion=(0, 0, content_w, content_h))
        self._update_scrollbars(viewport_w, viewport_h, content_w, content_h)

        # Totals row: same content_w and x origin as the grid, scrolled
        # horizontally in lockstep (see _on_grid_xscroll).
        self._totals_host.config(scrollregion=(0, 0, content_w, config.TOTALS_ROW_HEIGHT_PX))
        self._totals_host.xview_moveto(self._scroll_host.xview()[0])

        # Grid chrome first so the window can take clicks; AA time-block
        # images are the expensive part and run on the next tick.
        self.refresh(paint_entries=False)
        self._grid_painted = True
        self._schedule_entry_paint()

    def _vscroll_reserve_px(self, viewport_h, content_h) -> int:
        """Pixels to hold for a not-yet-mapped vertical scrollbar."""
        if content_h <= viewport_h + 0.5:
            return 0
        try:
            if self._vscroll.winfo_ismapped():
                return 0
        except (tk.TclError, AttributeError):
            pass
        return VectorScrollbar.WIDTH

    def _on_grid_xscroll(self, first, last):
        """xscrollcommand for _scroll_host (see where it's configured,
        above) -- forwards to the real horizontal scrollbar exactly like
        VectorScrollbar.set normally would on its own, and additionally
        keeps the totals row's own scrolling strip (_totals_host) at the
        same horizontal scroll position, so per-day totals never drift
        out of alignment with the day columns above them."""
        self._hscroll.set(first, last)
        self._totals_host.xview_moveto(float(first))

    def _update_scrollbars(self, viewport_w, viewport_h, content_w, content_h):
        """Show each scrollbar only while the grid's current content
        actually overflows the viewport on that axis -- a window roomy
        enough to fit everything shows no scrollbar chrome at all, same
        as before this existed."""
        if content_h > viewport_h + 0.5:
            self._vscroll.grid(row=0, column=1, sticky="ns")
        else:
            self._vscroll.grid_remove()
            self._scroll_host.yview_moveto(0)
        if content_w > viewport_w + 0.5:
            self._hscroll.grid(row=1, column=0, sticky="ew")
        else:
            self._hscroll.grid_remove()
            self._scroll_host.xview_moveto(0)

    # ------------------------------------------------------------------
    # Manual zoom (nav row -/100%/+) and mouse-wheel scrolling
    # ------------------------------------------------------------------
    def _zoom_in(self):
        self._set_zoom(self._zoom_mult + self._zoom_step)

    def _zoom_out(self):
        self._set_zoom(self._zoom_mult - self._zoom_step)

    def _set_zoom(self, mult):
        mult = max(self._zoom_min, min(self._zoom_max, mult))
        if mult == self._zoom_mult:
            return
        self._zoom_mult = mult
        self.zoom_label.config(text=f"{round(self._zoom_mult * 100)}%")
        viewport_w, viewport_h = self._last_viewport_size
        if viewport_w > 1 and viewport_h > 1:
            self._recompute_grid_dimensions(viewport_w, viewport_h)

    def _on_mousewheel(self, event):
        # Button-4/Button-5 (X11) always mean one notch up/down (event.num
        # is 4 or 5, event.delta meaningless); a real <MouseWheel> event
        # (Windows/macOS) leaves event.num at its unbound default instead
        # and carries the direction in event.delta -- checking num first
        # covers both without needing two separate handler methods.
        if event.num == 4:
            direction = -1
        elif event.num == 5:
            direction = 1
        else:
            direction = -1 if event.delta > 0 else 1
        self._scroll_host.yview_scroll(direction, "units")

    def _on_shift_mousewheel(self, event):
        if event.num == 4:
            direction = -1
        elif event.num == 5:
            direction = 1
        else:
            direction = -1 if event.delta > 0 else 1
        self._scroll_host.xview_scroll(direction, "units")

    def _bind_calendar_touchpad(self, widgets):
        """Tk 9+ <TouchpadScroll> (TIP 684). Same dual-path as ScrollArea:
        bind_all + hit-test, plus a direct bind on the grid widgets in
        case winfo_containing is wrong on Retina. Older Tk that doesn't
        know the event name is a no-op."""
        try:
            self.bind_all("<TouchpadScroll>", self._on_touchpad_anywhere, add="+")
        except tk.TclError:
            return
        for widget in widgets:
            try:
                widget.bind("<TouchpadScroll>", self._on_touchpad_direct, add="+")
            except tk.TclError:
                return

    def _is_over_calendar(self, widget) -> bool:
        w = widget
        while w is not None:
            if w in (self.canvas, self._scroll_host, self._vscroll, self._hscroll):
                return True
            w = getattr(w, "master", None)
        return False

    def _on_touchpad_anywhere(self, event):
        try:
            hovered = self.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):
            return
        if not self._is_over_calendar(hovered):
            return
        self._scroll_touchpad(event)

    def _on_touchpad_direct(self, event):
        self._scroll_touchpad(event)

    def _scroll_touchpad(self, event):
        """Pixel-precise two-finger scroll of the grid (Tk 9 TouchpadScroll)."""
        try:
            dx_str, dy_str = self.tk.splitlist(
                self.tk.call("tk::PreciseScrollDeltas", event.delta)
            )
            dx, dy = int(dx_str), int(dy_str)
        except (tk.TclError, ValueError, AttributeError, TypeError):
            return
        if dy:
            self._nudge_scroll_pixels("y", dy)
        if dx:
            self._nudge_scroll_pixels("x", dx)

    def _nudge_scroll_pixels(self, axis: str, delta: int):
        # Sign matches ScrollArea: a negative delta moves the view down/right.
        # macOS natural scrolling already inverted the hardware signal.
        try:
            region = str(self._scroll_host.cget("scrollregion")).split()
            x0, y0, x1, y1 = (float(v) for v in region)
        except (tk.TclError, ValueError):
            return
        if axis == "y":
            content = y1 - y0
            if content <= 0:
                return
            try:
                current = self._scroll_host.yview()[0] * content
            except tk.TclError:
                return
            new = max(0.0, min(current - delta, content))
            self._scroll_host.yview_moveto(new / content)
            return
        content = x1 - x0
        if content <= 0:
            return
        try:
            current = self._scroll_host.xview()[0] * content
        except tk.TclError:
            return
        new = max(0.0, min(current - delta, content))
        self._scroll_host.xview_moveto(new / content)

    # ------------------------------------------------------------------
    # Week navigation (normal mode only)
    # ------------------------------------------------------------------
    def _prev_week(self):
        self.week_start -= timedelta(days=7)
        self.refresh()
        self._emit_week_change()

    def _next_week(self):
        self.week_start += timedelta(days=7)
        self.refresh()
        self._emit_week_change()

    def _go_today(self):
        today = date.today()
        self.week_start = today - timedelta(days=today.weekday())
        self.refresh()
        self._emit_week_change()

    def _emit_week_change(self):
        if self.template_mode or self.on_week_change is None:
            return
        self.on_week_change(self.week_start)

    def day_date(self, day_idx: int) -> date:
        return self.week_start + timedelta(days=day_idx)

    # ------------------------------------------------------------------
    # Arrow-key shortcuts (bound on the canvas in _build_widgets)
    #
    # With nothing selected, Left/Right move a week at a time -- the
    # keyboard equivalent of the ‹/› nav buttons (Timesheet only; Template
    # isn't tied to a real week). With a block selected, Left/Right instead
    # nudge *that block* to the previous/next day, and Up/Down nudge its
    # time a slot earlier/later -- keyboard-driven rescheduling, each step
    # undoable like any other edit.
    # ------------------------------------------------------------------
    def _on_left_key(self, event=None):
        if self.selected_entry_id is not None:
            self._nudge_selected_entry(day_delta=-1)
        elif not self.template_mode:
            self._prev_week()
        return "break"

    def _on_right_key(self, event=None):
        if self.selected_entry_id is not None:
            self._nudge_selected_entry(day_delta=1)
        elif not self.template_mode:
            self._next_week()
        return "break"

    def _on_up_key(self, event=None):
        if self.selected_entry_id is not None:
            self._nudge_selected_entry(minute_delta=-config.SLOT_MINUTES)
        return "break"

    def _on_down_key(self, event=None):
        if self.selected_entry_id is not None:
            self._nudge_selected_entry(minute_delta=config.SLOT_MINUTES)
        return "break"

    def _nudge_selected_entry(self, day_delta: int = 0, minute_delta: int = 0):
        entry = self.entries_by_id.get(self.selected_entry_id)
        if entry is None:
            return
        day_idx = self._entry_day_idx(entry)
        start = self._view_hhmm_to_minute(entry.start_time)
        end = self._view_hhmm_to_minute(entry.end_time)
        duration = end - start

        new_day_idx = max(0, min(len(config.DAY_NAMES) - 1, day_idx + day_delta))
        new_start = max(0, min(self._view_minutes_total() - duration, start + minute_delta))
        if new_day_idx == day_idx and new_start == start:
            return  # already at an edge (day 0/4, or the top/bottom of the grid)

        before = self._snapshot(entry, day_idx)
        entry_id = entry.id
        if self.template_mode:
            assert isinstance(entry, TemplateEntry)
            entry.day_of_week = new_day_idx
        else:
            assert isinstance(entry, TimeEntry)
            entry.date = self.day_date(new_day_idx).isoformat()
        entry.start_time = self._view_minute_to_hhmm(new_start)
        entry.end_time = self._view_minute_to_hhmm(new_start + duration)
        self._db_update_entry(entry)
        after = self._snapshot(entry, new_day_idx)
        self._push_undo({"kind": "update", "id": entry_id, "before": before, "after": after})
        self.selected_entry_id = entry_id
        self.refresh()

    # ------------------------------------------------------------------
    # Template <-> real-week bridging (see module docstring)
    # ------------------------------------------------------------------
    def _entry_day_idx(self, entry: EntryLike) -> int:
        """Which day column (0-4) an entry belongs in."""
        if self.template_mode:
            assert isinstance(entry, TemplateEntry)
            return entry.day_of_week
        assert isinstance(entry, TimeEntry)
        return (datetime.strptime(entry.date, "%Y-%m-%d").date() - self.week_start).days

    def _entry_day_label(self, entry: EntryLike) -> str:
        """Human-readable day for messages (delete confirmation, etc.)."""
        if self.template_mode:
            return config.DAY_NAMES[self._entry_day_idx(entry)]
        assert isinstance(entry, TimeEntry)
        return entry.date

    def _make_entry(self, entry_id, activity_id, activity_name, jira_key, color, day_idx,
                     start_time, end_time, notes, jira_project, issue_type,
                     jira_worklog_id=None, jira_worklog_issue=None,
                     jira_worklog_fingerprint=None) -> EntryLike:
        if self.template_mode:
            return TemplateEntry(
                entry_id, activity_id, activity_name, jira_key, color, day_idx,
                start_time, end_time, notes, jira_project=jira_project, issue_type=issue_type,
            )
        return TimeEntry(
            entry_id, activity_id, activity_name, jira_key, color,
            self.day_date(day_idx).isoformat(), start_time, end_time, notes,
            jira_project=jira_project, issue_type=issue_type,
            jira_worklog_id=jira_worklog_id,
            jira_worklog_issue=jira_worklog_issue,
            jira_worklog_fingerprint=jira_worklog_fingerprint,
        )

    def _db_list_entries(self) -> List[EntryLike]:
        if self.template_mode:
            return list(self.db.list_template_entries())
        week_dates = [self.day_date(i).isoformat() for i in range(len(config.DAY_NAMES))]
        return list(self.db.list_time_entries_for_week(week_dates))

    def _db_add_entry(self, entry: EntryLike) -> int:
        if self.template_mode:
            assert isinstance(entry, TemplateEntry)
            return self.db.add_template_entry(entry)
        assert isinstance(entry, TimeEntry)
        return self.db.add_time_entry(entry)

    def _db_update_entry(self, entry: EntryLike):
        if self.template_mode:
            assert isinstance(entry, TemplateEntry)
            self.db.update_template_entry(entry)
        else:
            assert isinstance(entry, TimeEntry)
            self.db.update_time_entry(entry)

    def _db_delete_entry(self, entry_id: int):
        if self.template_mode:
            self.db.delete_template_entry(entry_id)
        else:
            self.db.delete_time_entry(entry_id)

    # ------------------------------------------------------------------
    # Undo / redo
    #
    # Every kind of edit this grid makes -- create (drag, quick-assign, or
    # the dialog's Save), move, resize, edit-via-dialog, delete, and
    # duplicate (single or multi-day) -- reduces to one of three plain-dict
    # commands:
    #   {"kind": "add",    "items": [{"id": ..., "fields": {...}}, ...]}
    #   {"kind": "remove", "items": [{"id": ..., "fields": {...}}, ...]}
    #   {"kind": "update", "id": ..., "before": {...}, "after": {...}}
    # "fields"/"before"/"after" are plain-value snapshots from _snapshot()
    # below -- everything _make_entry() needs to reconstruct a row, plus
    # day_idx in place of a real date/day_of_week so it means the same
    # thing in both Timesheet and Template mode.
    #
    # Undoing/redoing an add or remove re-inserts rows rather than trying
    # to resurrect their exact old database id (SQLite just hands out a new
    # one) -- item["id"] is mutated in place after each such re-insert so
    # the *next* undo/redo of that same command targets the right row. That
    # only matters within a single command; nothing else ever depends on a
    # specific numeric id surviving across an undo/redo boundary. An
    # "update" never deletes/re-inserts its row, so its id is stable and
    # never needs to change.
    # ------------------------------------------------------------------
    def _snapshot(self, entry: EntryLike, day_idx: int) -> dict:
        return {
            "activity_id": entry.activity_id, "activity_name": entry.activity_name,
            "jira_key": entry.jira_key, "color": entry.color, "day_idx": day_idx,
            "start_time": entry.start_time, "end_time": entry.end_time,
            "notes": entry.notes, "jira_project": entry.jira_project,
            "issue_type": entry.issue_type,
            "jira_worklog_id": getattr(entry, "jira_worklog_id", None),
            "jira_worklog_issue": getattr(entry, "jira_worklog_issue", None),
            "jira_worklog_fingerprint": getattr(entry, "jira_worklog_fingerprint", None),
        }

    def _entry_from_fields(self, fields: dict) -> EntryLike:
        return self._make_entry(
            None, fields["activity_id"], fields["activity_name"], fields["jira_key"],
            fields["color"], fields["day_idx"], fields["start_time"], fields["end_time"],
            fields["notes"], fields["jira_project"], fields["issue_type"],
            jira_worklog_id=fields.get("jira_worklog_id"),
            jira_worklog_issue=fields.get("jira_worklog_issue"),
            jira_worklog_fingerprint=fields.get("jira_worklog_fingerprint"),
        )

    def _apply_fields_to_entry(self, entry: EntryLike, fields: dict):
        entry.activity_id = fields["activity_id"]
        entry.activity_name = fields["activity_name"]
        entry.jira_key = fields["jira_key"]
        entry.color = fields["color"]
        if self.template_mode:
            assert isinstance(entry, TemplateEntry)
            entry.day_of_week = fields["day_idx"]
        else:
            assert isinstance(entry, TimeEntry)
            entry.date = self.day_date(fields["day_idx"]).isoformat()
        entry.start_time = fields["start_time"]
        entry.end_time = fields["end_time"]
        entry.notes = fields["notes"]
        entry.jira_project = fields["jira_project"]
        entry.issue_type = fields["issue_type"]
        if not self.template_mode:
            assert isinstance(entry, TimeEntry)
            entry.jira_worklog_id = fields.get("jira_worklog_id")
            entry.jira_worklog_issue = fields.get("jira_worklog_issue")
            entry.jira_worklog_fingerprint = fields.get("jira_worklog_fingerprint")

    def _push_undo(self, command: dict):
        self._undo_stack.append(command)
        if len(self._undo_stack) > self.UNDO_LIMIT:
            self._undo_stack.pop(0)
        # A fresh action invalidates whatever could previously be redone --
        # same convention as every other undo/redo implementation (a
        # branching history isn't worth the complexity here).
        self._redo_stack.clear()

    def can_undo(self) -> bool:
        return bool(self._undo_stack)

    def can_redo(self) -> bool:
        return bool(self._redo_stack)

    def undo(self):
        if not self._undo_stack:
            return
        command = self._undo_stack.pop()
        self._apply_command(command, forward=False)
        self._redo_stack.append(command)

    def redo(self):
        if not self._redo_stack:
            return
        command = self._redo_stack.pop()
        self._apply_command(command, forward=True)
        self._undo_stack.append(command)

    def _apply_command(self, command: dict, forward: bool):
        kind = command["kind"]
        if kind == "add":
            if forward:  # redo an add -> re-insert every item
                for item in command["items"]:
                    item["id"] = self._db_add_entry(self._entry_from_fields(item["fields"]))
                self.selected_entry_id = command["items"][-1]["id"] if command["items"] else None
            else:  # undo an add -> remove every item
                for item in command["items"]:
                    if item["id"] is not None:
                        self._db_delete_entry(item["id"])
                        item["id"] = None
                self.selected_entry_id = None
        elif kind == "remove":
            if forward:  # redo a remove -> delete every item again
                for item in command["items"]:
                    if item["id"] is not None:
                        self._db_delete_entry(item["id"])
                        item["id"] = None
                self.selected_entry_id = None
            else:  # undo a remove -> re-insert every item
                for item in command["items"]:
                    item["id"] = self._db_add_entry(self._entry_from_fields(item["fields"]))
                self.selected_entry_id = command["items"][-1]["id"] if command["items"] else None
        elif kind == "update":
            entry = self.entries_by_id.get(command["id"])
            if entry is not None:
                self._apply_fields_to_entry(entry, command["after"] if forward else command["before"])
                self._db_update_entry(entry)
            self.selected_entry_id = command["id"]
        self.refresh()

    def _day_options(self):
        """[(label, day_idx), ...] for the Day dropdown in the time-block
        panel -- plain weekday names in template mode, "Weekday Mon DD" in
        normal mode."""
        if self.template_mode:
            return [(name, i) for i, name in enumerate(config.DAY_NAMES)]
        return [(f"{config.DAY_NAMES[i]} {self.day_date(i).strftime('%b %d')}", i)
                for i in range(len(config.DAY_NAMES))]

    def _apply_template(self):
        """Copy every block from the Template tab onto this grid's current
        week as real time entries. Slots that already have a block are left
        alone (never overwritten) and reported back."""
        week_dates = [self.day_date(i).isoformat() for i in range(len(config.DAY_NAMES))]
        created, skipped = self.db.apply_template_to_week(week_dates)
        if created == 0 and not skipped:
            messagebox.showinfo(
                "Apply Template",
                "The Template tab is empty -- add your recurring meetings there first, "
                "then apply it to a week.",
            )
            return
        self.refresh()
        msg = f"Added {created} block(s) from the template to this week."
        if skipped:
            nice = ", ".join(
                f"{config.DAY_NAMES[t.day_of_week]} {t.start_time}–{t.end_time}" for t in skipped
            )
            msg += f"\n\n{len(skipped)} skipped because that slot is already taken: {nice}"
        messagebox.showinfo("Apply Template", msg)

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------
    def refresh(self, *, paint_entries: bool = True):
        """Redraw the whole grid + entries from the database."""
        if paint_entries:
            self._cancel_entry_paint()
        entries = self._db_list_entries()
        overlays = self._overlay_events()
        if self._update_view_hours(entries, extra=overlays) and paint_entries:
            self._relayout_for_view_hours()
            return
        c = self.canvas
        c.delete("all")
        c._aa_images = []

        if self.template_mode:
            self.week_label.config(text="Recurring Weekly Template")
        else:
            end_date = self.day_date(len(config.DAY_NAMES) - 1)
            self.week_label.config(
                text=f"{self.week_start.strftime('%b %d')} – {end_date.strftime('%b %d, %Y')}"
            )
        self._update_hint()
        self._reflow_nav_row()

        canvas_width = self.gutter_width + len(config.DAY_NAMES) * self.day_width
        grid_top = self.header_height
        grid_bottom = grid_top + self._view_minutes_total() * self.px_per_min

        # Header background + day labels. Today's date used to sit on the
        # bottom lip of a too-short chip (half on the blue, half on the
        # header), so the chip is now a real two-line badge with padding
        # around both the weekday and the date.
        c.create_rectangle(self.gutter_width, 0, canvas_width, grid_top,
                            fill=theme.HEADER_BG, outline="")
        today = date.today()
        name_y = grid_top / 2 if self.template_mode else 20
        date_y = 38
        for i, name in enumerate(config.DAY_NAMES):
            x0 = self.gutter_width + i * self.day_width
            x1 = x0 + self.day_width
            is_today = (not self.template_mode) and self.day_date(i) == today
            if is_today:
                c.create_rectangle(x0, grid_top, x1, grid_bottom,
                                    fill=theme.TODAY_TINT, outline="", tags="today_col")
                theme.place_rounded_rect(
                    c, x0 + 6, 6, x1 - 6, grid_top - 6,
                    radius=12, fill=theme.ACCENT_SOFT, outline="",
                    background=theme.HEADER_BG,
                )
            name_color = theme.ACCENT if is_today else theme.TEXT_PRIMARY
            c.create_text((x0 + x1) / 2, name_y, text=name, font=(self.family, 10, "bold"),
                           fill=name_color, justify="center")
            if not self.template_mode:
                date_color = theme.ACCENT if is_today else theme.TEXT_SECONDARY
                c.create_text((x0 + x1) / 2, date_y, text=self.day_date(i).strftime("%b %d"),
                               font=(self.family, 9, "bold" if is_today else "normal"),
                               fill=date_color, justify="center")

        # Horizontal slot lines + hour labels
        minute = 0
        while minute <= self._view_minutes_total():
            y = grid_top + minute * self.px_per_min
            is_hour = (minute % 60 == 0)
            c.create_line(self.gutter_width, y, canvas_width, y,
                           fill=theme.GRID_LINE_HOUR if is_hour else theme.GRID_LINE)
            if is_hour:
                hour = self._view_start_hour + minute // 60
                label = datetime.strptime(str(hour % 24), "%H").strftime("%I %p").lstrip("0")
                c.create_text(self.gutter_width - 8, y, text=label, anchor="e",
                               font=(self.family, 8), fill=theme.TEXT_MUTED)
            minute += config.SLOT_MINUTES

        # Vertical column separators
        for i in range(len(config.DAY_NAMES) + 1):
            x = self.gutter_width + i * self.day_width
            c.create_line(x, 0, x, grid_bottom, fill=theme.GRID_LINE_HOUR)

        # Imported work-calendar meetings sit behind logged blocks: pale,
        # not selectable, not counted in totals. Drawn before the now-line
        # and real entries so a booked block covers the guide underneath.
        if paint_entries:
            self._overlay_drawn = []
            for event in overlays:
                day_idx = self._overlay_day_idx(event)
                if day_idx is not None:
                    self._draw_overlay_event(event, day_idx)

        # "Now" indicator on today's column (normal mode only -- the
        # template isn't tied to any real date)
        if not self.template_mode:
            self._draw_now_line(today, grid_top)

        # Entries. Overlapping blocks are allowed (see _layout_day_entries)
        # rather than rejected, so group them by day first to work out how
        # many side-by-side columns each day actually needs before drawing
        # anything.
        self.entries_by_id = {}
        self._hover_slot = None
        n_days = len(config.DAY_NAMES)
        totals = [0] * n_days
        entries_by_day: List[List[EntryLike]] = [[] for _ in range(n_days)]
        for e in entries:
            self.entries_by_id[e.id] = e
            day_idx = self._entry_day_idx(e)
            totals[day_idx] += e.duration_minutes()
            entries_by_day[day_idx].append(e)

        # A selection whose entry no longer exists (deleted via undo,
        # another path, etc.) is stale -- drop it rather than leaving a
        # dangling id that _draw_entry/_delete_selected_entry would have to
        # separately guard against.
        if self.selected_entry_id is not None and self.selected_entry_id not in self.entries_by_id:
            self.selected_entry_id = None

        if paint_entries:
            for day_idx, day_entries in enumerate(entries_by_day):
                layout = self._layout_day_entries(day_entries)
                for e in day_entries:
                    assert e.id is not None
                    col_idx, col_count = layout[e.id]
                    self._draw_entry(e, day_idx, col_idx, col_count,
                                      is_selected=(e.id == self.selected_entry_id))

        self._draw_day_totals(totals, today)

        # Do not give this nested canvas its own scrollregion. `_scroll_host`
        # is the only scroller; bbox("all") here made Tk treat the inner
        # canvas as a second one, so event.y no longer matched item
        # coordinates once the host was panned — slots above the now-line
        # stopped being creatable or droppable.

    def _schedule_entry_paint(self):
        if self._entry_paint_job is not None:
            return
        try:
            self._entry_paint_job = self.after(1, self._paint_entries)
        except tk.TclError:
            self._entry_paint_job = None

    def _paint_entries(self):
        self._entry_paint_job = None
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        self.refresh()

    def _cancel_entry_paint(self):
        job = self._entry_paint_job
        if job is None:
            return
        self._entry_paint_job = None
        try:
            self.after_cancel(job)
        except tk.TclError:
            pass

    def _on_grid_destroy(self, event):
        if event.widget is not self:
            return
        self._cancel_now_line()

    def _schedule_now_line(self):
        """Keep the orange now-line in the current minute without a full
        grid refresh. Template weeks have no 'today', so they skip this."""
        if self.template_mode:
            return
        self._cancel_now_line()
        try:
            if not self.winfo_exists():
                return
            self._now_line_job = self.after(_now_line_delay_ms(), self._tick_now_line)
        except tk.TclError:
            self._now_line_job = None

    def _cancel_now_line(self):
        job = self._now_line_job
        if job is None:
            return
        self._now_line_job = None
        try:
            self.after_cancel(job)
        except tk.TclError:
            pass

    def _tick_now_line(self):
        self._now_line_job = None
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        self._redraw_now_line()
        self._schedule_now_line()

    def _redraw_now_line(self):
        """Move just the now-line; leave the rest of the grid alone."""
        if self.template_mode:
            return
        try:
            self.canvas.delete("now_line")
        except tk.TclError:
            return
        self._draw_now_line(date.today(), self.header_height)
        if not self.canvas.find_withtag("now_line"):
            return
        # Match refresh() z-order: overlays, then now-line, then entries.
        if self.canvas.find_withtag("entry"):
            self.canvas.tag_lower("now_line", "entry")
        elif self.canvas.find_withtag("overlay"):
            self.canvas.tag_raise("now_line", "overlay")

    def _draw_day_totals(self, totals: List[int], today: date):
        """Paint each day's hours at the same x as that day's grid column."""
        c = self._totals_host
        c.delete("total")
        h = config.TOTALS_ROW_HEIGHT_PX
        font = getattr(self, "_totals_font", None)
        if font is None:
            font = tkfont.Font(family=self.family, size=9, weight="bold")
            self._totals_font = font
        for i, mins in enumerate(totals):
            x0 = self.gutter_width + i * self.day_width
            cx = x0 + self.day_width / 2.0
            is_today = (not self.template_mode) and self.day_date(i) == today
            text = format_day_total_hours(mins)
            if is_today:
                pill_w = font.measure(text) + 24
                pill_h = 22
                theme.place_rounded_rect(
                    c, cx - pill_w / 2, (h - pill_h) / 2,
                    cx + pill_w / 2, (h + pill_h) / 2,
                    radius=10, fill=theme.ACCENT_SOFT, outline="",
                    background=theme.PANEL_BG, tags="total")
            c.create_text(
                cx, h / 2,
                text=text,
                font=font,
                fill=theme.ACCENT if is_today else theme.TEXT_SECONDARY,
                anchor="center",
                tags="total")

    def _draw_now_line(self, today: date, grid_top: float):
        for i in range(len(config.DAY_NAMES)):
            if self.day_date(i) != today:
                continue
            now = datetime.now()
            minute_of_day = now.hour * 60 + now.minute - self._view_start_hour * 60
            if not (0 <= minute_of_day <= self._view_minutes_total()):
                return
            x0 = self.gutter_width + i * self.day_width
            x1 = x0 + self.day_width
            y = grid_top + minute_of_day * self.px_per_min
            # Tight shapes only. A full-width create_line can report a bbox
            # from y=0 down to the line, which would steal hits on every
            # slot before now.
            self.canvas.create_oval(
                x0 - 3, y - 3, x0 + 3, y + 3,
                fill=theme.NOW_LINE, outline="", tags="now_line")
            self.canvas.create_rectangle(
                x0, y - 1, x1, y + 1,
                fill=theme.NOW_LINE, outline="", tags="now_line")
            return

    def _update_hint(self):
        armed = self.get_armed_activity()
        dropping = self._drag_state and self._drag_state.get("mode") == "qdm-drop"
        if dropping:
            act = self._drag_state["activity"]
            self.hint_label.config(
                text=f"  Drop “{act.name}” on a day — {config.QDM_DROP_MINUTES} min, then add a description  ",
                bg=theme.ACCENT_SOFT, fg=theme.ACCENT,
            )
            self._pack_hint()
        elif armed:
            self.hint_label.config(
                text=f"  Placing “{armed.name}” — click a slot (Esc to cancel)  ",
                bg=theme.ACCENT_SOFT, fg=theme.ACCENT,
            )
            self._pack_hint()
        else:
            self.hint_label.config(text="", bg=theme.PANEL_BG, fg=theme.TEXT_SECONDARY)
            self.hint_label.pack_forget()
            self._hint_visible = False

    def _pack_apply_template(self):
        btn = self.apply_template_btn
        zoom = getattr(self, "_zoom_row", None)
        if btn is None:
            return
        try:
            if zoom is not None:
                btn.pack(side="right", after=zoom, padx=(0, 8))
            else:
                btn.pack(side="right", padx=(0, 8))
        except tk.TclError:
            btn.pack(side="right", padx=(0, 8))

    def _pack_hint(self):
        anchor = None
        btn = self.apply_template_btn
        if btn is not None and self._apply_template_visible:
            try:
                if btn.winfo_ismapped():
                    anchor = btn
            except tk.TclError:
                pass
        if anchor is None:
            anchor = getattr(self, "_zoom_row", None)
        try:
            if anchor is not None:
                self.hint_label.pack(side="right", after=anchor, padx=(0, 8))
            else:
                self.hint_label.pack(side="right", padx=(0, 8))
            self._hint_visible = True
        except tk.TclError:
            pass

    def _layout_day_entries(self, day_entries: List[EntryLike]) -> Dict[int, Tuple[int, int]]:
        """Overlapping blocks aren't rejected (see the removed overlap
        checks in _finish_create/_finish_entry_drag/_open_entry_dialog/
        _open_duplicate_dialog) -- instead, like Toggl/Google Calendar, each
        one gets squeezed into a narrower side-by-side column so every
        overlapping block stays visible and clickable instead of one
        hiding behind another.

        Returns {entry_id: (col_index, col_count)}. Entries are grouped
        into clusters of entries that transitively overlap in time (so an
        unrelated block elsewhere in the same day still gets the full day
        width -- it isn't squeezed just because *something else* that day
        happens to overlap). Within a cluster, entries are greedily packed
        into as few columns as needed (an entry reuses the first column
        whose previous occupant has already ended by the time it starts).
        """
        result: Dict[int, Tuple[int, int]] = {}
        items = sorted(day_entries,
                        key=lambda e: (self._view_hhmm_to_minute(e.start_time), self._view_hhmm_to_minute(e.end_time)))

        def flush(cluster_items):
            if not cluster_items:
                return
            column_end_minutes: List[int] = []  # end-minute of each column's last occupant so far
            col_of_id: Dict[int, int] = {}
            for e in cluster_items:
                assert e.id is not None
                start = self._view_hhmm_to_minute(e.start_time)
                end = self._view_hhmm_to_minute(e.end_time)
                placed = False
                for ci, last_end in enumerate(column_end_minutes):
                    if start >= last_end:
                        column_end_minutes[ci] = end
                        col_of_id[e.id] = ci
                        placed = True
                        break
                if not placed:
                    column_end_minutes.append(end)
                    col_of_id[e.id] = len(column_end_minutes) - 1
            col_count = len(column_end_minutes)
            for e in cluster_items:
                assert e.id is not None
                result[e.id] = (col_of_id[e.id], col_count)

        cluster: List[EntryLike] = []
        cluster_end = -1
        for e in items:
            start = self._view_hhmm_to_minute(e.start_time)
            end = self._view_hhmm_to_minute(e.end_time)
            if cluster and start >= cluster_end:
                flush(cluster)
                cluster = []
                cluster_end = -1
            cluster.append(e)
            cluster_end = max(cluster_end, end)
        flush(cluster)

        return result

    def _day_column_bg(self, day_idx: int) -> str:
        """The grid color actually sitting under this day column.

        Today's column is TODAY_TINT, not GRID_BG. PhotoImage corners that
        blend toward GRID_BG stamp a black square over that tint.
        """
        if (not self.template_mode) and self.day_date(day_idx) == date.today():
            return theme.TODAY_TINT
        return theme.GRID_BG

    def _entry_geometry(self, entry: EntryLike, day_idx: int, col_index: int = 0, col_count: int = 1):
        day_x0 = self.gutter_width + day_idx * self.day_width + 3
        day_x1 = day_x0 + self.day_width - 6
        col_width = (day_x1 - day_x0) / max(1, col_count)
        x0 = day_x0 + col_index * col_width
        x1 = x0 + col_width
        if col_count > 1:
            # A hairline gap between side-by-side blocks so they read as
            # distinct blocks rather than one solid strip.
            gap = 2
            if col_index > 0:
                x0 += gap / 2
            if col_index < col_count - 1:
                x1 -= gap / 2
        y0 = self.header_height + self._view_hhmm_to_minute(entry.start_time) * self.px_per_min
        y1 = self.header_height + self._view_hhmm_to_minute(entry.end_time) * self.px_per_min
        return x0, y0, x1, y1

    _PREVIEW_CORNER_STEPS = max(18, int(config.BLOCK_CORNER_RADIUS * 2.5))

    def _set_live_block(self, state, key, x0, y0, x1, y1, *, fill, outline="", width=0,
                         mute=True):
        """Rounded live preview that can be reshaped every motion event.

        Settled blocks are coverage-AA PhotoImages. Live resize/move keeps
        a polygon so a new image isn't rasterized on every pixel; fill is
        still the muted project color, and a PhotoImage is restored on
        mouse-up via refresh().
        """
        if mute:
            fill = theme.calendar_block_fill(fill)
        radius = min(config.BLOCK_CORNER_RADIUS, max(0.0, (x1 - x0) / 2), max(0.0, (y1 - y0) / 2))
        points = theme.rounded_rect_points(
            x0, y0, x1, y1, radius=radius, steps=self._PREVIEW_CORNER_STEPS)
        item = state.get(key)
        if item is None:
            state[key] = self.canvas.create_polygon(
                points, fill=fill, outline=outline, width=width, joinstyle="round")
        else:
            self.canvas.coords(item, *points)
            try:
                self.canvas.itemconfigure(item, fill=fill)
            except tk.TclError:
                pass

    def _entry_text_lines(self, entry: EntryLike, x0, y0, x1, y1):
        """Fit name / notes / time inside the block, wrapping long text
        onto extra lines when the block is tall enough. Notes still beat
        the time range when space is tight, so two blocks of the same
        QDM stay distinguishable."""
        avail_w = max(10, x1 - x0 - 16)
        avail_h = max(8, y1 - y0 - 8)
        chars_per_line = max(6, int(avail_w / 6.0))
        line_pitch = 14
        max_lines = max(1, int(avail_h // line_pitch))

        name = (entry.activity_name or "").strip()
        if entry.jira_key:
            name = f"{name}  ·  {entry.jira_key}" if name else entry.jira_key.strip()
        name_parts = wrap_block_text(name, chars_per_line)
        notes_parts = wrap_block_text(
            " ".join(entry.notes.split()) if entry.notes else "", chars_per_line)
        time_line = f"{entry.start_time}–{entry.end_time}"

        chosen = _pack_block_lines(
            [(name_parts, True), (notes_parts, False)],
            max_lines, chars_per_line, time_line)

        result = []
        ty = y0 + 4
        for text, bold in chosen:
            result.append((text, bold, ty))
            ty += line_pitch
        return result

    def _draw_entry(self, entry: EntryLike, day_idx: int, col_index: int = 0, col_count: int = 1,
                     is_selected: bool = False):
        x0, y0, x1, y1 = self._entry_geometry(entry, day_idx, col_index, col_count)
        tag = f"entry_{entry.id}"
        # A selected block (see self.selected_entry_id) gets a thicker,
        # high-contrast outline instead of the normal thin one -- the same
        # SELECTION_OUTLINE color every theme defines but nothing drew with
        # until this keyboard-selection feature existed.
        surface = self._day_column_bg(day_idx)
        fill = theme.calendar_block_fill(entry.color, surface)
        outline_color = theme.SELECTION_OUTLINE if is_selected else ""
        outline_width = 2.5 if is_selected else 0
        ids = theme.place_rounded_rect(
            self.canvas, x0, y0, x1, y1, radius=config.BLOCK_CORNER_RADIUS,
            fill=fill, outline=outline_color, width=outline_width,
            background=surface, tags=("entry", tag), full=True,
            transparent_outside=True,
        )
        rect = ids[-1]
        title_color = theme.calendar_block_title_color(entry.color, fill)
        notes_color = theme.calendar_block_notes_color(entry.color, fill)
        unsent = (not self.template_mode and isinstance(entry, TimeEntry)
                  and entry_needs_push(entry) and (x1 - x0) > 20 and (y1 - y0) > 12)
        reserve = _unsent_label_reserve(y1 - y0) if unsent else 0
        unsent_w = 0
        if unsent and reserve == 0:
            caption, caption_font = self._unsent_caption_text(x1 - x0)
            unsent_w = caption_font.measure(caption) + 12
        rail = 5
        for text, is_bold, ty in self._entry_text_lines(
                entry, x0 + rail, y0, x1 - unsent_w, y1 - reserve):
            item = self.canvas.create_text(
                x0 + 8 + rail, ty, text=text, anchor="nw",
                font=(self.family, 9 if is_bold else 8, "bold" if is_bold else "normal"),
                fill=title_color if is_bold else notes_color, tags=("entry_text", tag),
            )
            self.canvas.tag_raise(item, rect)
        if (x1 - x0) > 22 and (y1 - y0) > 16:
            self._draw_color_rail(x0, y0, y1, tag,
                                  theme.calendar_block_rail_color(entry.color, fill))
        if unsent:
            self._draw_unsent_label(x0 + rail, y0, x1, y1, fill, tag, entry.color)

    def _draw_color_rail(self, x0, y0, y1, tag, color):
        """3px identity stripe — Apple Calendar's colored leading edge."""
        x = x0 + 5
        pad = 8 if (y1 - y0) > 24 else 5
        self.canvas.create_line(
            x, y0 + pad, x, y1 - pad,
            fill=color, width=3, capstyle="round",
            tags=("entry", tag),
        )

    def _unsent_caption_text(self, block_width: float):
        font = tkfont.Font(family=self.family, size=8)
        label = "Not synced"
        if font.measure(label) > (block_width - 16):
            label = "Unsynced"
        return label, font

    def _draw_unsent_label(self, x0, y0, x1, y1, block_fill, tag, project_color=None):
        """Quiet 'Not synced' caption — no chip, no alarm red.

        Tall enough blocks stack it under the title. 15-minute slots put
        it on the title line, right-aligned, so the two strings cannot
        paint over each other.
        """
        if (y1 - y0) < 14:
            return
        label, font = self._unsent_caption_text(x1 - x0)
        fill = theme.unsent_label_color(block_fill, project_color)
        if (y1 - y0) < _UNSENT_STACK_MIN_HEIGHT:
            self.canvas.create_text(
                x1 - 6, y0 + 4, text=label, anchor="ne",
                font=font, fill=fill, tags=("entry", tag, "unsent"),
            )
            return
        self.canvas.create_text(
            x0 + 8, y1 - 6, text=label, anchor="sw",
            font=font, fill=fill, tags=("entry", tag, "unsent"),
        )

    def _overlay_events(self) -> List[CalendarEvent]:
        """Imported meetings for this week -- empty on the Template tab."""
        if self.template_mode or self.get_overlay_events is None:
            return []
        try:
            events = self.get_overlay_events(self.week_start) or []
        except Exception:
            return []
        return [e for e in events if not getattr(e, "all_day", False)]

    def _overlay_day_idx(self, event: CalendarEvent) -> Optional[int]:
        try:
            day = date.fromisoformat(event.date)
        except (TypeError, ValueError):
            return None
        idx = (day - self.week_start).days
        if 0 <= idx < len(config.DAY_NAMES):
            return idx
        return None

    def _overlay_text_lines(self, event: CalendarEvent, x0, y0, x1, y1):
        """Wrap the meeting title (and location/time if the block is tall
        enough) the same way logged blocks wrap QDM names -- one ellipsis
        line was wasting the rest of the pale rectangle."""
        avail_w = max(10, x1 - x0 - 16)
        avail_h = max(8, y1 - y0 - 8)
        chars_per_line = max(6, int(avail_w / 6.0))
        line_pitch = 14
        max_lines = max(1, int(avail_h // line_pitch))

        title = (event.title or "").strip() or "(busy)"
        title_parts = wrap_block_text(title, chars_per_line)
        location_parts = wrap_block_text(
            " ".join((event.location or "").split()), chars_per_line)
        time_line = f"{event.start_time}–{event.end_time}"

        chosen = _pack_block_lines(
            [(title_parts, True), (location_parts, False)],
            max_lines, chars_per_line, time_line)

        result = []
        ty = y0 + 4
        for text, bold in chosen:
            result.append((text, bold, ty))
            ty += line_pitch
        return result

    def _draw_overlay_event(self, event: CalendarEvent, day_idx: int):
        x0, y0, x1, y1 = self._entry_geometry(event, day_idx)
        grid_top = self.header_height
        grid_bottom = grid_top + self._view_minutes_total() * self.px_per_min
        y0 = max(y0, grid_top)
        y1 = min(y1, grid_bottom)
        if y1 - y0 < 4:
            return
        index = len(self._overlay_drawn)
        self._overlay_drawn.append(event)
        tag = f"guide_{index}"
        surface = self._day_column_bg(day_idx)
        fill = theme.overlay_fill(surface)
        outline = theme.overlay_outline(surface)
        ids = theme.place_rounded_rect(
            self.canvas, x0, y0, x1, y1, radius=config.BLOCK_CORNER_RADIUS,
            fill=fill, outline=outline, width=1,
            background=surface, tags=("overlay", tag), full=True,
            transparent_outside=True,
        )
        if y1 - y0 < 14:
            return
        rect = ids[-1] if ids else None
        for text, is_bold, ty in self._overlay_text_lines(event, x0, y0, x1, y1):
            item = self.canvas.create_text(
                x0 + 8, ty, text=text, anchor="nw",
                font=(self.family, 9 if is_bold else 8, "bold" if is_bold else "normal"),
                fill=theme.overlay_text_color(),
                tags=("overlay", "overlay_text", tag),
            )
            if rect is not None:
                self.canvas.tag_raise(item, rect)

    def _overlay_event_at(self, x: float, y: float) -> Optional[CalendarEvent]:
        for item in reversed(self.canvas.find_overlapping(x, y, x, y)):
            for t in self.canvas.gettags(item):
                if not t.startswith("guide_"):
                    continue
                try:
                    idx = int(t.split("_", 1)[1])
                except ValueError:
                    continue
                if 0 <= idx < len(self._overlay_drawn):
                    return self._overlay_drawn[idx]
        return None

    # ------------------------------------------------------------------
    # Hit testing helpers
    # ------------------------------------------------------------------
    def _day_idx_for_x(self, x: float) -> Optional[int]:
        if x < self.gutter_width:
            return None
        idx = int((x - self.gutter_width) // self.day_width)
        if 0 <= idx < len(config.DAY_NAMES):
            return idx
        return None

    def _snapped_minute_for_y(self, y: float) -> int:
        """Map a grid y to a slot. Bounded only by the visible day, never by now."""
        raw = (y - self.header_height) / self.px_per_min
        snapped = round(raw / config.SLOT_MINUTES) * config.SLOT_MINUTES
        return max(0, min(self._view_minutes_total(), snapped))

    def _entry_id_at(self, x: float, y: float) -> Optional[int]:
        # Topmost item wins so a later block's hit-box cannot cover empty
        # morning slots (or the now-line) sitting visually underneath.
        for item in reversed(self.canvas.find_overlapping(x, y, x, y)):
            for t in self.canvas.gettags(item):
                if t.startswith("entry_"):
                    return int(t.split("_", 1)[1])
        return None

    def _hit_region(self, entry_id: int, y: float) -> str:
        entry = self.entries_by_id[entry_id]
        day_idx = self._entry_day_idx(entry)
        _, y0, _, y1 = self._entry_geometry(entry, day_idx)
        if abs(y - y0) <= config.RESIZE_GRIP_PX:
            return "resize-top"
        if abs(y - y1) <= config.RESIZE_GRIP_PX:
            return "resize-bottom"
        return "move"

    # ------------------------------------------------------------------
    # Mouse handlers
    # ------------------------------------------------------------------
    def _on_button1(self, event):
        # Canvas widgets don't grab keyboard focus on click by themselves in
        # Tk -- without this, Escape/Delete/arrow-key navigation would only
        # ever work after some *other* path happened to focus the canvas
        # (e.g. tab-switching -- see MainWindow._on_tab_changed), not right
        # after the click that a user would naturally expect to enable them.
        self.canvas.focus_set()

        x, y = self._grid_xy(event)
        entry_id = self._entry_id_at(x, y)
        if entry_id is not None:
            if event.state & CONTROL_STATE_MASK:
                # Ctrl+click on a block duplicates it on the spot -- a
                # keyboard-modifier shortcut for the same thing the
                # right-click "Duplicate…" menu item does through a dialog.
                # The copy lands in the exact same slot as the original, so
                # it necessarily overlaps it; that's fine now that
                # overlapping blocks render side by side (_layout_day_entries)
                # instead of being rejected.
                self._duplicate_entry_in_place(entry_id)
                self._drag_state = None
                return
            region = self._hit_region(entry_id, y)
            entry = self.entries_by_id[entry_id]
            day_idx = self._entry_day_idx(entry)
            self._drag_state = {
                "mode": region,
                "entry_id": entry_id,
                "orig_entry": entry,
                "start_day_idx": day_idx,
                "start_minute": self._view_hhmm_to_minute(entry.start_time),
                "end_minute": self._view_hhmm_to_minute(entry.end_time),
                "anchor_x": x,
                "anchor_y": y,
                "moved": False,
            }
            return

        # Empty-area click: deselect if something was highlighted, then
        # start a create-drag. A click that never moves past the drag
        # threshold will not open Time Block (see _finish_create) — that's
        # how you click away to deselect without being asked to log time.
        if self.selected_entry_id is not None:
            self.selected_entry_id = None
            self.refresh()

        day_idx = self._day_idx_for_x(x)
        if day_idx is None:
            return
        minute = self._snapped_minute_for_y(y)
        self._drag_state = {
            "mode": "create",
            "anchor_day_idx": day_idx,
            "anchor_minute": minute,
            "cur_day_idx": day_idx,
            "cur_minute": minute,
            "anchor_x": x,
            "anchor_y": y,
            "preview_rect": None,
            "moved": False,
        }

    def _on_double_click(self, event):
        """Double-clicking a time block jumps straight to its Edit tab --
        the same place right-click -> "Edit..." goes, just one motion
        instead of two. Empty-area double-clicks do nothing extra: the
        first of the two clicks that make up a double-click already ran
        _on_button1/_on_release as an ordinary click (Tk fires the
        single-click bindings for every press/release; <Double-Button-1>
        additionally fires on top of that for the second press only --
        see CONTROL_STATE_MASK's comment above for the same
        "most-specific-pattern-wins" rule), so on empty space that first
        click already ran as an ordinary empty-space click (deselect, or
        quick-assign if a QDM is armed); there's nothing more to do here
        for that case.

        entry_id is looked up fresh here rather than reusing anything
        from _drag_state, since _on_button1 already ran once for this same
        double-click's first press and may have started a drag/duplicate
        of its own -- that's discarded below in favor of just opening the
        edit dialog, which is the one unambiguous thing a double-click on
        a block should do.
        """
        x, y = self._grid_xy(event)
        entry_id = self._entry_id_at(x, y)
        if entry_id is None:
            return
        self._drag_state = None
        entry = self.entries_by_id[entry_id]
        self.selected_entry_id = entry_id
        self._edit_entry(entry)

    def _on_motion_drag(self, event):
        if not self._drag_state:
            return
        state = self._drag_state
        if state.get("mode") == "qdm-drop":
            return
        self._autoscroll_for_pointer(event)
        x, y = self._grid_xy(event)
        dx = x - state["anchor_x"]
        dy = y - state["anchor_y"]
        if abs(dx) > config.DRAG_THRESHOLD_PX or abs(dy) > config.DRAG_THRESHOLD_PX:
            state["moved"] = True

        if state["mode"] == "create":
            self._update_create_preview(x, y, state)
        elif state["mode"] in ("resize-top", "resize-bottom", "move"):
            # Only touch the real canvas items once an actual drag (past the
            # click threshold) is confirmed -- sub-pixel jitter on a plain
            # click must never mutate/replace the settled entry item.
            if state["moved"]:
                self._update_entry_drag_preview(x, y, state)

    def _update_create_preview(self, x, y, state: dict):
        day_idx = self._day_idx_for_x(x)
        if day_idx is None:
            day_idx = state["anchor_day_idx"]
        minute = self._snapped_minute_for_y(y)
        state["cur_day_idx"] = state["anchor_day_idx"]  # creation stays within the starting day
        state["cur_minute"] = minute

        start_min = min(state["anchor_minute"], minute)
        end_min = max(state["anchor_minute"], minute)
        if end_min == start_min:
            end_min = min(self._view_minutes_total(), start_min + config.SLOT_MINUTES)

        x0 = self.gutter_width + day_idx * self.day_width + 3
        x1 = x0 + self.day_width - 6
        y0 = self.header_height + start_min * self.px_per_min
        y1 = self.header_height + end_min * self.px_per_min

        self._set_live_block(
            state, "preview_rect", x0, y0, x1, y1,
            fill=theme.PREVIEW_FILL, mute=False)

    def _update_entry_drag_preview(self, x, y, state: dict):
        entry = state["orig_entry"]

        if "drag_rect_id" not in state:
            # First confirmed-drag frame: swap the settled (rounded) entry
            # items for a live rounded polygon we can reshape every motion
            # event. The real AA image is restored by the refresh() that
            # always runs at the end of the drag.
            for item in self.canvas.find_withtag(f"entry_{state['entry_id']}"):
                self.canvas.delete(item)
            state["drag_rect_id"] = None
            state["drag_text_id"] = self.canvas.create_text(
                0, 0, text="", anchor="nw",
                fill=theme.calendar_block_title_color(entry.color),
                font=(self.family, 8, "bold"), justify="left",
            )

        text_id = state["drag_text_id"]
        duration = state["end_minute"] - state["start_minute"]

        if state["mode"] == "resize-top":
            new_start = self._snapped_minute_for_y(y)
            new_start = min(new_start, state["end_minute"] - config.SLOT_MINUTES)
            new_start = max(0, new_start)
            new_end = state["end_minute"]
            day_idx = state["start_day_idx"]
        elif state["mode"] == "resize-bottom":
            new_end = self._snapped_minute_for_y(y)
            new_end = max(new_end, state["start_minute"] + config.SLOT_MINUTES)
            new_end = min(self._view_minutes_total(), new_end)
            new_start = state["start_minute"]
            day_idx = state["start_day_idx"]
        else:  # move
            dy_minutes = round((y - state["anchor_y"]) / self.px_per_min / config.SLOT_MINUTES) * config.SLOT_MINUTES
            new_start = state["start_minute"] + dy_minutes
            new_start = max(0, min(self._view_minutes_total() - duration, new_start))
            new_end = new_start + duration
            day_idx = self._day_idx_for_x(x)
            if day_idx is None:
                day_idx = state["start_day_idx"]

        state["preview_day_idx"] = day_idx
        state["preview_start"] = new_start
        state["preview_end"] = new_end

        x0 = self.gutter_width + day_idx * self.day_width + 3
        x1 = x0 + self.day_width - 6
        y0 = self.header_height + new_start * self.px_per_min
        y1 = self.header_height + new_end * self.px_per_min
        self._set_live_block(
            state, "drag_rect_id", x0, y0, x1, y1,
            fill=entry.color)
        rect_id = state["drag_rect_id"]

        label = entry.activity_name
        if entry.jira_key:
            label += f" [{entry.jira_key}]"
        label += f"\n{self._view_minute_to_hhmm(new_start)}–{self._view_minute_to_hhmm(new_end)}"
        self.canvas.itemconfigure(text_id, text=label, width=max(10, x1 - x0 - 10))
        self.canvas.coords(text_id, x0 + 6, y0 + 4)
        self.canvas.tag_raise(text_id, rect_id)

    def _on_release(self, event):
        state = self._drag_state
        if state is None:
            return
        if state.get("mode") == "qdm-drop":
            return
        self._drag_state = None

        if state["mode"] == "create":
            self._finish_create(event, state)
        else:
            self._finish_entry_drag(state)

    def _finish_create(self, event, state):
        if state["preview_rect"] is not None:
            self.canvas.delete(state["preview_rect"])

        day_idx = state["anchor_day_idx"]

        if not state["moved"]:
            # Plain click on empty space. An armed QDM still places a
            # block (that's the "click a slot" shortcut). Otherwise this
            # is just click-away: selection was already cleared in
            # _on_button1. Opening Time Block here is what made deselect
            # feel like creating a log — drag a slot to add one instead.
            armed = self.get_armed_activity()
            start_min = state["anchor_minute"]
            if armed:
                duration = armed.default_duration_minutes or config.QDM_DROP_MINUTES
                end_min = min(self._view_minutes_total(), start_min + duration)
                if end_min == start_min:
                    return
                self._place_qdm_block(armed, day_idx, start_min, end_min)
            return

        start_min = min(state["anchor_minute"], state["cur_minute"])
        end_min = max(state["anchor_minute"], state["cur_minute"])
        if end_min == start_min:
            end_min = min(self._view_minutes_total(), start_min + config.SLOT_MINUTES)
        armed = self.get_armed_activity()
        if armed:
            self._place_qdm_block(armed, day_idx, start_min, end_min)
            return
        self._open_entry_dialog(new=True, day_idx=day_idx,
                                 start_hhmm=self._view_minute_to_hhmm(start_min),
                                 end_hhmm=self._view_minute_to_hhmm(end_min),
                                 require_notes=True)

    def begin_qdm_drop(self, activity: Activity, event=None):
        """Start a drag from the QDM list onto this grid.

        A card follows the pointer on the grid (drawn like a time block,
        not a rectangular window). The slot under the pointer is always a
        30-minute block on that day — duration is edited on the Time
        Block tab after drop, so crossing Monday on the way to Tuesday
        cannot lock the day.
        """
        if self._drag_state and self._drag_state.get("mode") == "qdm-drop":
            return
        self._drag_state = {
            "mode": "qdm-drop",
            "activity": activity,
            "preview_rect": None,
            "preview_day_idx": None,
            "preview_start": None,
            "preview_end": None,
        }
        try:
            self.canvas.config(cursor="plus")
            self.winfo_toplevel().config(cursor="plus")
        except tk.TclError:
            pass
        self._ensure_qdm_ghost(activity, event)
        self._update_hint()
        root = self.winfo_toplevel()
        self._qdm_drop_binds = {
            "motion": root.bind("<B1-Motion>", self._on_qdm_drop_motion, add="+"),
            "release": root.bind("<ButtonRelease-1>", self._on_qdm_drop_release, add="+"),
        }

    def _ensure_qdm_ghost(self, activity: Activity, event=None):
        if self._qdm_ghost is not None:
            return
        fill = theme.calendar_block_fill(activity.color or theme.ACCENT)
        fg = theme.calendar_block_title_color(activity.color or theme.ACCENT, fill)
        name = (activity.name or "").strip() or "QDM"
        title_lines = wrap_block_text(name, 28)[:2] or [name]
        meta_bits = []
        if (activity.jira_key or "").strip():
            meta_bits.append(activity.jira_key.strip())
        meta_bits.append(f"{config.QDM_DROP_MINUTES} min")
        meta = " · ".join(meta_bits)
        title_font = tkfont.Font(family=self.family, size=10, weight="bold")
        meta_font = tkfont.Font(family=self.family, size=8)
        pad = 10
        text_w = max(
            [title_font.measure(line) for line in title_lines] + [meta_font.measure(meta), 120]
        )
        line_h = int(title_font.metrics("linespace"))
        meta_h = int(meta_font.metrics("linespace"))
        self._qdm_ghost = {
            "fill": fill,
            "fg": fg,
            "title_lines": title_lines,
            "meta": meta,
            "title_font": title_font,
            "meta_font": meta_font,
            "pad": pad,
            "line_h": line_h,
            "w": int(text_w + pad * 2),
            "h": int(pad * 2 + line_h * len(title_lines) + 2 + meta_h),
            "ids": [],
            "origin": None,
        }
        if event is not None:
            self._place_qdm_ghost(event.x_root, event.y_root)

    def _raise_qdm_ghost(self):
        ghost = self._qdm_ghost
        if not ghost:
            return
        try:
            self.canvas.tag_raise("qdm_ghost")
        except tk.TclError:
            pass

    def _place_qdm_ghost(self, x_root, y_root):
        ghost = self._qdm_ghost
        if not ghost:
            return
        pos = self._canvas_xy_from_root(x_root, y_root, require_inside=False)
        if pos is None:
            return
        x0 = pos[0] + 16
        y0 = pos[1] + 16
        x1 = x0 + ghost["w"]
        y1 = y0 + ghost["h"]
        last = ghost.get("origin")
        try:
            if not ghost["ids"]:
                ids = theme.place_rounded_rect(
                    self.canvas, x0, y0, x1, y1,
                    radius=config.BLOCK_CORNER_RADIUS,
                    fill=ghost["fill"], outline="",
                    background=theme.GRID_BG, full=True,
                    transparent_outside=True,
                    tags=("qdm_ghost",),
                )
                pad = ghost["pad"]
                ty = y0 + pad
                for line in ghost["title_lines"]:
                    ids.append(self.canvas.create_text(
                        x0 + pad, ty, text=line, anchor="nw",
                        font=ghost["title_font"], fill=ghost["fg"],
                        tags=("qdm_ghost",),
                    ))
                    ty += ghost["line_h"]
                ids.append(self.canvas.create_text(
                    x0 + pad, ty + 2, text=ghost["meta"], anchor="nw",
                    font=ghost["meta_font"], fill=ghost["fg"],
                    tags=("qdm_ghost",),
                ))
                ghost["ids"] = ids
                ghost["origin"] = (x0, y0)
            elif last != (x0, y0):
                self.canvas.move("qdm_ghost", x0 - last[0], y0 - last[1])
                ghost["origin"] = (x0, y0)
            self.canvas.tag_raise("qdm_ghost")
        except tk.TclError:
            pass

    def _destroy_qdm_ghost(self):
        ghost = self._qdm_ghost
        self._qdm_ghost = None
        if not ghost:
            return
        try:
            self.canvas.delete("qdm_ghost")
        except tk.TclError:
            pass
        for item in ghost.get("ids") or []:
            try:
                self.canvas.delete(item)
            except tk.TclError:
                pass

    def _unbind_qdm_drop(self):
        root = self.winfo_toplevel()
        binds = getattr(self, "_qdm_drop_binds", None) or {}
        self._qdm_drop_binds = {}
        for key, seq in (("motion", "<B1-Motion>"), ("release", "<ButtonRelease-1>")):
            funcid = binds.get(key)
            if not funcid:
                continue
            try:
                root.unbind(seq, funcid)
            except tk.TclError:
                pass
        try:
            root.config(cursor="")
        except tk.TclError:
            pass
        self._destroy_qdm_ghost()

    def _canvas_xy_from_root(self, x_root, y_root, *, require_inside=True):
        """Pointer → the inner grid's own coordinates.

        The grid canvas sits inside `_scroll_host` and never scrolls
        itself (see the comment on `_scroll_host`). Host canvasx/canvasy
        is the reliable conversion once that host has been panned;
        winfo_rootx of the nested canvas can report the clipped viewport
        origin instead of the widget's real origin.

        `require_inside=False` still converts when the pointer is over
        the sidebar so the drag card can slide onto the grid.
        """
        try:
            host = self._scroll_host
            hx = host.winfo_rootx()
            hy = host.winfo_rooty()
            hw = host.winfo_width()
            hh = host.winfo_height()
        except (tk.TclError, AttributeError):
            return None
        if require_inside and not (hx <= x_root <= hx + hw and hy <= y_root <= hy + hh):
            return None
        try:
            return host.canvasx(x_root - hx), host.canvasy(y_root - hy)
        except tk.TclError:
            return x_root - hx, y_root - hy

    def _grid_xy(self, event):
        """Pointer position in the inner grid's coordinate space.

        Raw event.x/y on this nested canvas are viewport-relative once
        `_scroll_host` has been panned, so the visible top maps to
        minute 0. After you scroll down to the now-line, every slot
        above it (before the current time) becomes untargetable. QDM
        drop already converted through `_canvas_xy_from_root`; click /
        drag / hover must do the same.

        Synthetic tests copy x→x_root; those stay on event.x/y.
        """
        x_root = getattr(event, "x_root", None)
        y_root = getattr(event, "y_root", None)
        try:
            mapped = bool(self._scroll_host.winfo_ismapped())
        except (tk.TclError, AttributeError):
            mapped = False
        if (
            mapped
            and x_root is not None
            and y_root is not None
            and (x_root != event.x or y_root != event.y)
        ):
            pos = self._canvas_xy_from_root(x_root, y_root, require_inside=False)
            if pos is not None:
                return pos
        try:
            return float(self.canvas.canvasx(event.x)), float(self.canvas.canvasy(event.y))
        except (tk.TclError, AttributeError, TypeError):
            return float(event.x), float(event.y)

    _DRAG_SCROLL_MARGIN_PX = 28
    _DRAG_SCROLL_STEP_PX = 36

    def _autoscroll_for_pointer(self, event):
        """Pan the grid when a drag is held against the viewport edge.

        A create/move cannot enter times that are scrolled off-screen,
        which is often everything before the now-line.
        """
        x_root = getattr(event, "x_root", None)
        y_root = getattr(event, "y_root", None)
        if x_root is None or y_root is None:
            return
        if x_root == event.x and y_root == event.y:
            return
        try:
            host = self._scroll_host
            hy = host.winfo_rooty()
            hh = host.winfo_height()
        except (tk.TclError, AttributeError):
            return
        if hh <= 1:
            return
        margin = self._DRAG_SCROLL_MARGIN_PX
        if y_root < hy + margin:
            self._nudge_scroll_pixels("y", self._DRAG_SCROLL_STEP_PX)
        elif y_root > hy + hh - margin:
            self._nudge_scroll_pixels("y", -self._DRAG_SCROLL_STEP_PX)

    def _on_qdm_drop_motion(self, event):
        state = self._drag_state
        if not state or state.get("mode") != "qdm-drop":
            return
        self._place_qdm_ghost(event.x_root, event.y_root)
        pos = self._canvas_xy_from_root(event.x_root, event.y_root)
        if pos is None:
            self._clear_qdm_slot_preview(state)
            self._raise_qdm_ghost()
            return
        self._update_qdm_drop_preview(pos[0], pos[1], state)
        self._raise_qdm_ghost()

    def _discard_preview_items(self, state):
        ids = list(state.get("_aa_ids") or [])
        if state.get("preview_rect") is not None and state["preview_rect"] not in ids:
            ids.append(state["preview_rect"])
        for item in ids:
            try:
                self.canvas.delete(item)
            except tk.TclError:
                pass
        state["preview_rect"] = None
        state["_aa_ids"] = None
        state["_aa_geom"] = None

    def _clear_qdm_slot_preview(self, state):
        self._discard_preview_items(state)
        state["preview_day_idx"] = None
        state["preview_start"] = None
        state["preview_end"] = None

    def _update_qdm_drop_preview(self, x, y, state):
        """30-minute slot on whichever day the pointer is over right now."""
        day_idx = self._day_idx_for_x(x)
        if day_idx is None or y < self.header_height:
            self._clear_qdm_slot_preview(state)
            return
        start_min = self._snapped_minute_for_y(y)
        end_min = min(self._view_minutes_total(), start_min + config.QDM_DROP_MINUTES)
        if end_min <= start_min:
            start_min = max(0, self._view_minutes_total() - config.QDM_DROP_MINUTES)
            end_min = self._view_minutes_total()
            if end_min <= start_min:
                return

        x0 = self.gutter_width + day_idx * self.day_width + 3
        x1 = x0 + self.day_width - 6
        y0 = self.header_height + start_min * self.px_per_min
        y1 = self.header_height + end_min * self.px_per_min
        fill = theme.calendar_block_fill(state["activity"].color)
        geom = (round(x0), round(y0), round(x1), round(y1), fill)
        if state.get("_aa_geom") != geom:
            self._discard_preview_items(state)
            ids = theme.place_rounded_rect(
                self.canvas, x0, y0, x1, y1, radius=config.BLOCK_CORNER_RADIUS,
                fill=fill, outline="", background=self._day_column_bg(day_idx), full=True,
                transparent_outside=True)
            state["_aa_ids"] = ids
            state["preview_rect"] = ids[-1]
            state["_aa_geom"] = geom
        state["preview_start"] = start_min
        state["preview_end"] = end_min
        state["preview_day_idx"] = day_idx

    def _on_qdm_drop_release(self, event):
        self._unbind_qdm_drop()
        state = self._drag_state
        if not state or state.get("mode") != "qdm-drop":
            return
        self._drag_state = None
        self._discard_preview_items(state)
        try:
            self.canvas.config(cursor="")
        except tk.TclError:
            pass
        if state.get("preview_day_idx") is None or state.get("preview_start") is None:
            self._update_hint()
            return
        self._place_qdm_block(
            state["activity"], state["preview_day_idx"],
            state["preview_start"], state["preview_end"],
        )

    def _place_qdm_block(self, activity, day_idx, start_min, end_min):
        if end_min <= start_min:
            end_min = min(self._view_minutes_total(), start_min + config.QDM_DROP_MINUTES)
        if end_min <= start_min:
            return
        # Always a 30-minute starting block from a sidebar drop. Jira
        # worklogs need a comment, so open Time Block on Description —
        # start/end can be changed there. Cancel leaves no block.
        self._open_entry_dialog(
            new=True, day_idx=day_idx,
            start_hhmm=self._view_minute_to_hhmm(start_min),
            end_hhmm=self._view_minute_to_hhmm(end_min),
            placed_activity=activity,
            require_notes=True,
        )
        self.clear_armed_activity()

    def _finish_entry_drag(self, state):
        entry = state["orig_entry"]
        if not state["moved"]:
            # Plain click on an entry: select it (Delete removes it, arrow
            # keys nudge it -- see MainWindow's key handlers) rather than
            # doing nothing. Right-click still opens the full Edit/
            # Duplicate/Delete menu regardless of selection.
            self.selected_entry_id = state["entry_id"]
            self.refresh()
            return

        day_idx = state.get("preview_day_idx", state["start_day_idx"])
        new_start = state.get("preview_start", state["start_minute"])
        new_end = state.get("preview_end", state["end_minute"])
        new_start_hhmm = self._view_minute_to_hhmm(new_start)
        new_end_hhmm = self._view_minute_to_hhmm(new_end)

        self.selected_entry_id = state["entry_id"]

        unchanged = (day_idx == self._entry_day_idx(entry) and new_start_hhmm == entry.start_time
                     and new_end_hhmm == entry.end_time)
        if unchanged:
            self.refresh()
            return

        before = self._snapshot(entry, self._entry_day_idx(entry))
        if self.template_mode:
            assert isinstance(entry, TemplateEntry)
            entry.day_of_week = day_idx
        else:
            assert isinstance(entry, TimeEntry)
            entry.date = self.day_date(day_idx).isoformat()
        entry.start_time = new_start_hhmm
        entry.end_time = new_end_hhmm
        self._db_update_entry(entry)
        after = self._snapshot(entry, day_idx)
        self._push_undo({"kind": "update", "id": state["entry_id"], "before": before, "after": after})
        self.refresh()

    def _cancel_drag(self):
        # Escape does three jobs depending on what's active, cheapest first:
        # cancel an in-progress drag, un-arm an activity queued from the
        # sidebar, and deselect a keyboard-selected block -- all three are
        # harmless no-ops when they don't apply, and self.refresh() below
        # repaints whichever of them actually did something.
        if self._drag_state:
            self._discard_preview_items(self._drag_state)
        self._unbind_qdm_drop()
        self._drag_state = None
        self.clear_armed_activity()
        self.selected_entry_id = None
        self.refresh()

    # ------------------------------------------------------------------
    # Hover cursor
    # ------------------------------------------------------------------
    def _on_hover(self, event):
        if self._drag_state:
            self._clear_hover_preview()
            return
        x, y = self._grid_xy(event)
        entry_id = self._entry_id_at(x, y)
        if entry_id is not None:
            self._clear_hover_preview()
            if event.state & CONTROL_STATE_MASK:
                # Hints at the Ctrl+click-to-duplicate shortcut.
                self.canvas.config(cursor="plus")
                return
            region = self._hit_region(entry_id, y)
            cursor = "sb_v_double_arrow" if region != "move" else "fleur"
            self.canvas.config(cursor=cursor)
        else:
            armed = self.get_armed_activity()
            self.canvas.config(cursor="hand2" if armed else "")
            if armed:
                self._clear_hover_preview()
            else:
                self._update_hover_preview(x, y)

    def _clear_hover_preview(self):
        if self._hover_slot is None:
            return
        self._hover_slot = None
        try:
            self.canvas.delete("hover_preview")
        except tk.TclError:
            pass

    def _update_hover_preview(self, x: float, y: float):
        if y < self.header_height or x < self.gutter_width:
            self._clear_hover_preview()
            return
        day = self._day_idx_for_x(x)
        if day is None:
            self._clear_hover_preview()
            return
        minute = int((y - self.header_height) / self.px_per_min)
        if minute < 0 or minute >= self._view_minutes_total():
            self._clear_hover_preview()
            return
        slot = (minute // config.SLOT_MINUTES) * config.SLOT_MINUTES
        key = (day, slot)
        if key == self._hover_slot:
            return
        self._hover_slot = key
        self.canvas.delete("hover_preview")
        x0 = self.gutter_width + day * self.day_width + 4
        x1 = x0 + self.day_width - 8
        y0 = self.header_height + slot * self.px_per_min + 1
        y1 = y0 + self.slot_height - 2
        theme.place_rounded_rect(
            self.canvas, x0, y0, x1, y1, radius=min(8, (x1 - x0) / 2, (y1 - y0) / 2),
            fill=theme.ACCENT_SOFT, outline="", background=self._day_column_bg(day),
            tags="hover_preview", full=True, transparent_outside=True)
        if self.canvas.find_withtag("entry"):
            self.canvas.tag_lower("hover_preview", "entry")
        elif self.canvas.find_withtag("today_col"):
            self.canvas.tag_raise("hover_preview", "today_col")

    # ------------------------------------------------------------------
    # Right-click menu
    # ------------------------------------------------------------------
    def _on_right_click(self, event):
        x, y = self._grid_xy(event)
        entry_id = self._entry_id_at(x, y)
        if entry_id is not None:
            entry = self.entries_by_id[entry_id]
            menu = tk.Menu(self, tearoff=0)
            menu.add_command(label="Edit…", command=lambda: self._edit_entry(entry))
            menu.add_command(label="Duplicate…", command=lambda: self._open_duplicate_dialog(entry))
            menu.add_separator()
            menu.add_command(label="Delete", command=lambda: self._delete_entry(entry))
            menu.tk_popup(event.x_root, event.y_root)
            return
        meeting = self._overlay_event_at(x, y)
        if meeting is None:
            return
        self._show_meeting_details(meeting)

    def _show_meeting_details(self, event: CalendarEvent):
        messagebox.showinfo(
            event.title or "Meeting",
            format_meeting_details(event),
            parent=self.winfo_toplevel(),
        )

    def _edit_entry(self, entry: EntryLike):
        self._open_entry_dialog(new=False, existing=entry)

    def _delete_entry(self, entry: EntryLike):
        assert entry.id is not None
        if messagebox.askyesno("Delete time block",
                                f"Delete “{entry.activity_name}” "
                                f"({entry.start_time}–{entry.end_time}) on "
                                f"{self._entry_day_label(entry)}?"):
            deleted_id = entry.id
            fields = self._snapshot(entry, self._entry_day_idx(entry))
            self._db_delete_entry(deleted_id)
            self._push_undo({"kind": "remove", "items": [{"id": deleted_id, "fields": fields}]})
            if self.selected_entry_id == deleted_id:
                self.selected_entry_id = None
            self.refresh()

    def _delete_selected_entry(self, event=None):
        """Delete/Backspace shortcut -- removes whatever block is currently
        selected (see self.selected_entry_id), with the same confirmation
        dialog as the right-click menu's Delete. A no-op when nothing is
        selected, so it's safe to bind unconditionally on the canvas."""
        if self.selected_entry_id is None:
            return
        entry = self.entries_by_id.get(self.selected_entry_id)
        if entry is None:
            self.selected_entry_id = None
            return
        self._delete_entry(entry)

    # ------------------------------------------------------------------
    # Duplicate
    # ------------------------------------------------------------------
    def _duplicate_entry_in_place(self, entry_id: int):
        """Ctrl+click shortcut: duplicate a block into its own exact slot
        (same day, same start/end). It will overlap the original by
        definition -- that's expected, not an error; overlapping blocks
        render side by side (see _layout_day_entries) so both stay visible
        and you can drag either one to a new time afterward."""
        entry = self.entries_by_id.get(entry_id)
        if entry is None:
            return
        day_idx = self._entry_day_idx(entry)
        copy = self._make_entry(
            None, entry.activity_id, entry.activity_name, entry.jira_key,
            entry.color, day_idx, entry.start_time, entry.end_time, entry.notes,
            entry.jira_project, entry.issue_type,
        )
        new_id = self._db_add_entry(copy)
        self._push_undo({"kind": "add", "items": [
            {"id": new_id, "fields": self._snapshot(copy, day_idx)}]})
        self.selected_entry_id = new_id
        self.refresh()

    def _open_duplicate_dialog(self, entry: EntryLike):
        def on_duplicate(target_day_indices):
            items = []
            for day_idx in target_day_indices:
                copy = self._make_entry(
                    None, entry.activity_id, entry.activity_name, entry.jira_key,
                    entry.color, day_idx, entry.start_time, entry.end_time, entry.notes,
                    entry.jira_project, entry.issue_type,
                )
                new_id = self._db_add_entry(copy)
                items.append({"id": new_id, "fields": self._snapshot(copy, day_idx)})
            if items:
                self._push_undo({"kind": "add", "items": items})
                self.selected_entry_id = items[-1]["id"]
            self.refresh()

        source_day_idx = self._entry_day_idx(entry)
        assert self.open_duplicate is not None
        self.open_duplicate(
            source_entry=entry, day_options=self._day_options(),
            source_day_idx=source_day_idx, on_duplicate=on_duplicate,
        )

    # ------------------------------------------------------------------
    # Add/Edit dialog
    # ------------------------------------------------------------------
    def _open_entry_dialog(self, new: bool, day_idx: Optional[int] = None,
                            start_hhmm: Optional[str] = None, end_hhmm: Optional[str] = None,
                            existing: Optional[EntryLike] = None,
                            placed_activity: Optional[Activity] = None,
                            require_notes: bool = False,
                            force_date: Optional[str] = None,
                            on_committed: Optional[Callable] = None,
                            on_cancelled: Optional[Callable] = None):
        activities = self.db.list_activities()
        armed = placed_activity or (self.get_armed_activity() if new else None)

        def on_save(result):
            target_day_idx = result["day_idx"]
            if new:
                entry = self._make_entry(
                    None, result["activity_id"], result["activity_name"], result["jira_key"],
                    result["color"], target_day_idx, result["start_time"], result["end_time"],
                    result["notes"], result["jira_project"], result["issue_type"],
                )
                if force_date and isinstance(entry, TimeEntry):
                    entry.date = force_date
                new_id = self._db_add_entry(entry)
                self._push_undo({"kind": "add", "items": [
                    {"id": new_id, "fields": self._snapshot(entry, target_day_idx)}]})
                # Creating a block is not a selection — the outline is for
                # click/keyboard targets (Delete, arrow nudges), not a
                # "you just saved this" highlight.
                self.selected_entry_id = None
                committed = entry
            else:
                assert existing is not None
                before = self._snapshot(existing, self._entry_day_idx(existing))
                existing.activity_id = result["activity_id"]
                existing.activity_name = result["activity_name"]
                existing.jira_key = result["jira_key"]
                existing.color = result["color"]
                if self.template_mode:
                    assert isinstance(existing, TemplateEntry)
                    existing.day_of_week = target_day_idx
                else:
                    assert isinstance(existing, TimeEntry)
                    existing.date = self.day_date(target_day_idx).isoformat()
                existing.start_time = result["start_time"]
                existing.end_time = result["end_time"]
                existing.notes = result["notes"]
                existing.jira_project = result["jira_project"]
                existing.issue_type = result["issue_type"]
                self._db_update_entry(existing)
                after = self._snapshot(existing, target_day_idx)
                self._push_undo({"kind": "update", "id": existing.id, "before": before, "after": after})
                self.selected_entry_id = existing.id
                committed = existing
            self.refresh()
            if on_committed:
                on_committed(committed)
            return True

        def on_delete():
            assert existing is not None
            self._delete_entry(existing)

        existing_day_idx = self._entry_day_idx(existing) if existing else None

        assert self.open_time_block is not None
        self.open_time_block(
            activities=activities,
            day_options=self._day_options(),
            initial_day_idx=day_idx if day_idx is not None else existing_day_idx,
            initial_start=start_hhmm or (existing.start_time if existing else None),
            initial_end=end_hhmm or (existing.end_time if existing else None),
            initial_activity_id=(armed.id if armed else (existing.activity_id if existing else None)),
            initial_notes=(existing.notes if existing else ""),
            initial_jira_project=((armed.jira_project if armed else
                                    (existing.jira_project if existing else None)) or ""),
            on_save=on_save,
            on_delete=on_delete if existing else None,
            start_hour=self._view_start_hour, end_hour=self._view_end_hour,
            slot_minutes=config.SLOT_MINUTES,
            is_new=new,
            require_notes=require_notes,
            on_cancel=on_cancelled,
        )
