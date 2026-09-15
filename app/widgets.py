"""
Shared "modern look" building blocks used across the app: a hand-drawn
rounded button, a hand-drawn rounded-corner card, and a scrollable card
that combines a rounded card with the app's own hand-drawn scrollbar.

Tkinter/ttk has no first-class support for rounded corners -- there's no
border-radius style option, and ttk's "clam" theme engine draws buttons and
frame borders as hard rectangles no matter what padding/relief is
configured. This app already works around a very similar toolkit
limitation for its scrollbar (ttk.Scrollbar's thumb/track doesn't reliably
paint in this app's target environments -- see VectorScrollbar below) by
drawing it by hand on a Canvas instead of fighting the toolkit. Buttons and
card-style containers get the same treatment here, so every "box" in the
app -- buttons, cards, calendar blocks -- shares one consistent rounded
look instead of only the calendar blocks (see calendar_view._draw_entry)
looking modern while buttons and panels stayed sharp-cornered.
"""
import os
import math
import tkinter as tk
import tkinter.font as tkfont
from typing import Callable, List, Optional

from . import theme

# Corner radius (px) for hand-drawn buttons and cards -- deliberately
# generous; rounded_rect() clamps it to half of whatever width/height a
# particular button or card actually ends up with, so small elements (e.g.
# the "‹"/"›" week-nav buttons) automatically become pill-shaped instead of
# needing a separate smaller constant.
BUTTON_RADIUS = 12
CARD_RADIUS = 14
_BUTTON_ICON_SIZE = 14
_BUTTON_ICON_GAP = 6


def _jira_tile_points(x0: float, y0: float, m: float):
    """Square with a quarter-circle bitten from the SW corner (Jira tile)."""
    br = m * 0.62
    pts = [x0, y0, x0 + m, y0, x0 + m, y0 + m, x0 + br, y0 + m]
    for i in range(9):
        a = math.radians(0.0 + 90.0 * i / 8.0)
        pts.extend((x0 + br * math.cos(a), y0 + m - br * math.sin(a)))
    pts.extend((x0, y0 + m - br, x0, y0))
    return pts

# See sidebar.py's original copy of this flag (now here) -- set
# FREE_TIMESHEET_DEBUG_WHEEL=1 in the environment to print every wheel-ish
# event any ScrollArea's global dispatcher sees, and whether its geometric
# hit-test thought the pointer was over that particular scrollable area.
_DEBUG_WHEEL = os.environ.get("FREE_TIMESHEET_DEBUG_WHEEL") == "1"


def _color(spec: str) -> str:
    """Resolve a style-table color entry: a literal "#RRGGBB" is returned
    as-is, anything else is looked up as a live theme.* attribute name (so
    button colors always reflect whichever theme is currently active)."""
    if spec.startswith("#"):
        return spec
    return getattr(theme, spec)


def _darken(hex_color: str, amount: float) -> str:
    """Blend `hex_color` toward black by `amount` (0-1). Used for the
    optional drop shadow on RoundedButton: the shadow is drawn as a
    darkened echo of whatever surface sits behind the button (its own
    canvas bg, already resolved to the parent's color -- see
    _parent_bg), rather than a fixed gray that might clash with the
    current theme."""
    hex_color = hex_color.lstrip("#")
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    r = max(0, min(255, int(r * (1 - amount))))
    g = max(0, min(255, int(g * (1 - amount))))
    b = max(0, min(255, int(b * (1 - amount))))
    return f"#{r:02x}{g:02x}{b:02x}"


def _parent_bg(widget) -> str:
    """Best-effort guess at a widget's background color, used so a
    hand-drawn button/card's own canvas background matches whatever it
    sits on (its four corners, cut off by the rounded shape, need to blend
    into the parent rather than showing a mismatched square halo).

    Plain tk widgets (Frame/Label/Canvas) expose a real "bg"/"background"
    option we can read directly. ttk widgets (ttk.Frame and friends, used
    for most button rows in this app) don't -- they're styled, not
    configured, so cget("bg") raises TclError. Every ttk.Frame in this app
    uses the default "TFrame" style, which theme.apply_theme() always
    configures to PANEL_BG, so that's the safe fallback.
    """
    try:
        bg = widget.cget("bg")
    except tk.TclError:
        return theme.PANEL_BG
    # Some ttk widgets/Tk builds don't raise TclError for an option they
    # don't really support -- they just hand back an empty string instead.
    # Aqua also returns named system colors ("systemWindowBody") that
    # PhotoImage blending can't parse. An unusable bg passed to
    # rounded_rect_image is the "mismatched square halo" this function
    # exists to prevent.
    if not bg or not str(bg).startswith("#"):
        return theme.PANEL_BG
    return bg


def _hex_bg_at(root, abs_x: int, abs_y: int, fallback: str, ignore=None) -> str:
    """Hex background of the topmost widget under a screen coordinate.

    Floating menus are parented to the toplevel so they can hang off their
    field; `_parent_bg(root)` is then APP_BG (black or white), which is
    almost never the color actually sitting behind the menu. Walk
    `winfo_containing` instead so rounded-card corners blend into the
    timer bar, calendar, sidebar, or whatever else the popup covers.

    `ignore` is a widget to see through (the floating card itself). Tk
    cannot hit-test underneath it, so those samples fall back rather than
    blending the cut-off corners into the card's own fill.
    """
    try:
        widget = root.winfo_containing(int(abs_x), int(abs_y))
    except tk.TclError:
        widget = None
    if ignore is not None:
        w = widget
        while w is not None:
            if w is ignore:
                widget = None
                break
            w = getattr(w, "master", None)
    while widget is not None:
        try:
            own = widget.cget("bg")
        except tk.TclError:
            own = ""
        if own and str(own).startswith("#"):
            return own
        try:
            parent = widget.nametowidget(widget.winfo_parent())
        except (tk.TclError, KeyError):
            break
        if parent is widget:
            break
        widget = parent
    if fallback and str(fallback).startswith("#"):
        return fallback
    return theme.APP_BG


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------
_BUTTON_STYLES = {
    # Padding matches theme.apply_theme()'s old ttk button padding exactly
    # (14,8 / 10,6 / 10,6 / 8,5) -- not just for visual parity, but because
    # several smoke tests (and the sidebar/calendar minimum-width layout
    # itself) depend on the real pixel sizes these buttons occupy. Rounder
    # corners are a drawing change, not a "make everything bigger" change.
    #
    # Secondary/Nav both idle at "SURFACE" rather than "APP_BG": APP_BG is
    # this app's own darkest/outermost layer, which in every dark theme is
    # *darker* than the PANEL_BG these buttons actually sit on -- so an
    # APP_BG-filled button read as a dim notch sunk into its panel rather
    # than a button, exactly the "hard to read" overlapping-element problem.
    # SURFACE is a dedicated token that's deliberately a step *lighter* than
    # PANEL_BG in every dark theme (matching how BORDER/BORDER_STRONG below
    # already correctly lighten further still on hover/press), while
    # keeping the original APP_BG look for light themes, where a slightly
    # darker idle fill against a white/cream panel already reads fine. See
    # theme.py's THEMES dict for where SURFACE is defined per theme.
    "Accent.TButton": dict(bg="ACCENT", hover="ACCENT_HOVER", press="ACCENT_HOVER", fg="#FFFFFF",
                            bold=True, pad=(14, 8), border=None),
    "Ghost.TButton": dict(bg="APP_BG", hover="SURFACE", press="BORDER", fg="TEXT_PRIMARY",
                           bold=False, pad=(12, 8), border="BORDER_STRONG"),
    "Secondary.TButton": dict(bg="SURFACE", hover="BORDER", press="BORDER_STRONG", fg="TEXT_PRIMARY",
                               bold=False, pad=(10, 6), border=None),
    "Danger.TButton": dict(bg="DANGER_SOFT", hover="DANGER_SOFT_ACTIVE", press="DANGER_SOFT_ACTIVE",
                            fg="DANGER", bold=False, pad=(10, 6), border=None),
    "Nav.TButton": dict(bg="SURFACE", hover="BORDER", press="BORDER_STRONG", fg="TEXT_PRIMARY",
                         bold=False, pad=(8, 5), border=None),
}


def draw_button_icon(canvas, kind: str, cx: float, cy: float, size: float, fill: str,
                     background: str = ""):
    """Tiny document / Jira / play marks for header CTAs.

    Canvas primitives rather than a symbol font or a PNG, same reasoning
    as theme.draw_logo_mark: they have to render on every Tk we ship.
    """
    kind = (kind or "").lower().strip()
    if kind == "csv":
        w, h = size * 0.70, size * 0.92
        x0, y0 = cx - w / 2, cy - h / 2
        x1, y1 = cx + w / 2, cy + h / 2
        fold = size * 0.28
        canvas.create_line(
            x0, y0 + 1, x1 - fold, y0 + 1, x1 - 0.5, y0 + fold,
            x1 - 0.5, y1 - 1, x0, y1 - 1, x0, y0 + 1,
            fill=fill, width=1.5, capstyle="round", joinstyle="round")
        canvas.create_line(
            x1 - fold, y0 + 1, x1 - fold, y0 + fold, x1 - 0.5, y0 + fold,
            fill=fill, width=1.5, capstyle="round", joinstyle="round")
        for i in range(3):
            yy = y0 + fold + 2.5 + i * 3.0
            if yy < y1 - 2.5:
                canvas.create_line(
                    x0 + 2.5, yy, x1 - 2.5, yy,
                    fill=fill, width=1.2, capstyle="round")
        return
    if kind == "play":
        s = size * 0.42
        canvas.create_polygon(
            cx - s * 0.50, cy - s * 0.85,
            cx - s * 0.50, cy + s * 0.85,
            cx + s * 0.90, cy,
            fill=fill, outline="", joinstyle="round")
        return
    if kind == "stop":
        s = size * 0.32
        canvas.create_rectangle(cx - s, cy - s, cx + s, cy + s, fill=fill, outline="")
        return
    if kind in ("jira", "atlassian"):
        # Official Jira mark: three hooked tiles cascading up-right.
        # Each tile is a square with a quarter-circle cut from the inner
        # corner, drawn as one polygon so later tiles don't punch holes
        # through earlier ones (that was the three black dots).
        color = "#2684FF"
        m = size * 0.58
        step = size * 0.30
        x_mid = cx - m / 2.0
        y_mid = cy - m / 2.0
        for x0, y0 in (
            (x_mid - step, y_mid + step),
            (x_mid, y_mid),
            (x_mid + step, y_mid - step),
        ):
            canvas.create_polygon(
                *_jira_tile_points(x0, y0, m),
                fill=color, outline="", joinstyle="round", smooth=False)
        return


