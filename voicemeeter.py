"""
voicemeeter.py

Shared Voicemeeter Remote API helpers, used by dead_air_monitor.py (strip
levels) and audio_routing.py (strip B-bus routing).
"""

import os
from pathlib import Path

import psutil

# Voicemeeter type -> (hardware strip count, virtual strip count). Strips are
# numbered 0-based, hardware first. The virtual strip count is also the
# number of B buses; the hardware strip count is the number of A buses
# except on basic Voicemeeter (2 hardware strips, A1 only).
VOICEMEETER_LAYOUT = {1: (2, 1), 2: (3, 2), 3: (5, 3)}
A_BUS_COUNT = {1: 1, 2: 3, 3: 5}


def find_voicemeeter_dll():
    """Path to VoicemeeterRemote64.dll ($VOICEMEETER_DLL wins), or None."""
    override = os.getenv("VOICEMEETER_DLL")
    if override:
        return Path(override)

    candidates = []
    # VB-Audio's documented way to locate the install folder.
    try:
        import winreg

        key_path = (
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
            r"\VB:Voicemeeter {17359A74-1236-5467}"
        )
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            uninstall = winreg.QueryValueEx(key, "UninstallString")[0]
        candidates.append(Path(uninstall.strip('"')).parent)
    except OSError:
        pass

    # Fall back to wherever the running Voicemeeter executable lives.
    for proc in psutil.process_iter(["name", "exe"]):
        if (proc.info["name"] or "").lower().startswith("voicemeeter") and proc.info["exe"]:
            candidates.append(Path(proc.info["exe"]).parent)

    candidates.append(Path(r"C:\Program Files (x86)\VB\Voicemeeter"))

    for folder in candidates:
        dll = folder / "VoicemeeterRemote64.dll"
        if dll.is_file():
            return dll
    return None
