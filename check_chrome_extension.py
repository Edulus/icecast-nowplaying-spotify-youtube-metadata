"""Checks that the Now Playing Reporter Chrome extension is installed and enabled.

Chrome updates have been seen to drop unpacked extensions, and with the
extension gone the dashboard just shows "No word from the Chrome extension".
This reads Chrome's profile preferences (Chrome need not be closed) and looks
for an unpacked extension loaded from this project's chrome-extension folder.

Exit code: 0 = installed and enabled, 1 = missing or disabled, 2 = Chrome's
profile folder could not be found.
"""

import json
import os
import sys
from pathlib import Path

EXTENSION_DIR = Path(__file__).resolve().parent / "chrome-extension"
PREFS_FILES = ("Secure Preferences", "Preferences")


def _chrome_user_data():
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    path = Path(local) / "Google" / "Chrome" / "User Data"
    return path if path.is_dir() else None


def _same_path(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(str(b)))


def _find_extension(user_data):
    """Return a list of (profile, enabled) for every profile that has it."""
    found = {}
    for profile in user_data.iterdir():
        if not profile.is_dir() or not (profile.name == "Default" or profile.name.startswith("Profile ")):
            continue
        for name in PREFS_FILES:
            prefs = profile / name
            if not prefs.is_file():
                continue
            try:
                settings = json.loads(prefs.read_text(encoding="utf-8")).get("extensions", {}).get("settings", {})
            except (OSError, ValueError):
                continue
            for entry in settings.values():
                path = entry.get("path")
                if path and _same_path(path, EXTENSION_DIR):
                    # state 0 / disable_reasons mean the user (or Chrome) turned it off.
                    enabled = entry.get("state", 1) == 1 and not entry.get("disable_reasons")
                    found[profile.name] = found.get(profile.name, False) or enabled
    return list(found.items())


def main():
    user_data = _chrome_user_data()
    if user_data is None:
        print("[WARN] Chrome profile folder not found; cannot check the YouTube extension.")
        return 2

    found = _find_extension(user_data)
    if any(enabled for _, enabled in found):
        print("Chrome extension OK: Now Playing Reporter is installed and enabled.")
        return 0

    if found:
        print("[WARN] The Now Playing Reporter Chrome extension is installed but DISABLED.")
        print("       Turn it on at chrome://extensions.")
    else:
        print("[WARN] The Now Playing Reporter Chrome extension is NOT installed in Chrome.")
        print("       YouTube titles will not reach the stream until it is loaded:")
        print("         1. Open chrome://extensions and turn on Developer mode")
        print("         2. Click 'Load unpacked' and choose:")
        print(f"            {EXTENSION_DIR}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
