"""
mixxx_window.py

Reads the track on Mixxx's now-playing deck from its main window title
("Artist - Title | Mixxx"). That title follows the same deck Mixxx uses for
its own broadcast metadata. Shared by now_playing.py (dashboard display) and
nowplaying_poller.py (handing the on-air title back to Mixxx).
"""

import psutil
import win32gui
import win32process

MIXXX_PROCESS_NAME = "mixxx.exe"
_TITLE_SUFFIX = " | Mixxx"


def running_pids(process_name):
    return {
        p.pid for p in psutil.process_iter(["name"])
        if (p.info["name"] or "").lower() == process_name
    }


def mixxx_deck_title(mixxx_pids=None):
    """'Artist - Title' of Mixxx's now-playing deck, or None when Mixxx isn't
    running or no deck is playing (the window title is then just "Mixxx")."""
    if mixxx_pids is None:
        mixxx_pids = running_pids(MIXXX_PROCESS_NAME)
    if not mixxx_pids:
        return None

    found = []

    def _enum_handler(hwnd, _):
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid not in mixxx_pids or not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if title.endswith(_TITLE_SUFFIX):
            found.append(title[: -len(_TITLE_SUFFIX)].strip())

    win32gui.EnumWindows(_enum_handler, None)
    return found[0] if found and found[0] else None
