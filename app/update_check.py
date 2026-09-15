"""Checks GitHub Releases for a newer published version of this app than
the one currently running, so main_window.py can show a "new version
available" popup on launch.

Only works once the GitHub repo is public -- this app's own bundled
Python has no GitHub auth token to attach, and an unauthenticated request
against a private repo's API just comes back 404, which check_latest_
version() below already treats as "couldn't check" (returns None) rather
than an error. Nothing here ever raises out to the caller: no internet,
DNS failure, a slow connection, GitHub being down, rate limiting, a
private repo, no releases published yet, or a malformed response all
collapse to the same "skip the check this launch" outcome, since this is
a best-effort convenience, never something that should be allowed to
delay or interrupt a normal launch.

Uses only the standard library (urllib) rather than adding `requests` as
a dependency -- this app has stayed dependency-free everywhere else, and
one small, infrequent GET request doesn't need more than that.
"""
import json
import os
import re
import ssl
import time
import urllib.request
from typing import Optional, Tuple

from . import config

# Must match this project's actual GitHub repo (see the remote in `git
# remote -v`) -- there's nowhere else this could be auto-detected from
# inside a packaged, offline-installed copy of the app.
GITHUB_REPO = "SergioPessegueiro-RDG/quasar-timesheet-manager"
_LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_RELEASES_PAGE_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"

# GitHub's API rejects requests with no User-Agent header (403s them) --
# any identifying string satisfies it, this one just also makes this
# app's own traffic recognizable in server logs if that's ever useful.
_HEADERS = {
    "User-Agent": "QUASAR-Timesheet-Manager-update-check",
    "Accept": "application/vnd.github+json",
}


def parse_version(text: str) -> Tuple[int, ...]:
    """"v1.5.0" / "1.5.0" -> (1, 5, 0). Tolerant of a missing "v" prefix
    (GitHub tag names always have one in this project, but the raw
    APP_VERSION string in version.py deliberately doesn't) and of
    anything that isn't a recognizable version at all -- returns (0, 0, 0)
    for those, which is_newer() below then never treats as newer than a
    real version, so a malformed value on either side just disables the
    comparison instead of raising."""
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    if not match:
        return (0, 0, 0)
    return tuple(int(g) for g in match.groups())


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def build_ssl_context() -> ssl.SSLContext:
    """ssl.create_default_context() needs to find a trusted CA bundle to
    verify certificates against. Running from source, Python's own
    install already knows where to look. Inside a PyInstaller-frozen
    macOS build, though, the default context can come up with no CA
    roots at all -- a known PyInstaller/macOS packaging quirk: the
    frozen interpreter loses track of the certificate bundle its own
    bundled OpenSSL wants, even though the exact same code works fine
    unfrozen (confirmed via update_check.log: "[SSL:
    CERTIFICATE_VERIFY_FAILED] ... unable to get local issuer
    certificate" -- see _log_check_failure above).

    macOS ships its own system-wide root CA bundle at /etc/ssl/cert.pem
    regardless of any of that, so when it's present, build the context
    from it explicitly instead of leaving OpenSSL to find its own
    (missing) default. This only takes effect when that path exists --
    everywhere else (a normal source run, Windows, Linux) this is
    exactly ssl.create_default_context()'s own behavior, unchanged.

    Shared with auto_update.py's download_asset(), which hits the exact
    same HTTPS-from-a-frozen-build situation downloading the actual
    release zip."""
    if os.path.exists("/etc/ssl/cert.pem"):
        return ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    return ssl.create_default_context()


def _log_check_failure(exc: Exception) -> None:
    """Appends a one-line, best-effort record of a failed update check to
    a small log file, without ever letting the logging itself raise --
    check_latest_version() has to stay silent-on-failure toward its
    caller (see the module docstring above), but a *completely* silent
    failure is nearly impossible to debug from the outside once this is
    running as a packaged app with no visible console. This log file --
    right next to the app's own database -- is the only place that
    information survives, so a "why didn't the update popup show up"
    report can actually be diagnosed instead of guessed at."""
    try:
        os.makedirs(config.APP_DIR, exist_ok=True)
        log_path = os.path.join(config.APP_DIR, "update_check.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {type(exc).__name__}: {exc}\n")
    except Exception:
        pass  # logging the failure must never itself become a new failure


def check_latest_version(timeout: float = 4.0) -> Optional[dict]:
    """Returns {"tag_name": "v1.6.0", "html_url": "...", "assets": [...]}
    for the latest published GitHub Release, or None if it couldn't be
    determined for any reason. Blocks for up to `timeout` seconds --
    callers (see main_window.py's _check_for_updates) are expected to run
    this off the Tkinter main thread so a slow/unreachable network never
    freezes the UI.

    "assets" is the release's raw list of downloadable files, each a dict
    with (among other things) "name" and "browser_download_url" -- passed
    straight through from GitHub's API, unfiltered, since it's
    auto_update.py's job (not this module's) to pick out the one asset
    that matches the platform this app is running on."""
    request = urllib.request.Request(_LATEST_RELEASE_URL, headers=_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=build_ssl_context()) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        # Deliberately blanket toward the CALLER: see the module
        # docstring -- every failure mode here (no network, a 404 from a
        # still-private repo or a repo with no releases yet, a timeout,
        # malformed JSON, ...) should be indistinguishable to
        # main_window.py from "nothing to report". _log_check_failure
        # still records exactly what happened, for diagnosing that from
        # outside a packaged build with no console.
        _log_check_failure(exc)
        return None

    tag_name = data.get("tag_name")
    if not tag_name:
        return None
    return {
        "tag_name": tag_name,
        "html_url": data.get("html_url") or _RELEASES_PAGE_URL,
        "assets": data.get("assets") or [],
    }
