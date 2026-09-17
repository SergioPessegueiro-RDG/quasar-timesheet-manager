"""Single source of truth for this app's own version string.

Bump this to match the tag every time you cut a release (see the
"Cutting a release" section in the README) -- app/update_check.py
compares this against GitHub's latest published release to decide
whether to show the "a new version is available" popup on launch (see
main_window.py's _check_for_updates). A value left behind an old tag
means a false "update available" prompt on the very build that IS the
update; a value bumped ahead of an actual release means a real update
gets missed until the next one ships. Either way, it belongs in the same
commit as the version bump, not a separate one.
"""
APP_VERSION = "2.9.2"
