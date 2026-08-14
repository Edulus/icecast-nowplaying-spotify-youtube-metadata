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
"""

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psutil
import win32con
import win32gui
import win32process
from dotenv import load_dotenv

load_dotenv()

NOWPLAYING_FILE = os.getenv("NOWPLAYING_FILE", "now_playing.txt")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "5"))

HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8765
YOUTUBE_FRESH_SECONDS = 90

SPOTIFY_PROCESS_NAME = "spotify.exe"

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

# Chrome tab titles for YouTube carry more than the video title: an unread-
# notification count like "(2) " prepended, and " - YouTube" /
# " - YouTube Music" appended. Stripped here, before the channel name (from
# the content script) gets prepended below -- otherwise they'd end up stuck
# in the middle of the combined "Channel - Title" string instead of at an
# edge where nowplaying_poller.py's own stripping could still catch them.
_YT_NOTIFICATION_PREFIX_RE = re.compile(r"^\(\d+\)\s*")
_YT_PLATFORM_SUFFIX_RE = re.compile(r"\s*[-–—]\s*YouTube(?:\s+Music)?\s*$", re.IGNORECASE)


def _spotify_now_playing():
    """Return 'Artist - Track' from Spotify's hidden window, or None."""
    spotify_pids = {
        p.pid for p in psutil.process_iter(["name"])
        if (p.info["name"] or "").lower() == SPOTIFY_PROCESS_NAME
    }
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
        global _youtube_title, _youtube_channel, _youtube_last_seen

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = {}

        with _youtube_lock:
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
    server = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), _MetadataHandler)
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

    if channel and not title.lower().startswith(channel.lower()):
        return f"{channel} - {title}"
    return title


def main():
    _start_http_server()
    print(f"Listening for YouTube updates on http://{HTTP_HOST}:{HTTP_PORT}")

    last_written = None
    while True:
        title = _current_youtube_now_playing() or _spotify_now_playing()

        if title != last_written:
            with open(NOWPLAYING_FILE, "w", encoding="utf-8") as f:
                f.write(title or "")
            last_written = title
            print(f"Now playing: {title!r}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
