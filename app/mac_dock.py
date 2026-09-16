"""macOS Dock icon: artwork clipped to a squircle on the icon grid.

Tk's iconphoto is a raw square. setApplicationIconImage is the right
size but also a square — macOS only squircles bundled .icns. A custom
NSDockTile contentView can round the corners, but it fills the whole
tile (bigger than neighbors) and fighting applicationIconImage flickers.

The content view is a 128pt canvas with the rounded image inset to the
Dock icon grid. Re-applying the same path is a no-op so launch retries
don't flash.
"""
from __future__ import annotations

import ctypes
import os
import platform
import sys

_TILE_PT = 128.0
# Bundled .icns sit on the icon grid, not edge-to-edge of the tile.
_TILE_SCALE = 0.80
# 22.3% of the *inner* squircle, same ratio as a Mac app icon.
_TILE_CORNER_RATIO = 0.223

_retain = []
_applied_path = None
_applied_view = None


def fill_dock_tile(path: str) -> bool:
    if sys.platform != "darwin" or not path or not os.path.isfile(path):
        return False
    try:
        return _fill(os.path.abspath(path))
    except Exception:
        return False


def _fill(path: str) -> bool:
    global _applied_path, _applied_view

    ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/Foundation.framework/Foundation")
    ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/AppKit.framework/AppKit")
    ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/QuartzCore.framework/QuartzCore")
    libobjc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")

    objc_getClass = libobjc.objc_getClass
    objc_getClass.restype = ctypes.c_void_p
    objc_getClass.argtypes = [ctypes.c_char_p]
    sel_registerName = libobjc.sel_registerName
    sel_registerName.restype = ctypes.c_void_p
    sel_registerName.argtypes = [ctypes.c_char_p]

    def cls(name: str) -> int:
        return objc_getClass(name.encode())

    def sel(name: str) -> int:
        return sel_registerName(name.encode())

    cache = {}

    def msg(restype, *extra):
        key = (restype, extra)
        if key not in cache:
            cache[key] = ctypes.CFUNCTYPE(
                restype, ctypes.c_void_p, ctypes.c_void_p, *extra
            )(("objc_msgSend", libobjc))
        return cache[key]

    msg0 = msg(ctypes.c_void_p)
    msg_id = msg(ctypes.c_void_p, ctypes.c_void_p)
    msg_str = msg(ctypes.c_void_p, ctypes.c_char_p)
    msg_void_id = msg(None, ctypes.c_void_p)
    msg_void_2id = msg(None, ctypes.c_void_p, ctypes.c_void_p)
    msg_void_ul = msg(None, ctypes.c_ulong)
    msg_void_byte = msg(None, ctypes.c_byte)
    msg_int = msg(ctypes.c_void_p, ctypes.c_int)
    msg_bool = msg(ctypes.c_void_p, ctypes.c_byte)
    # NSRect is 4 doubles; on arm64 it is an HFA passed in d0–d3.
    msg_init_frame = msg(
        ctypes.c_void_p,
        ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double)
    msg_set_frame = ctypes.CFUNCTYPE(
        None, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double,
    )(("objc_msgSend", libobjc))

    ns_image = cls("NSImage")
    ns_image_view = cls("NSImageView")
    ns_view = cls("NSView")
    ns_app = cls("NSApplication")
    ns_string = cls("NSString")
    ns_number = cls("NSNumber")
    ns_color = cls("NSColor")
    if not all((ns_image, ns_image_view, ns_view, ns_app, ns_string, ns_number, ns_color)):
        return False

    def nsstr(text: str):
        return msg_str(ns_string, sel("stringWithUTF8String:"), text.encode())

    def kvc(obj, key: str, value):
        msg_void_2id(obj, sel("setValue:forKey:"), value, nsstr(key))

    app = msg0(ns_app, sel("sharedApplication"))
    tile = msg0(app, sel("dockTile")) if app else None
    if not tile:
        return False

    current = msg0(tile, sel("contentView"))
    if path == _applied_path and _applied_view and current == _applied_view:
        return True

    if platform.machine() != "arm64":
        return False

    ns_path = nsstr(path)
    if not ns_path:
        return False

    image = msg0(ns_image, sel("alloc"))
    image = msg_id(image, sel("initWithContentsOfFile:"), ns_path)
    if not image:
        return False
    msg0(image, sel("retain"))

    inner = _TILE_PT * _TILE_SCALE
    pad = (_TILE_PT - inner) / 2.0
    radius_pt = max(1, int(round(inner * _TILE_CORNER_RATIO)))

    container = msg0(ns_view, sel("alloc"))
    container = msg_init_frame(
        container, sel("initWithFrame:"), 0.0, 0.0, _TILE_PT, _TILE_PT)
    if not container:
        return False
    msg0(container, sel("retain"))
    msg_void_byte(container, sel("setWantsLayer:"), 1)
    msg_void_byte(container, sel("setOpaque:"), 0)

    view = msg_id(ns_image_view, sel("imageViewWithImage:"), image)
    if not view:
        return False
    msg0(view, sel("retain"))
    msg_void_ul(view, sel("setImageScaling:"), 1)
    msg_void_byte(view, sel("setWantsLayer:"), 1)
    msg_void_byte(view, sel("setClipsToBounds:"), 1)
    msg_void_byte(view, sel("setOpaque:"), 0)
    msg_set_frame(view, sel("setFrame:"), pad, pad, inner, inner)
    # Keep the inset when the Dock resizes the content view to the tile.
    msg_void_ul(view, sel("setAutoresizingMask:"), 63)

    yes = msg_bool(ns_number, sel("numberWithBool:"), 1)
    radius = msg_int(ns_number, sel("numberWithInt:"), radius_pt)
    for layer_host in (container, view):
        layer = msg0(layer_host, sel("layer"))
        if not layer:
            continue
        kvc(layer, "masksToBounds", yes)
        clear = msg0(ns_color, sel("clearColor"))
        cg = msg0(clear, sel("CGColor")) if clear else None
        if cg:
            msg_void_id(layer, sel("setBackgroundColor:"), cg)
        if layer_host is view:
            kvc(layer, "cornerCurve", nsstr("continuous"))
            kvc(layer, "cornerRadius", radius)

    msg_void_id(container, sel("addSubview:"), view)
    msg_void_id(tile, sel("setContentView:"), container)
    msg0(tile, sel("display"))

    _retain[:] = [image, view, container, yes, radius]
    _applied_path = path
    _applied_view = container
    return True