class RoundedButton(tk.Canvas):
    """Hand-drawn, pill/rounded-corner replacement for ttk.Button.

    Matches this app's four style names (Accent/Secondary/Danger/Nav --
    same colors and padding theme.apply_theme() configures for the ttk
    versions, just drawn with rounded corners) and the small slice of the
    ttk.Button API this app actually calls: text=/command=/style=/width= at
    construction time, plus .config(text=...) and .config(style=...)
    afterwards (used by the Timer bar's Start/Stop toggle and the Summary
    tab's Week/Month toggle) and .cget("text") (used by a couple of smoke
    tests) -- enough to be a mechanical drop-in at every call site.
    """

    def __init__(self, master, text: str = "", command: Optional[Callable[[], None]] = None,
                 style: str = "Secondary.TButton", width: Optional[int] = None,
                 shadow: bool = False, compact: bool = False, icon: Optional[str] = None, **kwargs):
        bg = kwargs.pop("bg", None) or _parent_bg(master)
        kwargs.setdefault("highlightthickness", 0)
        kwargs.setdefault("cursor", "hand2")
        super().__init__(master, bg=bg, **kwargs)
        self._command = command
        self._text = text
        self._style = style
        self._char_width = width
        self._hover = False
        self._pressed = False
        self._shadow = shadow
        self._compact = compact
        self._icon = icon

        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

        self._resize_to_content()

    # -- ttk.Button-compatible surface --------------------------------
    def cget(self, key):
        if key == "text":
            return self._text
        if key == "style":
            return self._style
        return super().cget(key)

    def __getitem__(self, key):
        return self.cget(key)

    def config(self, **kwargs):  # type: ignore[override]
        self.configure(**kwargs)

    def configure(self, **kwargs):  # type: ignore[override]
        resize = False
        if "text" in kwargs:
            self._text = kwargs.pop("text")
            resize = True
        if "style" in kwargs:
            self._style = kwargs.pop("style")
            resize = True
        if "icon" in kwargs:
            self._icon = kwargs.pop("icon")
            resize = True
        if "command" in kwargs:
            self._command = kwargs.pop("command")
        if "width" in kwargs:
            # A plain int here means ttk-style "character width" (this
            # app's only usage, e.g. width=3 for the "‹"/"›" nav buttons)
            # rather than a pixel Canvas width -- handled by
            # _resize_to_content(), not passed through to Canvas.configure.
            self._char_width = kwargs.pop("width")
            resize = True
        if kwargs:
            super().configure(**kwargs)
        if resize:
            self._resize_to_content()
        else:
            self._redraw()

    # -- sizing ---------------------------------------------------------
    def _font(self):
        spec = _BUTTON_STYLES[self._style]
        family = theme.resolve_font_family()
        weight = "bold" if spec["bold"] else "normal"
        return tkfont.Font(family=family, size=10, weight=weight)

    def _resize_to_content(self):
        spec = _BUTTON_STYLES[self._style]
        f = self._font()
        pad_x, pad_y = spec["pad"]
        text_w = f.measure(self._text)
        if self._char_width:
            text_w = max(text_w, self._char_width * f.measure("0"))
        height = f.metrics("linespace") + 2 * pad_y
        glow = self._glow_pad()
        if self._compact:
            # Square hit target drawn as a circle — the old width=3 +
            # drop-shadow left a rectangular canvas peeking around +/−.
            side = max(int(height), 30)
            super().configure(width=side, height=side)
            self._redraw()
            return
        width = max(text_w + 2 * pad_x + self._icon_span(), 2 * pad_x + 4)
        super().configure(width=int(width) + 2 * glow, height=int(height) + 2 * glow)
        self._redraw()

    def _icon_span(self) -> int:
        if not self._icon:
            return 0
        return _BUTTON_ICON_SIZE + _BUTTON_ICON_GAP

    def _glow_pad(self) -> int:
        if self._compact or self._style != "Accent.TButton":
            return 0
        if not getattr(theme, "accent_is_luminous", lambda: False)():
            return 0
        return int(getattr(theme, "ACCENT_GLOW_PAD", 6))

    # -- drawing ----------------------------------------------------------
    def _redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:
            return
        spec = _BUTTON_STYLES[self._style]
        parent_bg = self.cget("bg") or theme.APP_BG
        if self._compact:
            # Idle circle matches the parent so +/− / ‹ › read as glyphs,
            # not as a square plate with a round button drawn inside.
            fill = _color(spec["press"] if self._pressed else spec["hover"] if self._hover else spec["bg"])
            if not self._hover and not self._pressed:
                fill = parent_bg
            outline = ""
        else:
            fill = _color(spec["press"] if self._pressed else spec["hover"] if self._hover else spec["bg"])
            outline = _color(spec["border"]) if spec["border"] else ""
        fg = _color(spec["fg"])
        radius = min(w, h) / 2.0 if self._compact else BUTTON_RADIUS
        luminous = (self._style == "Accent.TButton" and not self._compact
                    and getattr(theme, "accent_is_luminous", lambda: False)())
        fill_end = ""
        if luminous:
            end = theme.ACCENT_B
            fill_end = theme._darken(end, 0.08) if (self._hover or self._pressed) else end
        glow_pad = self._glow_pad()
        try:
            if glow_pad:
                inner_w = max(1, w - 2 * glow_pad)
                inner_h = max(1, h - 2 * glow_pad)
                pill_r = min(inner_w, inner_h) / 2.0
                self._photo = theme.rounded_rect_image(
                    w, h, pill_r, fill, parent_bg,
                    outline=outline, outline_width=1 if outline else 0,
                    fill_end=fill_end, glow=glow_pad)
                self.create_image(0, 0, image=self._photo, anchor="nw")
            elif self._shadow and not self._compact:
                off = 2
                inner_w = max(1, w - off)
                inner_h = max(1, h - off)
                shadow_color = _darken(parent_bg, 0.35)
                self._shadow_photo = theme.rounded_rect_image(
                    inner_w, inner_h, radius, shadow_color, parent_bg,
                    outline="", outline_width=0)
                self.create_image(off, off, image=self._shadow_photo, anchor="nw")
                self._photo = theme.rounded_rect_image(
                    inner_w, inner_h, radius, fill, parent_bg,
                    outline=outline, outline_width=1 if outline else 0,
                    fill_end=fill_end)
                self.create_image(0, 0, image=self._photo, anchor="nw")
            else:
                self._photo = theme.rounded_rect_image(
                    w, h, radius, fill, parent_bg,
                    outline=outline, outline_width=1 if outline else 0,
                    fill_end=fill_end)
                self.create_image(0, 0, image=self._photo, anchor="nw")
        except tk.TclError:
            theme.rounded_rect(
                self, 0.5, 0.5, w - 0.5, h - 0.5, radius=radius,
                fill=fill, outline=outline, width=1 if outline else 0)
        f = self._font()
        icon_span = self._icon_span()
        text_x = w / 2.0 + icon_span / 2.0
        if self._icon:
            text_w = f.measure(self._text)
            icon_cx = text_x - text_w / 2.0 - _BUTTON_ICON_GAP - _BUTTON_ICON_SIZE / 2.0
            draw_button_icon(
                self, self._icon, icon_cx, h / 2.0, _BUTTON_ICON_SIZE, fg,
                background=parent_bg)
        self.create_text(text_x, h / 2, text=self._text, fill=fg, font=f, anchor="center")

    # -- interaction ------------------------------------------------------
    def _on_enter(self, _event=None):
        self._hover = True
        self._redraw()

    def _on_leave(self, _event=None):
        self._hover = False
        self._pressed = False
        self._redraw()

    def _on_press(self, _event=None):
        self._pressed = True
        self._redraw()

    def _on_release(self, event=None):
        was_pressed = self._pressed
        self._pressed = False
        self._redraw()
        if was_pressed and event is not None and self._command is not None:
            if 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
                self._command()

    def invoke(self):
        """Matches ttk.Button.invoke() -- fires the command directly,
        without needing a synthetic click. Used by nothing in this app
        today, kept for API parity / future tests."""
        if self._command is not None:
            self._command()


