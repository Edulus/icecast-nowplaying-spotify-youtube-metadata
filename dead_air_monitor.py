"""
dead_air_monitor.py

Piece 1 of the dead-air safety system.

Reports four states:

    AUDIO OK
    SILENCE
    STREAM OFFLINE
    LEVEL UNAVAILABLE

Two independent checks feed them:

  * Audio level -- read locally from Voicemeeter's meter for the strip that
    Mixxx's Main output plays into (the Voicemeeter AUX Input by default).
    The pre-fader input level is used, so it tracks Mixxx's master mix, which
    is exactly what Mixxx's Live Broadcasting encodes, regardless of that
    strip's fader or mute. SILENCE is declared only after the level stays at
    or below SILENCE_THRESHOLD_DBFS continuously for DEAD_AIR_SECONDS.

  * Stream presence -- read from the Caster.fm/Icecast server's public stats
    (see caster_stats.py). STREAM OFFLINE means the server is down or nothing
    is streaming to our mount.

Why not decode the listener stream: Caster.fm's free plan only serves the
direct stream with a per-IP listener token issued to its embedded players, so
a script can't open it reliably (401 Unauthorized).

This script takes no recovery or notification action yet. Later pieces can
subscribe to these state changes without changing the audio detector.
"""

import ctypes
import logging
import math
import os
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

import caster_stats
from single_instance import take_over
from status_writer import write_status

load_dotenv()

DEAD_AIR_SECONDS = float(os.getenv("DEAD_AIR_SECONDS", "30"))
SILENCE_THRESHOLD_DBFS = float(os.getenv("SILENCE_THRESHOLD_DBFS", "-60"))
STATUS_FILE = os.getenv("DEAD_AIR_STATUS_FILE", "dead_air_status.json")

# Seconds between Caster.fm stats checks for the STREAM OFFLINE state.
STREAM_CHECK_INTERVAL = float(os.getenv("DEAD_AIR_STREAM_CHECK_SECONDS", "5"))

# Voicemeeter strip Mixxx's Main output plays into, 0-based across all
# strips (hardware strips first, then virtual). On Voicemeeter Potato the
# strips are 5 hardware + VAIO (5), AUX (6), VAIO3 (7), so AUX is 6.
VOICEMEETER_STRIP = int(os.getenv("DEAD_AIR_VOICEMEETER_STRIP", "6"))

# Optional full path to VoicemeeterRemote64.dll; found automatically if blank.
VOICEMEETER_DLL = os.getenv("VOICEMEETER_DLL")

# Meter sampling. Levels are peak readings, so the loudest sample in each
# window is used: a window is "quiet" only if nothing in it rose above the
# threshold.
SAMPLE_SECONDS = 0.05
WINDOW_SECONDS = 1.0

# Voicemeeter type -> (hardware strip count, virtual strip count). Hardware
# strips have 2 meter channels each, virtual strips 8.
_VOICEMEETER_LAYOUT = {1: (2, 1), 2: (3, 2), 3: (5, 3)}
_LEVEL_PRE_FADER_INPUT = 0

LOG_FILE = "dead_air_monitor.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger(__name__)


def to_dbfs(linear):
    return 20.0 * math.log10(linear) if linear > 0 else float("-inf")


def find_voicemeeter_dll():
    if VOICEMEETER_DLL:
        return Path(VOICEMEETER_DLL)

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
    try:
        import psutil

        for proc in psutil.process_iter(["name", "exe"]):
            if (proc.info["name"] or "").lower().startswith("voicemeeter") and proc.info["exe"]:
                candidates.append(Path(proc.info["exe"]).parent)
    except ImportError:
        pass

    candidates.append(Path(r"C:\Program Files (x86)\VB\Voicemeeter"))

    for folder in candidates:
        dll = folder / "VoicemeeterRemote64.dll"
        if dll.is_file():
            return dll
    return None


class VoicemeeterMeter:
    """Reads one strip's pre-fader level through the Voicemeeter Remote API."""

    def __init__(self, dll_path, strip):
        self.dll = ctypes.WinDLL(str(dll_path))
        self.strip = strip
        self.logged_in = False
        self.channels = None

    def connect(self):
        if not self.logged_in:
            # 0 = OK, 1 = OK but Voicemeeter isn't running yet (the API
            # attaches once it starts), negative = failure.
            if self.dll.VBVMR_Login() < 0:
                return False
            self.logged_in = True

        if self.channels is None:
            vm_type = ctypes.c_long()
            if self.dll.VBVMR_GetVoicemeeterType(ctypes.byref(vm_type)) != 0:
                return False
            layout = _VOICEMEETER_LAYOUT.get(vm_type.value)
            if layout is None:
                raise RuntimeError(f"unknown Voicemeeter type {vm_type.value}")
            hardware, virtual = layout
            if not 0 <= self.strip < hardware + virtual:
                raise RuntimeError(
                    f"DEAD_AIR_VOICEMEETER_STRIP={self.strip} is out of range "
                    f"(this Voicemeeter has strips 0-{hardware + virtual - 1})"
                )
            if self.strip < hardware:
                start, count = self.strip * 2, 2
            else:
                start, count = hardware * 2 + (self.strip - hardware) * 8, 8
            self.channels = range(start, start + count)
        return True

    def read(self):
        """Loudest channel of the strip right now (linear, 1.0 = 0 dBFS), or
        None if Voicemeeter isn't running or its audio engine is stopped."""
        if not self.connect():
            return None
        value = ctypes.c_float()
        loudest = 0.0
        for channel in self.channels:
            if self.dll.VBVMR_GetLevel(_LEVEL_PRE_FADER_INPUT, channel, ctypes.byref(value)) != 0:
                self.channels = None  # re-check the type after a restart
                return None
            loudest = max(loudest, value.value)
        return loudest

    def close(self):
        if self.logged_in:
            self.dll.VBVMR_Logout()
            self.logged_in = False


