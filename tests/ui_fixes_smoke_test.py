"""
Headless smoke test (Xvfb) for four specific UI fixes:
  1. Every dialog that used to be a pop-up window (Time Block, Duplicate,
     Activity, Project, Settings, Export) is now an embedded tab in the
     main window, so each always appears in the same place (inside the
     window itself) regardless of where the main window is sitting or what
     the window manager does with pop-ups -- and only one such tab is ever
     shown at a time, alongside "Timesheet".
  2. The Activities sidebar can be dragged wider via the sash so long
     QDM names stay readable; widening the window itself leaves the
     sidebar at the chosen width and gives extra space to the calendar.
  3. Nothing at the bottom of the calendar (hour labels, daily totals) is
     clipped -- there's real pixel room below the last gridline/row.
  4. The Activities sidebar's scrollbar actually scrolls, including via the
     mouse wheel, once enough activities/projects overflow the visible area
     -- and the scrollbar itself is a hand-drawn _VectorScrollbar (a real
     ttk.Scrollbar was found not to render reliably here), so its thumb
     drawing, click-to-page, drag, and hover behavior are covered too.
"""
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tmp_home = tempfile.mkdtemp()
os.environ["HOME"] = tmp_home

from app.main_window import MainWindow  # noqa: E402
from app import config, theme  # noqa: E402
from app.models import Activity, Project  # noqa: E402
from app.widgets import VectorScrollbar  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


print("--- Main window off in a corner (simulates the user's reported setup) ---")
win = MainWindow()
win.update()
win.geometry("1100x700+20+20")  # tucked in the top-left corner of the screen
win.update()

print("\n--- Time block editor is a tab in the main window, not a pop-up ---")
# This is the fix for the bug that centering couldn't fully solve: on some
# macOS/Tk setups a pop-up Toplevel can ignore geometry() and open off in a
# corner regardless of what the app asks for. Making it an embedded tab
# sidesteps the problem entirely -- there's no separate window to place.
win.calendar._open_entry_dialog(new=True, day_idx=0,
                                 start_hhmm="09:00", end_hhmm="10:00")
win.update()
check("Time Block tab becomes visible", str(win.notebook.tab(win.timeblock_panel, "state")) == "normal")
check("Time Block tab is selected", win.notebook.select() == str(win.timeblock_panel))
check("The panel is a child of the main window's own notebook (no pop-up Toplevel involved)",
      str(win.timeblock_panel.winfo_toplevel()) == str(win))
win.timeblock_panel._cancel()
win.update()
check("Time Block tab hides again after Cancel",
      str(win.notebook.tab(win.timeblock_panel, "state")) == "hidden")

print("\n--- The Template tab is permanent, unlike the other tabs ---")
check("Template tab exists next to Timesheet", "Template" in win.notebook.tab(win.template_calendar.master, "text")
      or any(win.notebook.tab(t, "text") == "Template" for t in win.notebook.tabs()))
check("Template tab starts in the normal (visible) state",
      str(win.notebook.tab(win.template_calendar.master, "state")) == "normal")

print("\n--- Duplicate, Activity, Project, Settings, and Export are ALSO tabs, not pop-ups ---")
from app.models import TimeEntry  # noqa: E402

seed_entry = TimeEntry(None, None, "Seed Block", None, "#4C6EF5",
                        win.calendar.day_date(0).isoformat(), "09:00", "10:00", "")
win.db.add_time_entry(seed_entry)
win.calendar.refresh()
win.update()
saved_entry = win.db.list_time_entries_for_week(
    [win.calendar.day_date(i).isoformat() for i in range(5)])[0]

win.calendar._open_duplicate_dialog(saved_entry)
win.update()
check("Duplicate tab is a child of the main window's own notebook (no pop-up Toplevel)",
      str(win.duplicate_panel.winfo_toplevel()) == str(win))
check("Duplicate tab becomes visible and selected",
      str(win.notebook.tab(win.duplicate_panel, "state")) == "normal"
      and win.notebook.select() == str(win.duplicate_panel))

win._open_settings_dialog()
win.update()
# Settings is a permanent tab now (like Timesheet/Template/Summary), not
# one of the hide-until-opened panels _show_panel manages -- opening it
# just selects it, and doesn't hide whatever other single extra panel
# (Duplicate, here) happened to be open.
check("Opening Settings does not hide the still-open Duplicate tab (Settings isn't part of that single-extra-tab mechanism)",
      str(win.notebook.tab(win.duplicate_panel, "state")) == "normal")