# ---------------------------------------------------------------------------
# Combobox (pill field + popup list)
# ---------------------------------------------------------------------------
class RoundedCombobox(tk.Frame):
    """Readonly dropdown drawn as a pill, matching RoundedButton.

    ttk.Combobox is a hard rectangle on Aqua/clam no matter how the style
    is configured -- that's the box around the Timer's QDM picker. This
    keeps the slice of the Combobox API this app actually uses
    (textvariable, values, state, set/get/current/config/cget/bind, and
    <<ComboboxSelected>>). `filterable=True` adds a search field on the
    open list so a long QDM list can be narrowed by name or ticket number.
    """

    def __init__(self, master, textvariable=None, values=(), state="readonly",
                 width=20, style: str = "", filterable: bool = False,
                 filter_haystacks=(), value_labels=(), **kwargs):
        kwargs.pop("style", None)
        bg = kwargs.pop("bg", None) or _parent_bg(master)
        kwargs.setdefault("highlightthickness", 0)
        super().__init__(master, bg=bg, **kwargs)
        self._parent_bg = bg
        self._values = list(values)
        self._filter_haystacks = list(filter_haystacks or ())
        self._value_labels = list(value_labels or ())
        self._filterable = bool(filterable)
        self._visible_indices: List[int] = list(range(len(self._values)))
        self._filter_var = tk.StringVar()
        self._filter_entry: Optional[tk.Entry] = None
        self._filter_trace = self._filter_var.trace_add(
            "write", lambda *_: self._refilter_popup())
        self._state = state or "readonly"
        self._char_width = int(width) if width else 20
        self._variable = textvariable if textvariable is not None else tk.StringVar()
        self._style_name = style or ""
        self._popup: Optional[tk.Toplevel] = None
        self._popup_canvas: Optional[tk.Canvas] = None
        self._popup_row_h = 28
        self._popup_active = -1
        self._listbox = None
        self._hover = False
        self._photo = None
        self._trace = self._variable.trace_add("write", lambda *_: self._redraw())

        big = self._style_name == "Big.TCombobox"
        family = theme.resolve_font_family()
        self._tkfont = tkfont.Font(family=family, size=12 if big else 10)
        pad_y = 10 if big else 7
        self._height = int(self._tkfont.metrics("linespace") + 2 * pad_y)
        self._min_width = int(self._char_width * self._tkfont.measure("0") + 40)

        self._canvas = tk.Canvas(self, bg=bg, highlightthickness=0,
                                  width=self._min_width, height=self._height)
        self._canvas.pack(fill="both", expand=True)
        super().configure(width=self._min_width, height=self._height, takefocus=1)

        self._canvas.bind("<Configure>", lambda e: self._redraw())
        self._canvas.bind("<Enter>", self._on_enter)
        self._canvas.bind("<Leave>", self._on_leave)
        self._canvas.bind("<Button-1>", self._on_click)
        self.bind("<Button-1>", self._on_click)
        self.bind("<Down>", lambda e: self._open_popup() or "break")
        self.bind("<space>", lambda e: self._open_popup() or "break")
        self.bind("<Escape>", lambda e: self._close_popup() or "break")
        self.bind("<Key>", self._on_typeahead)
        self._canvas.bind("<Key>", self._on_typeahead)
        self.bind("<Destroy>", lambda e: self._close_popup())
        # Close the popup on outside clicks without unbind_all, which
        # would strip unrelated Button-1 handlers across the app.
        self.bind_all("<Button-1>", self._on_any_click, add="+")
        self.bind_all("<MouseWheel>", self._on_popup_wheel, add="+")
        self.bind_all("<Button-4>", self._on_popup_wheel, add="+")
        self.bind_all("<Button-5>", self._on_popup_wheel, add="+")
        try:
            self.bind_all("<TouchpadScroll>", self._on_popup_touchpad, add="+")
        except tk.TclError:
            pass

        self._redraw()

    def cget(self, key):
        if key == "values":
            return tuple(self._values)
        if key == "state":
            return self._state
        if key == "textvariable":
            return self._variable
        return super().cget(key)

    def __getitem__(self, key):
        return self.cget(key)

    def config(self, **kwargs):  # type: ignore[override]
        self.configure(**kwargs)

    def configure(self, **kwargs):  # type: ignore[override]
        if "values" in kwargs:
            self._values = list(kwargs.pop("values") or ())
            self._visible_indices = list(range(len(self._values)))
        if "filter_haystacks" in kwargs:
            self._filter_haystacks = list(kwargs.pop("filter_haystacks") or ())
        if "value_labels" in kwargs:
            self._value_labels = list(kwargs.pop("value_labels") or ())
        if "filterable" in kwargs:
            self._filterable = bool(kwargs.pop("filterable"))
        if "state" in kwargs:
            self._state = kwargs.pop("state") or "readonly"
        if "textvariable" in kwargs:
            if self._trace is not None:
                try:
                    self._variable.trace_remove("write", self._trace)
                except tk.TclError:
                    pass
            self._variable = kwargs.pop("textvariable")
            self._trace = self._variable.trace_add("write", lambda *_: self._redraw())
        if "width" in kwargs:
            self._char_width = int(kwargs.pop("width"))
            self._min_width = int(self._char_width * self._tkfont.measure("0") + 40)
            self._canvas.configure(width=self._min_width)
            kwargs["width"] = self._min_width
        if kwargs:
            super().configure(**kwargs)
        self._redraw()

    def set(self, value):
        self._variable.set(value if value is not None else "")

    def get(self):
        return self._variable.get()

    def current(self, index=None):
        if index is None:
            try:
                return self._values.index(self._variable.get())
            except ValueError:
                return -1
        if 0 <= int(index) < len(self._values):
            self._variable.set(self._values[int(index)])
            return int(index)
        return -1

    def _on_enter(self, _event=None):
        if self._state == "disabled":
            return
        self._hover = True
        self._redraw()

    def _on_leave(self, _event=None):
        self._hover = False
        self._redraw()

    def _on_click(self, _event=None):
        if self._state == "disabled":
            return "break"
        self.focus_set()
        if self._popup is not None:
            self._close_popup()
        else:
            self._open_popup()
        return "break"

    def _fill_fg(self):
        disabled = self._state == "disabled"
        fill = theme.SURFACE if disabled else (theme.BORDER if self._hover else theme.FIELD_BG)
        fg = theme.TEXT_MUTED if disabled else theme.TEXT_PRIMARY
        return fill, fg

    def _matching_indices(self, query: str) -> List[int]:
        """Indices into `_values` whose label/haystack contains `query`."""
        q = (query or "").strip().lower()
        if not q:
            return list(range(len(self._values)))
        digits = "".join(c for c in q if c.isdigit())
        out: List[int] = []
        for i, value in enumerate(self._values):
            hay = self._filter_haystacks[i] if i < len(self._filter_haystacks) else ""
            label = self._row_text(i)
            blob = f"{value}\n{label}\n{hay}".lower()
            if q in blob or (digits and digits in blob):
                out.append(i)
        return out

    def _row_text(self, index: int) -> str:
        if 0 <= index < len(self._value_labels) and str(self._value_labels[index] or "").strip():
            return str(self._value_labels[index])
        if 0 <= index < len(self._values):
            return str(self._values[index])
        return ""

    def _on_typeahead(self, event):
        if not self._filterable or self._state == "disabled":
            return
        if event.state & 0x4:  # Control
            return
        if event.keysym in (
            "Up", "Down", "Return", "Escape", "Tab", "Shift_L", "Shift_R",
            "Control_L", "Control_R", "Alt_L", "Meta_L", "Caps_Lock",
            "Left", "Right", "Home", "End",
        ):
            return
        if self._popup is not None:
            return
        ch = event.char or ""
        if event.keysym == "BackSpace":
            seed = ""
        elif len(ch) == 1 and ch.isprintable():
            seed = ch
        else:
            return
        self._open_popup()
        self._filter_var.set(seed)
        entry = self._filter_entry
        if entry is not None:
            try:
                entry.icursor("end")
            except tk.TclError:
                pass
        return "break"

    def _redraw(self):
        c = self._canvas
        try:
            w, h = c.winfo_width(), c.winfo_height()
        except tk.TclError:
            return
        if w <= 1 or h <= 1:
            w, h = self._min_width, self._height
        c.delete("all")
        fill, fg = self._fill_fg()
        radius = min(w, h) / 2.0
        try:
            self._photo = theme.rounded_rect_image(
                w, h, radius, fill, self._parent_bg, outline="", outline_width=0)
            c.create_image(0, 0, image=self._photo, anchor="nw")
        except tk.TclError:
            theme.rounded_rect(c, 0.5, 0.5, w - 0.5, h - 0.5, radius=radius,
                               fill=fill, outline="")
        text = self._variable.get()
        c.create_text(14, h / 2, text=text, fill=fg, font=self._tkfont, anchor="w")
        cx, cy = w - 16, h / 2
        c.create_line(cx - 4, cy - 2, cx, cy + 2, cx + 4, cy - 2,
                      fill=fg, width=1.5, capstyle="round", joinstyle="round")

    def _open_popup(self):
        if self._state == "disabled" or self._popup is not None:
            return
        self.update_idletasks()
        root = self.winfo_toplevel()

        # Size to the longest QDM name, then hang the menu off the field's
        # *right* edge so a picker in the top-right toolbar grows left
        # into the window instead of clipping to the remaining 8px of
        # margin (which is why names were ending in "…").
        label_iter = (self._row_text(i) for i in range(len(self._values)))
        text_w = max((self._tkfont.measure(str(v)) for v in label_iter), default=0) + 36
        field_w = max(int(self.winfo_width()), self._min_width)
        try:
            max_w = max(220, int(root.winfo_width()) - 24)
        except tk.TclError:
            max_w = 560
        width = min(max(field_w, text_w), max_w)

        rows_vis = min(max(len(self._values), 1), 10)
        row_h = int(self._tkfont.metrics("linespace") + 12)
        self._popup_row_h = row_h
        filter_h = 34 if self._filterable else 0
        height = filter_h + rows_vis * row_h + 8

        radius = 8
        inset = radius
        outer_w = int(width) + 2 * inset
        outer_h = int(height) + 2 * inset

        rx = root.winfo_rootx()
        ry = root.winfo_rooty()
        field_right = self.winfo_rootx() + self.winfo_width() - rx
        field_bottom = self.winfo_rooty() + self.winfo_height() - ry
        local_x = int(field_right - outer_w)
        local_y = int(field_bottom + 4)
        try:
            win_w, win_h = int(root.winfo_width()), int(root.winfo_height())
        except tk.TclError:
            win_w, win_h = 800, 600
        local_x = max(8, min(local_x, max(8, win_w - outer_w - 8)))
        if local_x + outer_w > win_w - 8:
            outer_w = max(160, win_w - local_x - 8)
            width = max(80, outer_w - 2 * inset)
        if local_y + outer_h > win_h - 8:
            up = int(self.winfo_rooty() - ry - outer_h - 6)
            local_y = up if up >= 8 else max(8, win_h - outer_h - 8)

        fallback = self._parent_bg if str(self._parent_bg).startswith("#") else theme.APP_BG
        try:
            sx = int(root.winfo_rootx() + local_x)
            sy = int(root.winfo_rooty() + local_y)
        except tk.TclError:
            sx, sy = 0, 0
        corner_bgs = {
            "nw": _hex_bg_at(root, sx, sy, fallback),
            "ne": _hex_bg_at(root, sx + outer_w - 1, sy, fallback),
            "sw": _hex_bg_at(root, sx, sy + outer_h - 1, fallback),
            "se": _hex_bg_at(root, sx + outer_w - 1, sy + outer_h - 1, fallback),
        }
        card = RoundedCard(
            root, bg=theme.FIELD_BG, radius=radius, outline=False, pad=inset,
            outer_bg=corner_bgs["nw"], corner_bgs=corner_bgs)
        card.place(x=local_x, y=local_y, width=outer_w, height=outer_h)
        card.lift()

        self._filter_entry = None
        self._filter_var.set("")
        if self._filterable:
            filter_wrap = tk.Frame(card.body, bg=theme.FIELD_BG)
            filter_wrap.pack(side="top", fill="x", padx=2, pady=(0, 4))
            entry = tk.Entry(
                filter_wrap, textvariable=self._filter_var,
                font=self._tkfont, relief="flat", bd=0,
                bg=theme.SURFACE, fg=theme.TEXT_PRIMARY,
                insertbackground=theme.TEXT_PRIMARY, highlightthickness=1,
                highlightbackground=theme.BORDER, highlightcolor=theme.ACCENT,
            )
            entry.pack(fill="x", ipady=3, padx=2)
            entry.bind("<Down>", lambda e: self._move_popup_active(1))
            entry.bind("<Up>", lambda e: self._move_popup_active(-1))
            entry.bind("<Return>", self._on_pick)
            entry.bind("<Escape>", lambda e: self._close_popup() or "break")
            self._filter_entry = entry

        list_h = max(row_h, height - filter_h)
        canvas = tk.Canvas(
            card.body, width=width, height=list_h, bg=theme.FIELD_BG,
            highlightthickness=0, borderwidth=0,
        )
        self._visible_indices = self._matching_indices(self._filter_var.get())
        inner_h = max(len(self._visible_indices), 1) * row_h + 8
        canvas.configure(
            scrollregion=(0, 0, width, inner_h),
            yscrollincrement=row_h,
        )
        if len(self._values) > 10:
            sb = VectorScrollbar(card.body, command=canvas.yview, bg=theme.FIELD_BG)
            canvas.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self._popup = card
        self._popup_canvas = canvas
        self._popup_width = width
        current = self.current()
        self._popup_active = (
            self._visible_indices.index(current) if current in self._visible_indices else 0
        )

        canvas.bind("<Motion>", self._on_popup_motion)
        canvas.bind("<ButtonRelease-1>", self._on_pick)
        canvas.bind("<Return>", self._on_pick)
        canvas.bind("<Escape>", lambda e: self._close_popup())
        canvas.bind("<Down>", lambda e: self._move_popup_active(1))
        canvas.bind("<Up>", lambda e: self._move_popup_active(-1))
        card.bind("<Escape>", lambda e: self._close_popup())

        self._redraw_popup_rows()
        try:
            if self._filter_entry is not None:
                self._filter_entry.focus_set()
            else:
                canvas.focus_set()
        except tk.TclError:
            pass

    def _popup_inner_height(self) -> int:
        return max(len(self._visible_indices), 1) * self._popup_row_h + 8

    def _refilter_popup(self):
        if self._popup is None or self._popup_canvas is None:
            return
        self._visible_indices = self._matching_indices(self._filter_var.get())
        if self._visible_indices:
            self._popup_active = min(max(self._popup_active, 0), len(self._visible_indices) - 1)
        else:
            self._popup_active = -1
        c = self._popup_canvas
        inner_h = self._popup_inner_height()
        w = int(getattr(self, "_popup_width", 0) or 200)
        try:
            c.configure(scrollregion=(0, 0, w, inner_h))
            c.yview_moveto(0)
        except tk.TclError:
            pass
        self._redraw_popup_rows()

    def _move_popup_active(self, delta: int):
        if not self._visible_indices:
            return "break"
        nxt = self._popup_active + delta
        if self._popup_active < 0:
            nxt = 0 if delta > 0 else len(self._visible_indices) - 1
        self._popup_active = max(0, min(len(self._visible_indices) - 1, nxt))
        self._redraw_popup_rows()
        c = self._popup_canvas
        if c is not None:
            try:
                row_top = 4 + self._popup_active * self._popup_row_h
                view_top = c.canvasy(0)
                view_h = int(c.winfo_height() or 1)
                row_bot = row_top + self._popup_row_h
                if row_top < view_top:
                    c.yview_moveto(max(0.0, row_top / self._popup_inner_height()))
                elif row_bot > view_top + view_h:
                    c.yview_moveto(max(0.0, (row_bot - view_h) / self._popup_inner_height()))
            except tk.TclError:
                pass
        return "break"

    def _fit_popup_text(self, text: str, max_px: int) -> str:
        if self._tkfont.measure(text) <= max_px:
            return text
        ell = "…"
        while text and self._tkfont.measure(text + ell) > max_px:
            text = text[:-1]
        return (text + ell) if text else ell

    def _redraw_popup_rows(self):
        c = self._popup_canvas
        if c is None:
            return
        c.delete("row")
        w = int(getattr(self, "_popup_width", 0) or c.winfo_width() or 200)
        max_text = max(40, w - 20)
        current = self.current()
        if not self._visible_indices:
            c.create_text(
                12, 4 + self._popup_row_h / 2, text="No matching QDMs",
                fill=theme.TEXT_MUTED, font=self._tkfont, anchor="w", tags="row")
            return
        for vis_i, val_i in enumerate(self._visible_indices):
            y0 = 4 + vis_i * self._popup_row_h
            y1 = y0 + self._popup_row_h
            if vis_i == self._popup_active:
                fill = theme.ACCENT
                fg = "#FFFFFF"
            elif val_i == current:
                fill = theme.ACCENT_SOFT
                fg = theme.ACCENT
            else:
                fill = ""
                fg = theme.TEXT_PRIMARY
            if fill:
                theme.place_rounded_rect(
                    c, 6, y0 + 2, w - 6, y1 - 2,
                    radius=8, fill=fill, outline="",
                    background=theme.FIELD_BG, tags="row")
            c.create_text(
                12, (y0 + y1) / 2,
                text=self._fit_popup_text(self._row_text(val_i), max_text),
                fill=fg, font=self._tkfont, anchor="w", tags="row")

    def _popup_index_at(self, y: float) -> Optional[int]:
        c = self._popup_canvas
        if c is None:
            return None
        y = float(c.canvasy(y))
        idx = int((y - 4) // self._popup_row_h)
        if 0 <= idx < len(self._visible_indices):
            return idx
        return None

    def _on_popup_motion(self, event):
        idx = self._popup_index_at(event.y)
        if idx is None or idx == self._popup_active:
            return
        self._popup_active = idx
        self._redraw_popup_rows()

    def _popup_pointer_inside(self, event) -> bool:
        popup = self._popup
        if popup is None:
            return False
        try:
            hovered = popup.winfo_containing(event.x_root, event.y_root)
        except (tk.TclError, KeyError, TypeError):
            hovered = None
        if hovered is not None and self._is_within(hovered, popup):
            return True
        try:
            x, y = event.x_root, event.y_root
            return (popup.winfo_rootx() <= x <= popup.winfo_rootx() + popup.winfo_width()
                    and popup.winfo_rooty() <= y <= popup.winfo_rooty() + popup.winfo_height())
        except tk.TclError:
            return False

    def _on_popup_wheel(self, event):
        c = self._popup_canvas
        if c is None or not self._popup_pointer_inside(event):
            return
        if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0:
            c.yview_scroll(-1, "units")
        else:
            c.yview_scroll(1, "units")
        return "break"

    def _on_popup_touchpad(self, event):
        c = self._popup_canvas
        if c is None or not self._popup_pointer_inside(event):
            return
        try:
            _dx, dy_str = self.tk.splitlist(
                self.tk.call("tk::PreciseScrollDeltas", event.delta))
            dy = int(dy_str)
        except (tk.TclError, ValueError, AttributeError):
            return
        if dy == 0:
            return "break"
        try:
            region = str(c.cget("scrollregion")).split()
            content_h = float(region[3]) - float(region[1])
            if content_h <= 0:
                return "break"
            new_top = c.yview()[0] * content_h - dy
            new_top = max(0.0, min(new_top, content_h))
            c.yview_moveto(new_top / content_h)
        except (tk.TclError, ValueError, IndexError):
            c.yview_scroll(-1 if dy > 0 else 1, "units")
        return "break"

    def _on_pick(self, event=None):
        if self._popup is None:
            return "break"
        idx = self._popup_active
        if (event is not None
                and getattr(event, "widget", None) is self._popup_canvas
                and hasattr(event, "y")):
            hit = self._popup_index_at(event.y)
            if hit is not None:
                idx = hit
        if idx is not None and 0 <= idx < len(self._visible_indices):
            self._variable.set(self._values[self._visible_indices[idx]])
            self._close_popup()
            self.event_generate("<<ComboboxSelected>>")
        elif not self._visible_indices:
            return "break"
        else:
            self._close_popup()
        return "break"

    def _on_any_click(self, event):
        try:
            if not self.winfo_exists() or self._popup is None:
                return
        except tk.TclError:
            return
        widget = event.widget
        if isinstance(widget, str):
            try:
                widget = self.nametowidget(widget)
            except KeyError:
                widget = None
        if widget is None:
            return
        if self._is_within(widget, self) or self._is_within(widget, self._popup):
            return
        self._close_popup()

    @staticmethod
    def _is_within(widget, ancestor) -> bool:
        w = widget
        while w is not None:
            if w is ancestor:
                return True
            w = getattr(w, "master", None)
        return False

    def _close_popup(self):
        popup = self._popup
        self._popup = None
        self._popup_canvas = None
        self._filter_entry = None
        self._listbox = None
        if popup is not None:
            try:
                popup.destroy()
            except tk.TclError:
                pass
        try:
            if self.winfo_exists():
                self._redraw()
        except tk.TclError:
            pass


class RoundedEntry(tk.Frame):
    """Text field in a pill, matching the sidebar search box and
    RoundedCombobox -- ttk.Entry stays a hard rectangle on Aqua."""

    def __init__(self, master, textvariable=None, width=24, show="", **kwargs):
        bg = kwargs.pop("bg", None) or _parent_bg(master)
        kwargs.setdefault("highlightthickness", 0)
        super().__init__(master, bg=bg, **kwargs)
        card = RoundedCard(
            self, bg=theme.FIELD_BG, radius=10, pad=10, outline=False, shrink=True)
        card.pack(fill="x", expand=True)
        family = theme.resolve_font_family()
        self._entry = tk.Entry(
            card.body, textvariable=textvariable, font=(family, 11),
            relief="flat", bd=0, highlightthickness=0, width=int(width) if width else 24,
            bg=theme.FIELD_BG, fg=theme.TEXT_PRIMARY,
            insertbackground=theme.TEXT_PRIMARY, show=show)
        self._entry.pack(fill="x", ipady=3)

    def configure(self, **kwargs):  # type: ignore[override]
        if "show" in kwargs:
            self._entry.configure(show=kwargs.pop("show"))
        if kwargs:
            super().configure(**kwargs)

    def config(self, **kwargs):  # type: ignore[override]
        self.configure(**kwargs)

    def focus_set(self):
        self._entry.focus_set()


class RoundedCheckbutton(tk.Frame):
    """A 20px rounded box plus label -- ttk.Checkbutton draws a square
    indicator on Aqua regardless of style padding."""

    def __init__(self, master, text: str = "", variable=None, command=None, **kwargs):
        bg = kwargs.pop("bg", None) or _parent_bg(master)
        kwargs.setdefault("highlightthickness", 0)
        super().__init__(master, bg=bg, **kwargs)
        self._bg = bg
        self._var = variable if variable is not None else tk.BooleanVar(value=False)
        self._command = command
        self._box = tk.Canvas(self, width=22, height=22, bg=bg, highlightthickness=0)
        self._box.pack(side="left")
        family = theme.resolve_font_family()
        self._label = tk.Label(
            self, text=text, bg=bg, fg=theme.TEXT_PRIMARY, font=(family, 11),
            anchor="w", justify="left")
        self._label.pack(side="left", padx=(8, 0))
        for widget in (self._box, self._label):
            widget.bind("<Button-1>", self._toggle)
        self.bind("<Button-1>", self._toggle)
        self._trace = self._var.trace_add("write", lambda *_: self._redraw())
        self._redraw()

    def _toggle(self, _event=None):
        self._var.set(not bool(self._var.get()))
        if self._command is not None:
            self._command()
        return "break"

    def _redraw(self):
        c = self._box
        c.delete("all")
        on = bool(self._var.get())
        fill = theme.ACCENT if on else theme.FIELD_BG
        theme.place_rounded_rect(
            c, 1, 1, 21, 21, radius=6, fill=fill, outline="",
            background=self._bg)
        if on:
            c.create_line(6, 12, 10, 16, 16, 7, fill="#FFFFFF", width=2,
                          capstyle="round", joinstyle="round")


# ---------------------------------------------------------------------------
# Rounded cards
# ---------------------------------------------------------------------------
class RoundedCard(tk.Frame):
    """A rounded-corner container. Draws a rounded rectangle onto an
    internal Canvas sized to fill `self`, then overlays `.body` (a plain
    Frame, filled with the same color as the rounded shape) inset by at
    least the corner radius on every side -- so `.body`'s own square
    corners always land on the rounded rect's straight edges, invisible
    against the matching fill color underneath. Real content goes in
    `.body`, packed/gridded exactly like it would be in any other Frame.
    """

    def __init__(self, master, bg: Optional[str] = None, radius: int = CARD_RADIUS,
                 outline: bool = True, pad: Optional[int] = None,
                 shrink: bool = False, outer_bg: Optional[str] = None,
                 corner_bgs: Optional[dict] = None, **kwargs):
        self._bg = bg or theme.PANEL_BG
        self._corner_bgs = corner_bgs
        if outer_bg is None and corner_bgs:
            outer_bg = corner_bgs.get("nw")
        if outer_bg is None:
            outer_bg = _parent_bg(master)
        kwargs.setdefault("bg", outer_bg)
        kwargs.setdefault("highlightthickness", 0)
        kwargs.setdefault("bd", 0)
        kwargs.setdefault("relief", "flat")
        super().__init__(master, **kwargs)
        self._radius = radius
        self._outline = outline
        if pad is None:
            self._inset = max(8, int(radius))
        elif pad <= 0:
            self._inset = 0
        else:
            # Square `.body` corners must sit on the straight edges of the
            # rounded canvas, otherwise they read as a box around the card.
            self._inset = max(int(pad), int(radius))

        self._canvas = tk.Canvas(self, highlightthickness=0, bg=outer_bg)
        self._canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self._canvas.bind("<Configure>", lambda e: self._redraw())
        self._shape_ids = []
        self._drawn_size = None

        self.body = tk.Frame(self, bg=self._bg)
        i = self._inset
        # `place`d children don't give this Frame a requested size, so a
        # card packed fill="x" (search, chips) would collapse to 0px tall.
        # Shrink-wrap by packing `.body` instead; fill-panels keep place.
        if shrink:
            self.body.pack(fill="both", expand=True, padx=i, pady=i)
        else:
            self.body.place(x=i, y=i, relwidth=1, relheight=1, width=-2 * i, height=-2 * i)

    def _redraw(self):
        c = self._canvas
        w, h = c.winfo_width(), c.winfo_height()
        if w <= 1 or h <= 1:
            return
        size = (w, h)
        if size == self._drawn_size and self._shape_ids:
            return
        outline_color = theme.BORDER if self._outline else ""
        outer_bg = c.cget("bg") or theme.APP_BG
        new_ids = theme.place_rounded_rect(
            c, 0, 0, w, h, radius=self._radius,
            fill=self._bg, outline=outline_color, width=1 if self._outline else 0,
            background=outer_bg, corner_backgrounds=self._corner_bgs)
        for old_id in self._shape_ids:
            try:
                c.delete(old_id)
            except tk.TclError:
                pass
        self._shape_ids = new_ids
        self._drawn_size = size

    def set_fill(self, bg: str):
        """Repaint this card's fill (sidebar row hover)."""
        if (bg or "").upper() == (self._bg or "").upper():
            return
        self._bg = bg
        try:
            self.body.configure(bg=bg)
        except tk.TclError:
            pass
        self._drawn_size = None
        self._redraw()

    def set_corner_backgrounds(self, corner_bgs: Optional[dict]):
        """Repaint the cut-off corners against whatever is behind this card.

        The widget is still a square Frame; the rounded fill only covers
        the middle. Without this, those four leftover triangles keep the
        parent/window color and read as a box around the card.
        """
        corner_bgs = corner_bgs or {}
        key = (
            corner_bgs.get("nw"), corner_bgs.get("ne"),
            corner_bgs.get("sw"), corner_bgs.get("se"),
        )
        if key == getattr(self, "_corner_key", None) and self._shape_ids:
            return
        self._corner_key = key
        self._corner_bgs = corner_bgs
        self._drawn_size = None
        outer = corner_bgs.get("nw") or theme.APP_BG
        try:
            self.configure(bg=outer)
            self._canvas.configure(bg=outer)
        except tk.TclError:
            pass
        self._redraw()


class ScrollArea(RoundedCard):
    """A RoundedCard whose interior scrolls vertically -- the one thing to
    reach for whenever a panel's content might not fit the window (which,
    below full-screen, several already didn't: Settings' theme
    previews, the Activities sidebar once it has more than a handful of
    activities/projects, a tall Duplicate/Export/Backup panel, etc).

    Real content goes in `.content` (not `.body`, which here just hosts the
    scrolling machinery). Scrolling works four ways, in order of how
    reliable each one has proven across platforms in this app's history
    (see the long comment this replaces in sidebar.py's git history):
    two-finger trackpad scrolling on Tk 9+ (see _on_touchpad_anywhere/
    _on_touchpad_direct below -- Tk 9.0 stopped translating trackpad
    gestures into MouseWheel events at all, per TIP 684, in favor of a new
    dedicated TouchpadScroll event; confirmed via an isolated reproduction
    that on Tk 9.0.4/macOS zero MouseWheel/Button-4/Button-5 events ever
    fire for a trackpad swipe, only this one does), classic mouse wheel
    (best-effort -- older Tk/Aqua builds and some trackpads don't always
    deliver wheel events the way X11 does either), dragging the scrollbar
    thumb, and clicking the scrollbar's up/down arrow buttons or its bare
    track (guaranteed to work everywhere, since these depend only on plain
    button clicks rather than any wheel/scroll event actually reaching Tk).
    """

    def __init__(self, master, bg: Optional[str] = None, radius: int = CARD_RADIUS,
                 outline: bool = True, pad: Optional[int] = None,
                 autohide_scrollbar: bool = False, **kwargs):
        kwargs.pop("shrink", None)
        super().__init__(master, bg=bg, radius=radius, outline=outline, pad=pad, **kwargs)
        bgc = self._bg
        self._autohide_scrollbar = autohide_scrollbar

        self.canvas = tk.Canvas(self.body, bg=bgc, highlightthickness=0)
        self.scrollbar = VectorScrollbar(self.body, command=self.canvas.yview, bg=bgc)
        self.content = tk.Frame(self.canvas, bg=bgc)
        self._wheel_bound: set = set()
        # TouchpadScroll (see _scroll_touchpad_from_event) reports small,
        # precise per-event pixel deltas via tk::PreciseScrollDeltas -- no
        # rounding to integer "units" happens anywhere in that path, so
        # unlike classic MouseWheel there's no leftover fractional motion
        # to track between events.

        self.content.bind("<Configure>", self._on_content_configure)
        window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(window, width=e.width))
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        # Scrollbar packed first: an expand=True widget packed first claims
        # the whole container, leaving a later-packed scrollbar nothing to
        # show in (see sidebar.py's original note on this same gotcha).
        # Pie legends pass autohide_scrollbar so a 4-row key doesn't keep
        # a dead track between the names and the hours.
        if not autohide_scrollbar:
            self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        # Mouse wheel scrolling is wired up TWO independent ways, since a
        # real user reported wheel scrolling still not working after the
        # first approach alone (bind_all + winfo_containing) shipped, on
        # macOS specifically:
        #
        # 1. A global, geometry-hit-tested dispatch: on every wheel event
        #    anywhere in the app, check via winfo_containing() whether the
        #    pointer is actually over *this* ScrollArea. Works well on
        #    X11/Windows in testing.
        # 2. A DIRECT binding on every widget inside `.content` (see
        #    bind_wheel_recursive/_on_content_configure below), which
        #    sidesteps winfo_containing() entirely -- the event simply
        #    fires on whichever widget the pointer is actually over, no
        #    coordinate math involved. This matters because winfo_containing
        #    depends on root-window coordinates lining up with what the
        #    platform reports for the pointer, which is exactly the kind of
        #    thing HiDPI/Retina scaling on macOS has a real history of
        #    getting subtly wrong in Tk -- see the module docstring's note
        #    on Tk/Aqua wheel delivery being unreliable in general.
        #
        # Both paths end up calling _scroll_from_event, so neither one
        # "wins" -- whichever fires first just scrolls the canvas.
        self.bind_all("<MouseWheel>", self._on_wheel_anywhere, add="+")
        self.bind_all("<Button-4>", self._on_wheel_anywhere, add="+")
        self.bind_all("<Button-5>", self._on_wheel_anywhere, add="+")
        # <TouchpadScroll> is a Tk 9+ event (TIP 684). An older/other Tk
        # build that's never heard of it usually just never fires it if
        # you bind to it -- but Tk on Windows instead raises TclError
        # ("bad event type or keysym") the moment you try, which crashed
        # the app on startup there. So this is probed once, here, instead
        # of assumed: _bind_touchpad_scroll reports whether it actually
        # took, and every later touchpad bind (bind_wheel_recursive below)
        # trusts that same result instead of trying again.
        self._touchpad_supported = self._bind_touchpad_scroll(
            self, self._on_touchpad_anywhere, bind_all=True)
        self.bind_wheel_recursive(self.canvas)
        self.bind_wheel_recursive(self.scrollbar)
        self.bind_wheel_recursive(self.content)

    def _bind_touchpad_scroll(self, widget, handler, bind_all: bool = False) -> bool:
        """Best-effort <TouchpadScroll> bind (Tk 9+, TIP 684). Confirmed on
        Windows that some Tk builds raise TclError for this event name at
        bind time ("bad event type or keysym") rather than silently never
        firing it, so every attempt is guarded -- a failure here just means
        touchpad scrolling falls back to the other three scrolling paths
        (mouse wheel, scrollbar drag, and the scrollbar's arrow buttons)."""
        try:
            if bind_all:
                widget.bind_all("<TouchpadScroll>", handler, add="+")
            else:
                widget.bind("<TouchpadScroll>", handler, add="+")
            return True
        except tk.TclError:
            return False

    def refresh_scrollregion(self):
        """Call after replacing `.content`'s children in bulk (destroying
        and re-`pack`ing everything, say) if a caller needs the scrollbar
        to reflect the new size sooner than the next natural <Configure>."""
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.bind_wheel_recursive(self.content)
        try:
            self.update_idletasks()
        except tk.TclError:
            pass
        self._sync_scrollbar()

    def _sync_scrollbar(self):
        if not self._autohide_scrollbar:
            return
        try:
            bbox = self.canvas.bbox("all")
            content_h = (bbox[3] - bbox[1]) if bbox else 0
            view_h = int(self.canvas.winfo_height())
        except tk.TclError:
            return
        overflow = content_h > view_h + 1 and view_h > 1
        try:
            mapped = bool(self.scrollbar.winfo_ismapped())
        except tk.TclError:
            return
        if overflow and not mapped:
            self.scrollbar.pack(side="right", fill="y", before=self.canvas)
        elif not overflow and mapped:
            self.scrollbar.pack_forget()
            try:
                self.canvas.yview_moveto(0)
            except tk.TclError:
                pass

    def _on_content_configure(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        # `.content`'s children change constantly (rows added/removed,
        # panels rebuilt) -- re-attach the direct wheel binding here so
        # every newly-created row/label/frame picks it up automatically,
        # without every call site that populates a ScrollArea needing to
        # remember to do it themselves. bind_wheel_recursive tracks what's
        # already bound, so this is cheap on repeat calls.
        self.bind_wheel_recursive(self.content)
        self._sync_scrollbar()

    def bind_wheel_recursive(self, widget):
        """Attach the direct (non-hit-tested) wheel handler to `widget` and
        every current descendant of it, skipping anything already bound
        (tracked in `self._wheel_bound`) so repeat calls -- e.g. from
        _on_content_configure, which fires on every row add/remove -- don't
        keep stacking additional callbacks on long-lived widgets and
        scrolling faster and faster."""
        if widget not in self._wheel_bound:
            widget.bind("<MouseWheel>", self._on_wheel_direct, add="+")
            widget.bind("<Button-4>", self._on_wheel_direct, add="+")
            widget.bind("<Button-5>", self._on_wheel_direct, add="+")
            # Tk 9+ TouchpadScroll (TIP 684) -- see class docstring and
            # _bind_touchpad_scroll. Only attempted when the __init__ probe
            # already confirmed this Tk build actually supports it.
            if self._touchpad_supported:
                self._bind_touchpad_scroll(widget, self._on_touchpad_direct)
            self._wheel_bound.add(widget)
        for child in widget.winfo_children():
            self.bind_wheel_recursive(child)

    def _is_within(self, widget) -> bool:
        w = widget
        while w is not None:
            if w is self.canvas or w is self.scrollbar:
                return True
            w = getattr(w, "master", None)
        return False

    def _scroll_from_event(self, event) -> bool:
        """Scroll the canvas per one wheel event. Returns whether it
        actually recognized the event as a scroll (handles the X11-style
        Button-4/5 case and the delta-based MouseWheel case used by
        Windows/macOS)."""
        if getattr(event, "num", None) == 4:
            self.canvas.yview_scroll(-1, "units")
        elif getattr(event, "num", None) == 5:
            self.canvas.yview_scroll(1, "units")
        elif getattr(event, "delta", 0):
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        else:
            return False
        return True

    def _on_wheel_anywhere(self, event):
        try:
            hovered = self.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):
            return
        within = self._is_within(hovered)
        if _DEBUG_WHEEL:
            print(f"[wheel-debug] anywhere event={event.type} num={getattr(event, 'num', None)} "
                  f"delta={getattr(event, 'delta', None)} root=({event.x_root},{event.y_root}) "
                  f"hovered={hovered!r} within={within}", flush=True)
        if not within:
            return
        self._scroll_from_event(event)

    def _on_wheel_direct(self, event):
        # Being called at all means the event fired on a widget we know is
        # inside this ScrollArea -- no hit-test needed, unlike
        # _on_wheel_anywhere above.
        if _DEBUG_WHEEL:
            print(f"[wheel-debug] direct event={event.type} num={getattr(event, 'num', None)} "
                  f"delta={getattr(event, 'delta', None)} widget={event.widget!r}", flush=True)
        self._scroll_from_event(event)

    def _scroll_touchpad_from_event(self, event) -> bool:
        """Scroll the canvas per one <TouchpadScroll> event (Tk 9+, TIP 684).

        Unlike classic <MouseWheel> (one event == one "click" == a fixed
        unit jump), TouchpadScroll fires many events per swipe, each
        carrying a small precise pixel delta packed into event.delta (the
        Tk %D substitution). We unpack it with the Tcl helper proc
        tk::PreciseScrollDeltas, then move the view by that many pixels
        (via yview_moveto against the canvas's current scrollregion)
        rather than converting to integer "units" -- doing units would
        require setting an explicit yscrollincrement on the canvas, which
        would risk changing the already-working feel of the classic
        MouseWheel/Button-4/5/scrollbar-arrow paths that all currently
        rely on Tk's default per-canvas unit size.

        Sign convention: mirrors the classic wheel handling below (a
        negative delta scrolls the view down). macOS's own "natural
        scrolling" setting already inverts the raw hardware signal before
        Tk ever sees it, so this is a best-guess convention -- flip the
        sign here if real-world testing shows it's backwards.
        """
        try:
            dx_str, dy_str = self.tk.splitlist(
                self.tk.call("tk::PreciseScrollDeltas", event.delta)
            )
            dy = int(dy_str)
        except (tk.TclError, ValueError, AttributeError):
            return False

        if dy == 0:
            return True  # recognized as a touchpad event, just no vertical motion

        try:
            region = self.canvas.cget("scrollregion")
            _, y0, _, y1 = (float(v) for v in str(region).split())
            content_height = y1 - y0
        except (tk.TclError, ValueError):
            return False

        if content_height <= 0:
            return True

        pixel_delta = -dy

        try:
            current_top = self.canvas.yview()[0] * content_height
        except tk.TclError:
            return False

        new_top = current_top + pixel_delta
        new_top = max(0.0, min(new_top, content_height))
        self.canvas.yview_moveto(new_top / content_height)
        return True

    def _on_touchpad_anywhere(self, event):
        try:
            hovered = self.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):
            return
        within = self._is_within(hovered)
        if _DEBUG_WHEEL:
            print(f"[touchpad-debug] anywhere event={event.type} delta={getattr(event, 'delta', None)} "
                  f"root=({event.x_root},{event.y_root}) hovered={hovered!r} within={within}", flush=True)
        if not within:
            return
        self._scroll_touchpad_from_event(event)

    def _on_touchpad_direct(self, event):
        # Being called at all means the event fired on a widget we know is
        # inside this ScrollArea -- no hit-test needed, unlike
        # _on_touchpad_anywhere above.
        if _DEBUG_WHEEL:
            print(f"[touchpad-debug] direct event={event.type} delta={getattr(event, 'delta', None)} "
                  f"widget={event.widget!r}", flush=True)
        self._scroll_touchpad_from_event(event)