class StreamChecker:
    """Polls Caster.fm stats on a background thread so a slow server never
    delays the audio meter."""

    def __init__(self):
        self.offline_detail = None  # None = online (or not yet known)

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        warned_config = False
        while True:
            stats = caster_stats.fetch_stats()
            state = stats["state"]
            if state == "server_offline":
                self.offline_detail = "Caster.fm server is not running"
            elif state == "no_source":
                self.offline_detail = "nothing is streaming to the mount"
            elif state == "on_air":
                self.offline_detail = None
            elif state == "config_error" and not warned_config:
                log.warning("Stream check disabled: %s", stats.get("detail"))
                warned_config = True
            # "error" (server answered but stats unreadable) keeps the last
            # known result rather than guessing either way.
            time.sleep(STREAM_CHECK_INTERVAL)


class StateReporter:
    def __init__(self):
        self.current = None
        self.detail = None

    def set(self, state, detail=None):
        if state != self.current:
            self.current = state
            if detail:
                log.info("STATUS: %s — %s", state, detail)
            else:
                log.info("STATUS: %s", state)
        self.detail = detail
        self.publish()

    def publish(self, level_dbfs=None):
        # Called once per window whether or not anything changed, so
        # dashboard.py can tell a running monitor from a stalled one by
        # watching updated_at go stale, and its meter keeps moving.
        write_status(STATUS_FILE, state=self.current, detail=self.detail, level_dbfs=level_dbfs)


def run(meter, stream, reporter):
    silence_started = None

    while True:
        window_end = time.monotonic() + WINDOW_SECONDS
        loudest = 0.0
        unavailable = False
        while time.monotonic() < window_end:
            reading = meter.read()
            if reading is None:
                unavailable = True
            else:
                loudest = max(loudest, reading)
            time.sleep(SAMPLE_SECONDS)

        now = time.monotonic()
        level = None if unavailable else to_dbfs(loudest)

        if level is None:
            # Can't hear anything, so the silence timer can't be trusted.
            silence_started = None
        elif level <= SILENCE_THRESHOLD_DBFS:
            if silence_started is None:
                silence_started = now
                log.info(
                    "Audio below %.1f dBFS; starting %.0fs dead-air timer.",
                    SILENCE_THRESHOLD_DBFS,
                    DEAD_AIR_SECONDS,
                )
        else:
            if silence_started is not None and now - silence_started < DEAD_AIR_SECONDS:
                log.info(
                    "Audio returned after %.1fs below threshold; timer reset.",
                    now - silence_started,
                )
            silence_started = None

        if stream.offline_detail:
            reporter.set("STREAM OFFLINE", stream.offline_detail)
        elif level is None:
            reporter.set(
                "LEVEL UNAVAILABLE",
                "Voicemeeter isn't running or its audio engine is stopped",
            )
        elif silence_started is not None and now - silence_started >= DEAD_AIR_SECONDS:
            reporter.set(
                "SILENCE",
                f"{now - silence_started:.0f}s at or below {SILENCE_THRESHOLD_DBFS:.1f} dBFS",
            )
        elif silence_started is not None:
            # During the grace period we remain AUDIO OK; only continuous
            # quiet past DEAD_AIR_SECONDS becomes SILENCE.
            reporter.set("AUDIO OK", "quiet; dead-air timer running")
        else:
            reporter.set("AUDIO OK", f"{level:.1f} dBFS")

        reporter.publish(level_dbfs=level if level is not None and math.isfinite(level) else None)


def main():
    take_over(__file__)
    if DEAD_AIR_SECONDS <= 0:
        log.error("DEAD_AIR_SECONDS must be greater than zero.")
        write_status(STATUS_FILE, state="config_error", detail="DEAD_AIR_SECONDS must be > 0")
        return

    dll_path = find_voicemeeter_dll()
    if not dll_path or not dll_path.is_file():
        log.error(
            "VoicemeeterRemote64.dll was not found. Install Voicemeeter or set "
            "VOICEMEETER_DLL to its full path."
        )
        write_status(STATUS_FILE, state="config_error", detail="VoicemeeterRemote64.dll not found")
        return

    meter = VoicemeeterMeter(dll_path, VOICEMEETER_STRIP)
    try:
        meter.connect()
    except RuntimeError as exc:
        log.error("%s", exc)
        write_status(STATUS_FILE, state="config_error", detail=str(exc))
        return

    log.info("Dead-air monitor started.")
    log.info("Audio level: Voicemeeter strip %d (pre-fader), via %s", VOICEMEETER_STRIP, dll_path)
    log.info("Stream presence: Caster.fm stats every %.0fs.", STREAM_CHECK_INTERVAL)
    log.info(
        "SILENCE = %.0f continuous seconds at or below %.1f dBFS.",
        DEAD_AIR_SECONDS,
        SILENCE_THRESHOLD_DBFS,
    )

    stream = StreamChecker()
    stream.start()
    try:
        run(meter, stream, StateReporter())
    except KeyboardInterrupt:
        pass
    finally:
        meter.close()


if __name__ == "__main__":
    main()