check("Settings tab is selected",
      str(win.notebook.tab(win.settings_panel, "state")) == "normal"
      and win.notebook.select() == str(win.settings_panel))
win.settings_panel._cancel()
win.update()
check("Settings tab stays present (it's permanent) after Cancel -- Cancel just navigates back to Timesheet",
      str(win.notebook.tab(win.settings_panel, "state")) == "normal"
      and win.notebook.select() == str(win.timesheet_tab))

win._open_export_dialog()
win.update()
check("Export tab becomes visible and selected",
      str(win.notebook.tab(win.export_panel, "state")) == "normal"
      and win.notebook.select() == str(win.export_panel))
win.export_panel._cancel()
win.update()
check("Export tab hides again after Cancel",
      str(win.notebook.tab(win.export_panel, "state")) == "hidden")

win.sidebar._add_activity()
win.update()
check("Activity tab becomes visible and selected",
      str(win.notebook.tab(win.activity_panel, "state")) == "normal"
      and win.notebook.select() == str(win.activity_panel))
win.activity_panel._cancel()
win.update()
check("Activity tab hides again after Cancel",
      str(win.notebook.tab(win.activity_panel, "state")) == "hidden")
check("Back to the Timesheet tab after closing everything",
      win.notebook.index(win.notebook.select()) == 0)
check("Template tab stayed visible the whole time (never auto-hidden like the other panels)",
      str(win.notebook.tab(win.template_calendar.master, "state")) == "normal")

print("\n--- Sidebar width is user-chosen (sash), not window-proportional ---")
narrow_width = win.sidebar.winfo_width()
check(f"Sidebar respects the minimum width at 1100px window (got {narrow_width})",
      narrow_width >= config.MIN_SIDEBAR_WIDTH_PX - 2)

win.geometry("1800x900+20+20")
win.update()
wide_width = win.sidebar.winfo_width()
check(f"Sidebar stays put when the window is widened (narrow={narrow_width}, wide={wide_width})",
      abs(wide_width - narrow_width) < 40)

win.sidebar_width = min(config.MAX_SIDEBAR_WIDTH_PX, int(narrow_width) + 120)
win._apply_sidebar_column_width()
win.update()
dragged = win.sidebar.winfo_width()
check(f"Widening via the sash grows the sidebar (was {narrow_width}, now {dragged})",
      dragged > narrow_width + 40)

calendar_narrow_dw = win.calendar.day_width
win.update()
check("Calendar day columns still get most of the extra space (day_width > MIN)",
      win.calendar.day_width >= config.MIN_DAY_WIDTH_PX)

# The activity rows inside the sidebar's scrollable list should stretch to
# the sidebar's own width too (this was the literal "doesn't fit into the
# box" bug -- row highlight backgrounds stopping short of the right edge).
win.sidebar._activities = win.sidebar.db.list_activities()
win.sidebar._projects = win.sidebar.db.list_projects()
win.sidebar._render_rows()
win.update()
rows = [w for w in win.sidebar.list_frame.winfo_children() if w.winfo_class() == "Frame"]
if rows:
    row_w = rows[0].winfo_width()
    canvas_w = [c for c in win.sidebar.winfo_children()][0]
    check(f"An activity row stretches close to the sidebar's width (row={row_w}, sidebar={dragged})",
          row_w >= dragged - 60)

print("\n--- Activities sidebar scrolls (scrollbar + mouse wheel) once it overflows ---")
# Shrink back down and pile in enough projects/activities that the list
# can't possibly fit in view, then confirm both the scrollbar and the mouse
# wheel actually move it -- this is what "add a scrollbar so I can see all
# of them without collapsing projects" means in practice.
win.geometry("1100x700+20+20")
win.update()

db = win.sidebar.db
for i in range(3):
    pid = db.add_project(Project(None, f"Scroll Project {i}", "#4C6EF5"))
    for j in range(4):
        db.add_activity(Activity(None, f"Scroll Activity {i}-{j}", None, 30, project_id=pid))
win.sidebar.refresh()
win.update()

sidebar_canvas = win.sidebar.canvas
bbox = sidebar_canvas.bbox("all")
check("Sidebar content overflows the visible list area (enough rows to need scrolling)",
      bbox is not None and bbox[3] > sidebar_canvas.winfo_height())

top_before = sidebar_canvas.yview()[0]
check("Sidebar starts scrolled to the top", top_before == 0.0)