# ---------------------------------------------------------------------------
# Scrollbar
# ---------------------------------------------------------------------------
class VectorScrollbar(tk.Canvas):
    """A hand-drawn stand-in for ttk.Scrollbar.

    ttk.Scrollbar's "clam"-theme thumb/track doesn't reliably paint in this
    app's target environments -- an isolated reproduction (a bare Canvas +
    ttk.Scrollbar with an explicit width and a bright, unmistakable color)
    still rendered with zero visible pixels. That's the same class of
    problem this app has already hit with native pop-up window placement
    and Unicode glyph rendering, and the fix has always been the same:
    stop trusting the toolkit to draw the thing and draw it ourselves.

    This implements just enough of the real Scrollbar protocol to be a
    drop-in replacement: `set(first, last)` (called automatically via
    `yscrollcommand`) to draw the thumb, and being passed as `command=` to
    whatever it scrolls -- it calls that command the same way a real
    Scrollbar would, with ("moveto", fraction) or ("scroll", n, "units").

    Also draws a small up/down arrow button at each end of the track, and
    supports clicking/dragging the thumb and clicking the bare track. These
    all work via plain <Button-1>/<ButtonRelease-1>/<B1-Motion> and don't
    depend on wheel events at all -- a guaranteed fallback for platforms
    where mouse-wheel delivery to Tk is unreliable (see ScrollArea above).
    """
    WIDTH = 12
    MIN_THUMB_H = 24
    ARROW_H = 14
    ARROW_REPEAT_MS = 90
    ARROW_REPEAT_FIRST_MS = 350

    def __init__(self, master, command: Callable[..., None], **kwargs):
        kwargs.setdefault("width", self.WIDTH)
        kwargs.setdefault("bg", theme.PANEL_BG)
        kwargs.setdefault("highlightthickness", 0)
        kwargs.setdefault("cursor", "arrow")
        super().__init__(master, **kwargs)
        self._command = command
        self._first = 0.0
        self._last = 1.0
        self._dragging = False
        self._drag_offset = 0.0
        self._hover = False
        self._arrow_hover: Optional[str] = None  # "up" | "down" | None
        self._repeat_job: Optional[str] = None
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Button-1>", self._on_click)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Motion>", self._on_motion)

    def set(self, first, last):
        """Matches ttk.Scrollbar.set / the yscrollcommand protocol."""
        self._first = float(first)
        self._last = float(last)
        self._redraw()

    # -- Track geometry (the draggable area sits between the two arrows) --
    def _track_bounds(self):
        h = self.winfo_height()
        return self.ARROW_H, max(self.ARROW_H, h - self.ARROW_H)

    def _thumb_bounds(self):
        track_top, track_bottom = self._track_bounds()
        track_h = track_bottom - track_top
        if track_h <= 1:
            return track_top, track_top
        top = track_top + self._first * track_h
        bottom = track_top + self._last * track_h
        if bottom - top < self.MIN_THUMB_H:
            center = (top + bottom) / 2
            top = center - self.MIN_THUMB_H / 2
            bottom = center + self.MIN_THUMB_H / 2
            if top < track_top:
                top, bottom = track_top, track_top + self.MIN_THUMB_H
            elif bottom > track_bottom:
                top, bottom = track_bottom - self.MIN_THUMB_H, track_bottom
        return top, bottom

    def _which_arrow(self, y) -> Optional[str]:
        h = self.winfo_height()
        if h <= 2 * self.ARROW_H:
            return None
        if y < self.ARROW_H:
            return "up"
        if y > h - self.ARROW_H:
            return "down"
        return None

    def _redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:
            return
        self.create_rectangle(0, 0, w, h, fill=self.cget("bg"), outline="")

        can_scroll = not (self._first <= 0.0 and self._last >= 1.0)

        for which in ("up", "down"):
            active = self._arrow_hover == which and can_scroll
            tri_color = theme.ACCENT if active else (
                theme.TEXT_SECONDARY if can_scroll else theme.BORDER_STRONG)
            cx = w / 2
            cy = self.ARROW_H / 2 if which == "up" else h - self.ARROW_H / 2
            sz = 3.2
            if which == "up":
                pts = (cx - sz, cy + sz * 0.6, cx + sz, cy + sz * 0.6, cx, cy - sz * 0.6)
            else:
                pts = (cx - sz, cy - sz * 0.6, cx + sz, cy - sz * 0.6, cx, cy + sz * 0.6)
            self.create_polygon(*pts, fill=tri_color, outline="")

        if not can_scroll:
            # Everything already fits -- an empty track (and dimmed arrows)
            # communicates that better than a full-height thumb that can't
            # actually move.
            return
        top, bottom = self._thumb_bounds()
        pad = 2
        color = theme.ACCENT if (self._hover or self._dragging) else theme.BORDER_STRONG
        theme.place_rounded_rect(self, pad, top + pad, w - pad, bottom - pad,
                                 radius=(w - 2 * pad) / 2, fill=color, outline="",
                                 background=self.cget("bg"))

    def _on_enter(self, _event=None):
        self._hover = True
        self._redraw()

    def _on_leave(self, _event=None):
        self._hover = False
        self._arrow_hover = None
        self._redraw()

    def _on_motion(self, event):
        if self._dragging:
            return
        arrow = self._which_arrow(event.y)
        if arrow != self._arrow_hover:
            self._arrow_hover = arrow
            self._redraw()

    def _on_click(self, event):
        arrow = self._which_arrow(event.y)
        if arrow is not None:
            self._step(arrow)
            self._start_repeat(arrow)
            return
        top, bottom = self._thumb_bounds()
        if top <= event.y <= bottom:
            self._dragging = True
            self._drag_offset = event.y - top
            self._redraw()
        else:
            # Clicking the bare track (above or below the thumb) jumps the
            # view to roughly that position, same as clicking a real
            # scrollbar's track.
            track_top, track_bottom = self._track_bounds()
            track_h = track_bottom - track_top
            frac = max(0.0, min(1.0, (event.y - track_top) / track_h)) if track_h > 1 else 0.0
            self._command("moveto", frac)

    def _step(self, arrow: str):
        self._command("scroll", -1 if arrow == "up" else 1, "units")

    def _start_repeat(self, arrow: str):
        # Press-and-hold auto-repeats the step, same as a native scrollbar
        # arrow, so holding it down pages through a long list instead of
        # needing repeated clicks.
        self._cancel_repeat()

        def repeat():
            if self._arrow_hover != arrow:
                return
            self._step(arrow)
            self._repeat_job = self.after(self.ARROW_REPEAT_MS, repeat)

        self._repeat_job = self.after(self.ARROW_REPEAT_FIRST_MS, repeat)

    def _cancel_repeat(self):
        if self._repeat_job is not None:
            self.after_cancel(self._repeat_job)
            self._repeat_job = None

    def _on_drag(self, event):
        if not self._dragging:
            return
        track_top, track_bottom = self._track_bounds()
        track_h = track_bottom - track_top
        if track_h <= 1:
            return
        top0, bottom0 = self._thumb_bounds()
        thumb_h = bottom0 - top0
        usable = track_h - thumb_h
        new_top = max(track_top, min(track_top + usable, event.y - self._drag_offset))
        frac = (new_top - track_top) / usable if usable > 0 else 0.0
        self._command("moveto", frac)

    def _on_release(self, _event=None):
        self._dragging = False
        self._cancel_repeat()
        arrow = self._which_arrow(_event.y) if _event is not None else None
        self._arrow_hover = arrow
        self._redraw()


