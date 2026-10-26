"""
now_playing.py

Detects the current track from Spotify (desktop app) and/or YouTube (via the
companion Chrome extension) and writes it to a local text file that
nowplaying_poller.py reads and pushes to Icecast.

Spotify detection:
    Spotify's desktop app keeps a window whose title is "Artist - Track", but
    that window is NOT visible in the normal sense -- enumerating windows and
    filtering on IsWindowVisible() finds nothing. The only reliable way to
    find it is to enumerate *every* top-level window (visible or not),
    resolve each window's owning process ID via GetWindowThreadProcessId, and
    keep the ones owned by Spotify.exe. Among Spotify's windows, the one with
    a title containing " - " (and not equal to the bare app name "Spotify")
    is the now-playing title.

YouTube detection:
    A Manifest V3 Chrome extension (chrome-extension/) finds the actually
    audible YouTube/YouTube Music tab and POSTs its title as JSON to a small
    HTTP server this script runs on localhost. That data is considered
    "fresh" for YOUTUBE_FRESH_SECONDS; if fresh, YouTube wins over Spotify
    (since YouTube in a browser is usually a deliberate, active choice).
    Otherwise we fall back to whatever Spotify reports.

Aux on-air check:
    Spotify and YouTube reach Mixxx's Aux inputs through Voicemeeter, and the
    route changes (EarTrumpet picks the Voicemeeter input per app, the
    strip's B buttons pick the buses). audio_routing.py reads that route live
    to find which Aux inputs a source reaches; SPOTIFY_AUX / YOUTUBE_AUX are
    only the fallback when it can't be read. A source's title is only written
    when one of its Aux inputs is feeding the main mix -- PLAY on, fader up,
    not crossfaded out -- as reported by the TelosFM Bridge mapping over MIDI
    (mixxx_bridge.py). If the bridge isn't connected, titles are written
    unchecked, unless the route shows the audio doesn't reach Mixxx at all.

Mixxx deck detection (dashboard only):
    Mixxx sends its own deck titles to Icecast, so they are never written to
    NOWPLAYING_FILE. For the dashboard, the deck track is read from Mixxx's
    main window title ("Artist - Title | Mixxx"; see mixxx_window.py), which follows the same
    now-playing deck Mixxx uses for its broadcast metadata. (The library
    database's last_played_at was tried first but lags by minutes.)
"""

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler

import win32con
import win32gui
import win32process
from dotenv import load_dotenv

from audio_routing import VoicemeeterRouting, playback_devices_by_process
from mixxx_bridge import MixxxBridge
from mixxx_window import MIXXX_PROCESS_NAME, mixxx_deck_title, running_pids
from single_instance import exclusive_http_server, take_over
from status_writer import write_status

load_dotenv()

NOWPLAYING_FILE = os.getenv("NOWPLAYING_FILE", "now_playing.txt")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "5"))
STATUS_FILE = os.getenv("NOW_PLAYING_STATUS_FILE", "now_playing_status.json")

HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8765
YOUTUBE_FRESH_SECONDS = 90

SPOTIFY_PROCESS_NAME = "spotify.exe"
CHROME_PROCESS_NAME = "chrome.exe"

# Fallback Mixxx Aux input for each source, used only when its live
# Voicemeeter route can't be read (see audio_routing.py).
SPOTIFY_AUX = int(os.getenv("SPOTIFY_AUX", "1"))
YOUTUBE_AUX = int(os.getenv("YOUTUBE_AUX", "2"))

# Window titles that are never a real track. The bare app name "Spotify" is
# already excluded by the " - " requirement below; these are extra guards for
# ad playback. NOTE: ad-title behaviour varies by Spotify client version and
# was NOT verified against a live ad -- treat this list as best-effort.
SPOTIFY_IGNORE_TITLES = {"spotify", "advertisement", "spotify free", "spotify premium"}

# Shared state updated by the HTTP server thread, read by the main loop.
_youtube_lock = threading.Lock()
_youtube_title = None
_youtube_channel = None
_youtube_last_seen = 0.0
_youtube_last_contact = 0.0  # any POST from the extension, idle or not