def _root_center(widget):
    return (widget.winfo_rootx() + widget.winfo_width() // 2,
            widget.winfo_rooty() + widget.winfo_height() // 2)


# Scrolling now lives on the sidebar's ScrollArea (app/widgets.py), not on
# Sidebar itself -- see win.sidebar.list_container. Its dispatcher
# (_on_wheel_anywhere) does its own geometric hit-test via winfo_containing
# on the event's root coordinates, so these synthetic events need real
# rootx/rooty over the sidebar's canvas to be recognized as "over the list"
# at all (see the longer comment on this same requirement further below).
sidebar_rx, sidebar_ry = _root_center(sidebar_canvas)

# Mouse wheel, Windows/macOS-style event (signed .delta, no useful .num).
wheel_down = types.SimpleNamespace(delta=-120, num=0, x_root=sidebar_rx, y_root=sidebar_ry)
win.sidebar.list_container._on_wheel_anywhere(wheel_down)
win.update()
top_after_wheel = sidebar_canvas.yview()[0]
check("Mouse wheel (Windows/macOS-style <MouseWheel>) scrolls the sidebar down",
      top_after_wheel > top_before)

wheel_up = types.SimpleNamespace(delta=120, num=0, x_root=sidebar_rx, y_root=sidebar_ry)
win.sidebar.list_container._on_wheel_anywhere(wheel_up)
win.update()
check("Mouse wheel scrolling the other way moves it back up",
      sidebar_canvas.yview()[0] < top_after_wheel)

# X11-style wheel events (<Button-4>/<Button-5>, no .delta).
x11_down = types.SimpleNamespace(delta=0, num=5, x_root=sidebar_rx, y_root=sidebar_ry)
win.sidebar.list_container._on_wheel_anywhere(x11_down)
win.update()
check("X11-style wheel-down (<Button-5>) also scrolls the sidebar",
      sidebar_canvas.yview()[0] > top_before)

# The scrollbar itself (not just the wheel) should move the same view.
sidebar_canvas.yview_moveto(0)
win.update()
sidebar_canvas.yview_scroll(3, "units")
win.update()
check("Scrolling the canvas view directly (what the scrollbar drives) also moves it",
      sidebar_canvas.yview()[0] > 0.0)

# The wheel has to work no matter which specific widget is directly under
# the pointer -- in practice that's almost always a deeply nested Label
# inside a row, not bare canvas. Two earlier designs were tried and both
# had real, diagnosed flaws:
#   1. Arming/disarming a bind_all binding on the canvas's own
#      <Enter>/<Leave> -- broken because list_frame is embedded *inside*
#      the canvas via create_window, making it a real child X window, so
#      moving onto it (or any row/label nested inside it) delivered a
#      LeaveNotify to the canvas and tore the binding back down almost
#      immediately. It never actually worked while hovering the list.
#   2. Binding <MouseWheel>/<Button-4>/<Button-5> directly on every widget
#      in the scrollable tree (recursively, re-bound on every render) --
#      this worked under X11 in testing, but the user reported it still
#      didn't scroll in real usage, on the theory that not every platform
#      necessarily routes a native wheel gesture to "whichever widget is
#      literally under the pointer" the way X11 Button-4/5 clicks do.
# The current design (#3) sidesteps that uncertainty entirely: a single
# bind_all registration (see Sidebar.__init__) fires on ANY wheel event
# delivered ANYWHERE in the app, and the handler (_on_mousewheel_anywhere)
# does its own geometric hit-test via winfo_containing(event.x_root,
# event.y_root) rather than trusting event.widget. That means a test has
# to populate real root-screen coordinates on the synthetic event for the
# hit-test to have anything meaningful to check -- event_generate() only
# fills in x_root/y_root when explicitly given -rootx/-rooty (confirmed
# empirically: without them, Tk leaves x_root/y_root at -1, which is
# exactly why the *previous* version of this test block -- which called
# event_generate() with no rootx/rooty -- would have silently exercised
# nothing once the mechanism changed).
def _find_deepest_label(widget, depth=0):
    best, best_depth = (widget, depth) if widget.winfo_class() == "Label" else (None, depth)
    for child in widget.winfo_children():
        found, found_depth = _find_deepest_label(child, depth + 1)
        if found is not None and found_depth > best_depth:
            best, best_depth = found, found_depth
    return best, best_depth


# Only search inside the FIRST row: winfo_containing() only finds
# widgets that are actually visible on screen right now, and a deeply
# nested widget from a row further down the list would be scrolled out of
# the canvas's clipped viewport (X clips a canvas's embedded child window
# to the canvas's own bounds, same as any parent/child window pair) --
# querying a point over an off-screen row finds nothing there, which
# isn't what this check is trying to prove. The first row is guaranteed
# visible once the canvas is scrolled to the top, below.
sidebar_canvas.yview_moveto(0)
win.update()
first_row = win.sidebar.list_frame.winfo_children()[0] if win.sidebar.list_frame.winfo_children() else None
deep_widget, deep_depth = _find_deepest_label(first_row, depth=1) if first_row is not None else (None, 0)
check("Found a deeply nested widget inside the scrollable list to test against",
      deep_widget is not None and deep_depth >= 2)

deep_rx, deep_ry = _root_center(deep_widget)

sidebar_canvas.yview_moveto(0)
win.update()
deep_widget.event_generate("<MouseWheel>", delta=-120, rootx=deep_rx, rooty=deep_ry)
win.update()
check("A real MouseWheel event delivered to a deeply nested row widget scrolls the sidebar",
      sidebar_canvas.yview()[0] > 0.0)

sidebar_canvas.yview_moveto(0)
win.update()
deep_widget.event_generate("<Button-5>", rootx=deep_rx, rooty=deep_ry)
win.update()
check("A real X11-style Button-5 wheel event on the same nested widget also scrolls it",
      sidebar_canvas.yview()[0] > 0.0)

# The scrollbar itself should also respond directly to the wheel.
sidebar_canvas.yview_moveto(0)
win.update()
sidebar_scrollbar_for_wheel = None
for _child in win.sidebar.canvas.master.winfo_children():
    if _child.winfo_class() != "Canvas" or _child is sidebar_canvas:
        continue
    sidebar_scrollbar_for_wheel = _child
check("Found the scrollbar widget to test wheel-over-scrollbar behavior",
      sidebar_scrollbar_for_wheel is not None)
if sidebar_scrollbar_for_wheel is not None:
    sb_rx, sb_ry = _root_center(sidebar_scrollbar_for_wheel)
    sidebar_scrollbar_for_wheel.event_generate("<MouseWheel>", delta=-120, rootx=sb_rx, rooty=sb_ry)
    win.update()
    check("Wheel events over the scrollbar itself also scroll the sidebar",
          sidebar_canvas.yview()[0] > 0.0)

# And the hit-test should be a real geometric test, not a rubber stamp --
# a wheel event whose root coordinates land somewhere else entirely (e.g.
# over the calendar grid) must NOT scroll the sidebar.
check("ScrollArea._is_within is true for a widget actually inside the list",
      win.sidebar.list_container._is_within(deep_widget) is True)
check("ScrollArea._is_within is false for a widget outside the list (the calendar canvas)",
      win.sidebar.list_container._is_within(win.calendar.canvas) is False)

sidebar_canvas.yview_moveto(0)
win.update()
cal_rx, cal_ry = _root_center(win.calendar.canvas)
win.calendar.canvas.event_generate("<MouseWheel>", delta=-120, rootx=cal_rx, rooty=cal_ry)
win.update()
check("A wheel event over the calendar (not the sidebar list) leaves the sidebar's scroll position alone",
      sidebar_canvas.yview()[0] == 0.0)

print("\n--- Custom vector scrollbar (ttk.Scrollbar doesn't render reliably here) ---")
# ttk.Scrollbar's "clam"-theme thumb was found to not paint at all in this
# app's headless/Xvfb environment, even with correct pack ordering and
# explicit style colors/width -- verified with an isolated bare
# Canvas+ttk.Scrollbar reproduction that showed zero visible pixels.
# VectorScrollbar (a small hand-drawn Canvas widget, now shared across the
# whole app via app/widgets.py) replaces it. These checks cover both that
# it actually draws a thumb, and that clicking, dragging, and hovering it
# behave like a real scrollbar would.
sidebar_scrollbar = None
for _child in win.sidebar.canvas.master.winfo_children():
    if isinstance(_child, VectorScrollbar):
        sidebar_scrollbar = _child
        break
check("The sidebar's scrollable list has a VectorScrollbar sibling",
      sidebar_scrollbar is not None)

sidebar_canvas.yview_moveto(0)
win.update()
top, bottom = sidebar_scrollbar._thumb_bounds()
check("The thumb has real, drawable extent (not collapsed to zero height)",
      bottom > top)
thumb_items = [i for i in sidebar_scrollbar.find_all()
               if sidebar_scrollbar.itemcget(i, "fill") == theme.BORDER_STRONG]
check("A thumb rectangle is actually drawn in the scrollbar's own border color",
      len(thumb_items) >= 1)

sidebar_scrollbar._on_click(types.SimpleNamespace(y=bottom + 20))
win.update()
check("Clicking the track below the thumb pages the sidebar down",
      sidebar_canvas.yview()[0] > 0.0)

print("\n--- Scrollbar up/down arrow buttons (a wheel-independent fallback) ---")
# These exist because the mouse *wheel* binding has been through three
# designs and at least two still didn't work for a real user on macOS --
# a plain click on a button doesn't depend on wheel events at all, so it's
# a guaranteed way to scroll regardless of whatever platform/Tk quirk is
# behind that. y=0 and y=height-1 now land inside the arrow hit zones
# (ARROW_H from each end), not the open track, so clicking there steps one
# unit instead of jumping to that track position -- confirm the hit-test
# and the resulting scroll amount are both right.
sidebar_canvas.yview_moveto(0.5)
win.update()
check("_which_arrow identifies the top strip as the up arrow",
      sidebar_scrollbar._which_arrow(0) == "up")
check("_which_arrow identifies the bottom strip as the down arrow",
      sidebar_scrollbar._which_arrow(sidebar_scrollbar.winfo_height() - 1) == "down")
check("_which_arrow returns nothing for the open track in the middle",
      sidebar_scrollbar._which_arrow(sidebar_scrollbar.winfo_height() / 2) is None)

before_arrow_click = sidebar_canvas.yview()[0]
sidebar_scrollbar._on_click(types.SimpleNamespace(y=sidebar_scrollbar.winfo_height() - 1))
win.update()
after_down_click = sidebar_canvas.yview()[0]
check("Clicking the down arrow scrolls down by one small step (not a jump)",
      0 < after_down_click - before_arrow_click < 0.15)
check("Clicking an arrow starts a press-and-hold repeat timer",
      sidebar_scrollbar._repeat_job is not None)
sidebar_scrollbar._on_release(types.SimpleNamespace(y=sidebar_scrollbar.winfo_height() - 1))
win.update()
check("Releasing the arrow cancels the repeat timer",
      sidebar_scrollbar._repeat_job is None)

sidebar_scrollbar._on_click(types.SimpleNamespace(y=0))
win.update()
after_up_click = sidebar_canvas.yview()[0]
check("Clicking the up arrow scrolls back up by one small step",
      after_up_click < after_down_click)
sidebar_scrollbar._on_release(types.SimpleNamespace(y=0))

sidebar_canvas.yview_moveto(0)
win.update()
top2, bottom2 = sidebar_scrollbar._thumb_bounds()
sidebar_scrollbar._dragging = True
sidebar_scrollbar._drag_offset = 5
sidebar_scrollbar._on_drag(types.SimpleNamespace(y=int(top2) + 5 + 150))
win.update()
check("Dragging the thumb moves the sidebar's view",
      sidebar_canvas.yview()[0] > 0.0)
sidebar_scrollbar._on_release()
check("Releasing the drag clears the dragging flag", sidebar_scrollbar._dragging is False)

sidebar_scrollbar._on_enter()
win.update()
hover_colors = {sidebar_scrollbar.itemcget(i, "fill") for i in sidebar_scrollbar.find_all()}
check("Hovering the scrollbar highlights the thumb in the accent color",
      theme.ACCENT in hover_colors)
sidebar_scrollbar._on_leave()
win.update()
leave_colors = {sidebar_scrollbar.itemcget(i, "fill") for i in sidebar_scrollbar.find_all()}
check("Moving off the scrollbar reverts the thumb color",
      theme.ACCENT not in leave_colors)

print("\n--- Calendar bottom padding: nothing is clipped ---")
canvas = win.calendar.canvas
canvas_h = canvas.winfo_height()
bbox = canvas.bbox("all")
check("Canvas has drawn content", bbox is not None)
if bbox:
    content_bottom = bbox[3]
    check(f"Drawn content bottom ({content_bottom}) fits within the canvas height ({canvas_h})",
          content_bottom <= canvas_h)

gutter_h = win.calendar._totals_host.winfo_height()
check(f"Totals canvas has real height, not collapsed (got {gutter_h}px)",
      gutter_h >= config.TOTALS_ROW_HEIGHT_PX - 2)
total_items = win.calendar._totals_host.find_withtag("total")
check(f"Totals canvas drew a label for each day (got {len(total_items)})",
      len(total_items) >= len(config.DAY_NAMES))

win.destroy()

print("\n============================")
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
