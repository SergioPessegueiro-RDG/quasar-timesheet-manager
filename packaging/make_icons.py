#!/usr/bin/env python3
"""
Turns packaging/icons/source.png into the committed app-icon files.

The in-window header loads app/assets/logo.png (see theme.draw_logo_mark).
The Dock / .exe / Linux icon uses icon.png + icon.ico + icon.icns.
app/assets/app_icon.png is the full-bleed mark (no extra frame). Packaged
Mac builds use the .icns; running from source fills the Dock tile so
macOS clips it to the system squircle.

Replace source.png with a new square PNG, then:

    python3 packaging/make_icons.py

Pillow is only needed for this script. Homebrew Python often won't let
you `pip install` it globally, so the first run creates
packaging/.icons-venv and installs Pillow there.
"""
import io
import os
import shutil
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "icons")
SOURCE = os.path.join(OUT_DIR, "source.png")
HEADER_LOGO = os.path.join(HERE, "..", "app", "assets", "logo.png")
APP_ICON = os.path.join(HERE, "..", "app", "assets", "app_icon.png")
VENV_DIR = os.path.join(HERE, ".icons-venv")


def _venv_python() -> str:
    if sys.platform == "win32":
        return os.path.join(VENV_DIR, "Scripts", "python.exe")
    return os.path.join(VENV_DIR, "bin", "python")


def _ensure_pillow():
    try:
        import PIL  # noqa: F401
        return
    except ImportError:
        pass
    venv_py = _venv_python()
    if os.path.abspath(sys.executable) == os.path.abspath(venv_py):
        raise SystemExit("Pillow is missing even inside packaging/.icons-venv")
    print("Pillow isn't installed for this Python -- using packaging/.icons-venv", flush=True)
    if not os.path.isfile(venv_py):
        subprocess.check_call([sys.executable, "-m", "venv", VENV_DIR])
    subprocess.check_call([venv_py, "-m", "pip", "install", "-q", "pillow"])
    os.execv(venv_py, [venv_py, os.path.abspath(__file__), *sys.argv[1:]])


_ensure_pillow()
from PIL import Image  # noqa: E402

ICONSET_FILES = (
    (16, "icon_16x16.png"),
    (32, "icon_16x16@2x.png"),
    (32, "icon_32x32.png"),
    (64, "icon_32x32@2x.png"),
    (128, "icon_128x128.png"),
    (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"),
    (512, "icon_256x256@2x.png"),
    (512, "icon_512x512.png"),
    (1024, "icon_512x512@2x.png"),
)


def _write_icns_png_chunks(png_1024: Image.Image, path: str):
    """Hand-assembles a modern (PNG-payload) .icns file when iconutil is
    not available (Linux/Windows)."""
    sizes = [
        (b"icp4", 16), (b"icp5", 32), (b"icp6", 64),
        (b"ic07", 128), (b"ic08", 256), (b"ic09", 512), (b"ic10", 1024),
    ]
    chunks = []
    for type_code, edge in sizes:
        resized = png_1024.resize((edge, edge), Image.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="PNG")
        data = buf.getvalue()
        chunk = type_code + struct.pack(">I", 8 + len(data)) + data
        chunks.append(chunk)

    body = b"".join(chunks)
    total_len = 8 + len(body)
    with open(path, "wb") as f:
        f.write(b"icns" + struct.pack(">I", total_len) + body)


def write_icns(png_1024: Image.Image, path: str):
    """Prefer `iconutil` on macOS so Finder/Dock get a real iconset .icns
    (the system then applies the squircle). Fall back to PNG chunks."""
    iconutil = shutil.which("iconutil")
    if iconutil:
        with tempfile.TemporaryDirectory() as tmp:
            iconset = os.path.join(tmp, "icon.iconset")
            os.makedirs(iconset)
            for edge, name in ICONSET_FILES:
                png_1024.resize((edge, edge), Image.LANCZOS).save(
                    os.path.join(iconset, name))
            subprocess.check_call([iconutil, "-c", "icns", iconset, "-o", path])
        return
    _write_icns_png_chunks(png_1024, path)


def _square_rgba(path: str, edge: int) -> Image.Image:
    img = Image.open(path).convert("RGBA")
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    if img.size != (edge, edge):
        img = img.resize((edge, edge), Image.LANCZOS)
    return img


def main():
    if not os.path.isfile(SOURCE):
        raise SystemExit(
            f"Missing {SOURCE}\n"
            "Drop a square PNG there, then re-run this script.")

    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(HEADER_LOGO), exist_ok=True)

    master = _square_rgba(SOURCE, 1024)
    master.save(os.path.join(OUT_DIR, "icon.png"))

    ico_sizes = [16, 24, 32, 48, 64, 128, 256]
    master.save(
        os.path.join(OUT_DIR, "icon.ico"),
        sizes=[(s, s) for s in ico_sizes],
    )
    write_icns(master, os.path.join(OUT_DIR, "icon.icns"))

    _square_rgba(SOURCE, 128).save(HEADER_LOGO)
    _square_rgba(SOURCE, 512).save(APP_ICON)

    print(f"Wrote icon.png, icon.ico, icon.icns to {OUT_DIR}")
    print(f"Wrote header logo to {os.path.normpath(HEADER_LOGO)}")
    print(f"Wrote Dock icon to {os.path.normpath(APP_ICON)}")


if __name__ == "__main__":
    main()
