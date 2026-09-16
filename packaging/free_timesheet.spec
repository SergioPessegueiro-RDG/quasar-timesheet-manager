# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for a real native QUASAR Timesheet Manager app -- a
double-clickable "QUASAR Timesheet Manager.app" on macOS, "QUASAR Timesheet
Manager.exe" on Windows, or a "QUASAR Timesheet Manager" binary on Linux,
each with Python and Tkinter bundled inside so the end user doesn't need
Python installed at all (unlike the "Start Free Timesheet.*" launcher
scripts one level up, which do require it -- those keep the app's original
file/folder name; only the on-screen branding and this packaged app's own
name changed).

This has to be BUILT ON the same OS you want to run it on -- PyInstaller
bundles the actual interpreter and native libraries of the machine it runs
on, it doesn't cross-compile. Use one of the three build_*.sh/.bat scripts
in this folder (they just wrap the right `pyinstaller` invocation for that
platform), or run it directly:

    pyinstaller --noconfirm packaging/free_timesheet.spec

from the repo root, with PyInstaller installed (`pip install pyinstaller
--break-system-packages` if needed) -- see the "Packaging as a native app"
section in the main README for the full walkthrough per platform.
"""
import importlib.util
import sys
from pathlib import Path

# This file lives in packaging/, one level under the repo root -- resolve
# paths relative to it rather than relying on the current working
# directory, so `pyinstaller packaging/free_timesheet.spec` works the same
# whether it's run from the repo root or from inside packaging/.
PACKAGING_DIR = Path(SPECPATH).resolve()
ROOT = PACKAGING_DIR.parent
ICONS = PACKAGING_DIR / "icons"

_version_spec = importlib.util.spec_from_file_location(
    "app_version", ROOT / "app" / "version.py")
_version_mod = importlib.util.module_from_spec(_version_spec)
_version_spec.loader.exec_module(_version_mod)
APP_VERSION = _version_mod.APP_VERSION

APP_NAME = "QUASAR Timesheet Manager"

block_cipher = None

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(ROOT / "app" / "assets"), "app/assets")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

if sys.platform == "win32":
    icon_path = str(ICONS / "icon.ico")
elif sys.platform == "darwin":
    icon_path = str(ICONS / "icon.icns")
else:
    icon_path = None  # Linux .exe icons aren't embedded this way -- see the
                       # .desktop file's Icon= line for how it's shown there.

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # A GUI app, not a command-line tool -- console=False suppresses the
    # terminal/console window that would otherwise pop up alongside the
    # Tkinter window on Windows (and shows as a background process with no
    # window on macOS/Linux either way).
    console=False,
    icon=icon_path,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=str(ICONS / "icon.icns"),
        bundle_identifier="com.quasartimesheetmanager.app",
        info_plist={
            "NSHighResolutionCapable": True,
            # Finder / About This App read these -- keep them on
            # app/version.py's APP_VERSION so they match Settings.
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion": APP_VERSION,
            "NSHumanReadableCopyright": "Alex Rae and Sérgio Pessegueiro",
        },
    )
