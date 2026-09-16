"""macOS Dock icon: full-bleed artwork clipped to the system squircle.

Tk's iconphoto is a raw square. Transparent pre-masked PNGs get the
legacy inset (too small) and a black plate shows as a frame. macOS 27
also does not clip a Dock-tile content view for us, so this rounds that
view itself with CALayer's continuous corner curve -- the same shape as
other Mac app icons -- and fills the tile.
"""
from __future__ import annotations

import ctypes
import os
import sys

# Standard NSDockTile is 128pt. 22.3% is the Mac app-icon corner.
# Do not use the PNG pixel size here: macOS 27 resizes the content view
# to the tile, so a 512px-derived radius cuts an X into the icon.
_TILE_CORNER = 28

_retain = []  # ObjC objects the Dock tile keeps drawing


def fill_dock_tile(path: str) -> bool:
    if sys.platform != "darwin" or not path or not os.path.isfile(path):
        return False
    try:
        return _fill(os.path.abspath(path))
    except Exception:
        return False


def _fill(path: str) -> bool:
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

    ns_image = cls("NSImage")
    ns_view = cls("NSImageView")
    ns_app = cls("NSApplication")
    ns_string = cls("NSString")
    ns_number = cls("NSNumber")
    if not all((ns_image, ns_view, ns_app, ns_string, ns_number)):
        return False

    def nsstr(text: str):
        return msg_str(ns_string, sel("stringWithUTF8String:"), text.encode())

    def kvc(obj, key: str, value):
        msg_void_2id(obj, sel("setValue:forKey:"), value, nsstr(key))

    ns_path = nsstr(path)
    if not ns_path:
        return False

    image = msg0(ns_image, sel("alloc"))
    image = msg_id(image, sel("initWithContentsOfFile:"), ns_path)
    if not image:
        return False
    msg0(image, sel("retain"))

    view = msg_id(ns_view, sel("imageViewWithImage:"), image)
    if not view:
        return False
    msg0(view, sel("retain"))
    # Fill the tile, don't letterbox.
    msg_void_ul(view, sel("setImageScaling:"), 1)
    msg_void_byte(view, sel("setWantsLayer:"), 1)
    msg_void_byte(view, sel("setClipsToBounds:"), 1)

    layer = msg0(view, sel("layer"))
    if not layer:
        return False
    yes = msg_bool(ns_number, sel("numberWithBool:"), 1)
    radius = msg_int(ns_number, sel("numberWithInt:"), _TILE_CORNER)
    kvc(layer, "masksToBounds", yes)
    kvc(layer, "cornerCurve", nsstr("continuous"))
    kvc(layer, "cornerRadius", radius)

    app = msg0(ns_app, sel("sharedApplication"))
    tile = msg0(app, sel("dockTile"))
    if not tile:
        return False
    msg_void_id(tile, sel("setContentView:"), view)
    msg0(tile, sel("display"))

    _retain[:] = [image, view, layer, yes, radius]
    return True