# Chrome tab titles for YouTube carry more than the video title: an unread-
# notification count like "(2) " prepended, and " - YouTube" /
# " - YouTube Music" appended. Stripped here, before the channel name (from
# the content script) gets prepended below -- otherwise they'd end up stuck
# in the middle of the combined "Channel - Title" string instead of at an
# edge where nowplaying_poller.py's own stripping could still catch them.
_YT_NOTIFICATION_PREFIX_RE = re.compile(r"^\(\d+\)\s*")
_YT_PLATFORM_SUFFIX_RE = re.compile(r"\s*[-–—]\s*YouTube(?:\s+Music)?\s*$", re.IGNORECASE)

# YouTube auto-generates a channel named "<Artist> - Topic" for music that
# has no official artist channel, which would otherwise combine into
# "<Artist> - Topic - <Track>" on air. Add more channel-name noise here as
# it turns up.
_YOUTUBE_CHANNEL_STRIP_SUFFIXES = [" - Topic"]


def _clean_youtube_channel(channel):
    if not channel:
        return channel
    for suffix in _YOUTUBE_CHANNEL_STRIP_SUFFIXES:
        if channel.lower().endswith(suffix.lower()):
            channel = channel[: -len(suffix)]
    return channel.strip()


def _spotify_now_playing(spotify_pids):
    """Return 'Artist - Track' from Spotify's hidden window, or None."""
    if not spotify_pids:
        return None

    found = []

    def _enum_handler(hwnd, _):
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid not in spotify_pids:
            return
        title = win32gui.GetWindowText(hwnd)
        if title and title.strip().lower() not in SPOTIFY_IGNORE_TITLES and " - " in title:
            found.append(title)

    # EnumWindows walks ALL top-level windows regardless of visibility --
    # that's the point. IsWindowVisible() filtering (the "normal" approach)
    # misses Spotify's now-playing window entirely.
    win32gui.EnumWindows(_enum_handler, None)

    return found[0] if found else None


class _MetadataHandler(BaseHTTPRequestHandler):
    """Receives now-playing POSTs from the Chrome extension."""

    def _set_cors_headers(self):
        # The extension's background service worker POSTs JSON to us, which
        # Chrome treats as a cross-origin request and precedes with an
        # OPTIONS preflight. Without these headers the browser blocks the
        # actual POST and the extension silently never delivers data.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._set_cors_headers()
        self.end_headers()

    def do_POST(self):
        global _youtube_title, _youtube_channel, _youtube_last_seen, _youtube_last_contact

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = {}

        with _youtube_lock:
            _youtube_last_contact = time.time()
            if data.get("idle"):
                _youtube_title = None
                _youtube_channel = None
            else:
                title = data.get("title")
                if title:
                    _youtube_title = title
                    _youtube_channel = data.get("channel")
                    _youtube_last_seen = time.time()

        self.send_response(200)
        self._set_cors_headers()
        self.end_headers()

    def log_message(self, format, *args):
        pass  # silence default request logging to stderr