class HorizontalVectorScrollbar(tk.Canvas):
    """HorizontalVectorScrollbar is VectorScrollbar's mirror image -- same
    hand-drawn thumb/track/arrow-button approach (see VectorScrollbar's
    own docstring for why this app draws its own scrollbars at all), just
    laid out along x instead of y. Used by the calendar grid's new
    horizontal scroll fallback (see calendar_view.py) -- VectorScrollbar
    itself is left untouched since ScrollArea and every other existing
    user of it depends on its vertical-only behavior exactly as it is.

    Same drop-in protocol as VectorScrollbar: `set(first, last)` (via
    `xscrollcommand`) and `command=` called with ("moveto", fraction) or
    ("scroll", n, "units")."""
    HEIGHT = 12
    MIN_THUMB_W = 24
    ARROW_W = 14
    ARROW_REPEAT_MS = 90
    ARROW_REPEAT_FIRST_MS = 350

    def __init__(self, master, command: Callable[..., None], **kwargs):
        kwargs.setdefault("height", self.HEIGHT)
        kwargs.setdefault("bg", theme.PANEL_BG)
        kwargs.setdefault("highlightthickness", 0)
        kwargs.setdefault("cursor", "arrow")
        super().__init__(master, **kwargs)
        self._command = command
        self._first = 0.0
        self._last = 1.0
        self._dragging = False
        self._drag_offset = 0.0
        self._hover = False
        self._arrow_hover: Optional[str] = None  # "left" | "right" | None
        self._repeat_job: Optional[str] = None
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Button-1>", self._on_click)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Motion>", self._on_motion)

    def set(self, first, last):
        """Matches ttk.Scrollbar.set / the xscrollcommand protocol."""
        self._first = float(first)
        self._last = float(last)
        self._redraw()

    # -- Track geometry (the draggable area sits between the two arrows) --
    def _track_bounds(self):
        w = self.winfo_width()
        return self.ARROW_W, max(self.ARROW_W, w - self.ARROW_W)

    def _thumb_bounds(self):
        track_left, track_right = self._track_bounds()
        track_w = track_right - track_left
        if track_w <= 1:
            return track_left, track_left
        left = track_left + self._first * track_w
        right = track_left + self._last * track_w
        if right - left < self.MIN_THUMB_W:
            center = (left + right) / 2
            left = center - self.MIN_THUMB_W / 2
            right = center + self.MIN_THUMB_W / 2
            if left < track_left:
                left, right = track_left, track_left + self.MIN_THUMB_W
            elif right > track_right:
                left, right = track_right - self.MIN_THUMB_W, track_right
        return left, right

    def _which_arrow(self, x) -> Optional[str]:
        w = self.winfo_width()
        if w <= 2 * self.ARROW_W:
            return None
        if x < self.ARROW_W:
            return "left"
        if x > w - self.ARROW_W:
            return "right"
        return None

    def _redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:
            return
        self.create_rectangle(0, 0, w, h, fill=self.cget("bg"), outline="")

        can_scroll = not (self._first <= 0.0 and self._last >= 1.0)

        for which in ("left", "right"):
            active = self._arrow_hover == which and can_scroll
            tri_color = theme.ACCENT if active else (
                theme.TEXT_SECONDARY if can_scroll else theme.BORDER_STRONG)
            cy = h / 2
            cx = self.ARROW_W / 2 if which == "left" else w - self.ARROW_W / 2
            sz = 3.2
            if which == "left":
                pts = (cx + sz * 0.6, cy - sz, cx + sz * 0.6, cy + sz, cx - sz * 0.6, cy)
            else:
                pts = (cx - sz * 0.6, cy - sz, cx - sz * 0.6, cy + sz, cx + sz * 0.6, cy)
            self.create_polygon(*pts, fill=tri_color, outline="")

        if not can_scroll:
            # Everything already fits -- an empty track (and dimmed arrows)
            # communicates that better than a full-width thumb that can't
            # actually move.
            return
        left, right = self._thumb_bounds()
        pad = 2
        color = theme.ACCENT if (self._hover or self._dragging) else theme.BORDER_STRONG
        theme.place_rounded_rect(self, left + pad, pad, right - pad, h - pad,
                                 radius=(h - 2 * pad) / 2, fill=color, outline="",
                                 background=self.cget("bg"))

    def _on_enter(self, _event=None):
        self._hover = True
        self._redraw()

    def _on_leave(self, _event=None):
        self._hover = False
        self._arrow_hover = None
        self._redraw()

    def _on_motion(self, event):
        if self._dragging:
            return
        arrow = self._which_arrow(event.x)
        if arrow != self._arrow_hover:
            self._arrow_hover = arrow
            self._redraw()

    def _on_click(self, event):
        arrow = self._which_arrow(event.x)
        if arrow is not None:
            self._step(arrow)
            self._start_repeat(arrow)
            return
        left, right = self._thumb_bounds()
        if left <= event.x <= right:
            self._dragging = True
            self._drag_offset = event.x - left
            self._redraw()
        else:
            # Clicking the bare track (left or right of the thumb) jumps
            # the view to roughly that position, same as clicking a real
            # scrollbar's track.
            track_left, track_right = self._track_bounds()
            track_w = track_right - track_left
            frac = max(0.0, min(1.0, (event.x - track_left) / track_w)) if track_w > 1 else 0.0
            self._command("moveto", frac)

    def _step(self, arrow: str):
        self._command("scroll", -1 if arrow == "left" else 1, "units")

    def _start_repeat(self, arrow: str):
        # Press-and-hold auto-repeats the step, same as a native scrollbar
        # arrow, so holding it down pages through a long list instead of
        # needing repeated clicks.
        self._cancel_repeat()

        def repeat():
            if self._arrow_hover != arrow:
                return
            self._step(arrow)
            self._repeat_job = self.after(self.ARROW_REPEAT_MS, repeat)

        self._repeat_job = self.after(self.ARROW_REPEAT_FIRST_MS, repeat)

    def _cancel_repeat(self):
        if self._repeat_job is not None:
            self.after_cancel(self._repeat_job)
            self._repeat_job = None

    def _on_drag(self, event):
        if not self._dragging:
            return
        track_left, track_right = self._track_bounds()
        track_w = track_right - track_left
        if track_w <= 1:
            return
        left0, right0 = self._thumb_bounds()
        thumb_w = right0 - left0
        usable = track_w - thumb_w
        new_left = max(track_left, min(track_left + usable, event.x - self._drag_offset))
        frac = (new_left - track_left) / usable if usable > 0 else 0.0
        self._command("moveto", frac)

    def _on_release(self, _event=None):
        self._dragging = False
        self._cancel_repeat()
        arrow = self._which_arrow(_event.x) if _event is not None else None
        self._arrow_hover = arrow
        self._redraw()