def _start_http_server():
    server = exclusive_http_server((HTTP_HOST, HTTP_PORT), _MetadataHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _current_youtube_now_playing():
    """Return 'Channel - Title' (or bare title if no channel) for the
    current YouTube tab, or None if nothing fresh has been reported."""
    with _youtube_lock:
        if not (_youtube_title and (time.time() - _youtube_last_seen) < YOUTUBE_FRESH_SECONDS):
            return None
        title = _youtube_title
        channel = _youtube_channel

    title = _YT_NOTIFICATION_PREFIX_RE.sub("", title)
    title = _YT_PLATFORM_SUFFIX_RE.sub("", title).strip()
    channel = _clean_youtube_channel(channel)

    if channel and not title.lower().startswith(channel.lower()):
        return f"{channel} - {title}"
    return title


def _youtube_state(youtube_title):
    """playing / idle (extension reports no audible tab) / no_report."""
    if youtube_title:
        return "playing"
    with _youtube_lock:
        last_contact = _youtube_last_contact
    # The extension reports while Chrome runs; a long gap means Chrome is
    # closed or the extension is disabled.
    if last_contact and time.time() - last_contact < YOUTUBE_FRESH_SECONDS:
        return "idle"
    return "no_report"


def _source_route(process_name, devices_by_process, routing):
    """Live Voicemeeter route of a source's audio, or None when it can't be
    worked out (no sound from it right now, or Windows/Voicemeeter can't be
    read)."""
    if not devices_by_process or process_name not in devices_by_process:
        return None
    return routing.route(devices_by_process[process_name])


def _source_status(state, title, route, fallback_aux, bridge, bridge_connected):
    """Per-source status, including whether any of its Aux channels is on air."""
    auxes = route["auxes"] if route else [fallback_aux]
    status = {"state": state, "title": title, "route": route, "auxes": auxes,
              "live_auxes": None, "off_air_reason": None}
    if not auxes:
        # The route shows its audio never reaches a Mixxx Aux input.
        status["on_air"] = False
        status["off_air_reason"] = route["note"] or "its audio doesn't reach a Mixxx Aux input"
    elif bridge_connected:
        live = [n for n in auxes if bridge.aux(n)["live"]]
        status["live_auxes"] = live
        status["on_air"] = bool(live)
        if not live:
            status["off_air_reason"] = "; ".join(f"Aux {n} {bridge.aux(n)['reason']}" for n in auxes)
    else:
        # Without the bridge there's no way to check the Aux PLAY button, so
        # a playing source is assumed on air (the pre-bridge behaviour).
        status["on_air"] = None
    return status


def main():
    take_over(__file__)
    _start_http_server()
    print(f"Listening for YouTube updates on http://{HTTP_HOST}:{HTTP_PORT}")

    bridge = MixxxBridge()
    bridge.start()
    print(
        f"Mixxx bridge: listening on MIDI port {bridge.port_name!r} "
        f"(Aux inputs read from Voicemeeter routing; fallback Spotify = Aux {SPOTIFY_AUX}, "
        f"YouTube = Aux {YOUTUBE_AUX})"
    )
    routing = VoicemeeterRouting()

    # Sentinel rather than None so the first pass always writes the file,
    # clearing any title left over from a previous run.
    last_written = object()
    last_bridge_connected = None
    while True:
        spotify_pids = running_pids(SPOTIFY_PROCESS_NAME)
        spotify_title = _spotify_now_playing(spotify_pids)
        youtube_title = _current_youtube_now_playing()

        bridge_connected = bridge.connected()
        if bridge_connected != last_bridge_connected:
            print("Mixxx bridge connected." if bridge_connected else
                  "Mixxx bridge not connected; titles are sent without checking Aux PLAY.")
            last_bridge_connected = bridge_connected

        if spotify_title:
            spotify_state = "playing"
        else:
            spotify_state = "idle" if spotify_pids else "not_running"
        devices_by_process = playback_devices_by_process()
        spotify = _source_status(
            spotify_state, spotify_title,
            _source_route(SPOTIFY_PROCESS_NAME, devices_by_process, routing),
            SPOTIFY_AUX, bridge, bridge_connected,
        )
        youtube = _source_status(
            _youtube_state(youtube_title), youtube_title,
            _source_route(CHROME_PROCESS_NAME, devices_by_process, routing),
            YOUTUBE_AUX, bridge, bridge_connected,
        )

        # Only a source whose Aux is actually feeding the broadcast gets its
        # title sent. YouTube still wins when both are on air.
        if youtube_title and youtube["on_air"] is not False:
            title, source = youtube_title, "YouTube"
        elif spotify_title and spotify["on_air"] is not False:
            title, source = spotify_title, "Spotify"
        else:
            title, source = None, None

        if title != last_written:
            with open(NOWPLAYING_FILE, "w", encoding="utf-8") as f:
                f.write(title or "")
            last_written = title
            print(f"Now playing: {title!r}")

        mixxx = {"state": "not_running"}
        mixxx_pids = running_pids(MIXXX_PROCESS_NAME)
        if mixxx_pids:
            mixxx = {"state": "running", "title": mixxx_deck_title(mixxx_pids)}
        mixxx["bridge"] = {
            "connected": bridge_connected,
            "port_found": bridge_connected or bridge.port_found(),
            "port_name": bridge.port_name,
            "aux": [bridge.aux(n) for n in range(1, 5)] if bridge_connected else None,
            "decks": [bridge.deck(n) for n in range(1, 5)] if bridge_connected else None,
        }

        write_status(
            STATUS_FILE,
            source=source,
            title=title,
            spotify=spotify,
            youtube=youtube,
            mixxx=mixxx,
        )
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