def show_saved_toast(widget, text: str = "Saved"):
    """Brief, self-dismissing confirmation toast anchored to the
    bottom-right corner of `widget`'s window -- the only feedback after
    clicking Save used to be the panel/tab closing, which is easy to miss
    if you weren't watching for it. Call this right when a save succeeds,
    before the panel closes.

    Deliberately not a Toplevel the user can interact with: it doesn't
    grab focus, isn't modal, and never raises on failure -- confirmation
    is a nice-to-have, so on any platform quirk (no window manager
    support for override-redirect positioning, no -alpha support, a
    window that's mid-teardown) this just quietly does nothing rather
    than risking the save itself.

    Calling this again before a previous toast has faded replaces it
    (tracked on the root window as _saved_toast) instead of stacking
    multiple toasts on top of each other.
    """
    try:
        root = widget.winfo_toplevel()
        existing = getattr(root, "_saved_toast", None)
        if existing is not None:
            try:
                existing.destroy()
            except tk.TclError:
                pass
            root._saved_toast = None

        toast = tk.Toplevel(root)
        root._saved_toast = toast
        toast.withdraw()
        toast.overrideredirect(True)
        try:
            toast.attributes("-topmost", True)
        except tk.TclError:
            pass

        bg = theme.ACCENT
        card = RoundedCard(toast, bg=bg, radius=10, outline=False, pad=12)
        card.pack()
        family = theme.resolve_font_family()
        tk.Label(card.body, text=f"✓  {text}", font=(family, 11, "bold"),
                 bg=bg, fg="#FFFFFF").pack(padx=6, pady=4)

        toast.update_idletasks()
        w, h = toast.winfo_reqwidth(), toast.winfo_reqheight()
        x = root.winfo_rootx() + root.winfo_width() - w - 28
        y = root.winfo_rooty() + root.winfo_height() - h - 28
        toast.geometry(f"{w}x{h}+{x}+{y}")

        supports_alpha = True
        try:
            toast.attributes("-alpha", 0.0)
        except tk.TclError:
            supports_alpha = False
        toast.deiconify()

        def destroy_toast():
            if getattr(root, "_saved_toast", None) is toast:
                root._saved_toast = None
            try:
                toast.destroy()
            except tk.TclError:
                pass

        if not supports_alpha:
            root.after(1400, destroy_toast)
            return

        def fade(step, total, start, end, then):
            try:
                frac = step / total
                toast.attributes("-alpha", start + (end - start) * frac)
            except tk.TclError:
                then()
                return
            if step >= total:
                then()
            else:
                root.after(15, lambda: fade(step + 1, total, start, end, then))

        def fade_out():
            fade(0, 8, 1.0, 0.0, destroy_toast)

        fade(0, 6, 0.0, 1.0, lambda: root.after(1200, fade_out))
    except tk.TclError:
        pass
