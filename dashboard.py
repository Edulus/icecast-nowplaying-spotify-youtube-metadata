"""
dashboard.py

Serves a single local status page (http://127.0.0.1:8766/) showing the live
state of the other three pipeline scripts -- now_playing.py,
nowplaying_poller.py, and dead_air_monitor.py -- by reading the small JSON
status files each of them writes (see status_writer.py), plus listener stats
fetched from the Caster.fm/Icecast server itself. Read-only: this
script never touches the pipeline itself, so it can be started, stopped, or
crash without affecting metadata pushes or dead-air detection.
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import load_dotenv

import caster_stats

load_dotenv()

HTTP_HOST = "127.0.0.1"
HTTP_PORT = int(os.getenv("DASHBOARD_PORT", "8766"))

# How old a status file's updated_at can be before the dashboard treats that
# script as "not running" rather than just quiet. Comfortably above every
# script's own POLL_INTERVAL/tick rate (default 5s) with margin for a slow
# machine or a Windows console window being dragged around.
STALE_SECONDS = float(os.getenv("DASHBOARD_STALE_SECONDS", "15"))

NOW_PLAYING_STATUS_FILE = os.getenv("NOW_PLAYING_STATUS_FILE", "now_playing_status.json")
POLLER_STATUS_FILE = os.getenv("POLLER_STATUS_FILE", "poller_status.json")
DEAD_AIR_STATUS_FILE = os.getenv("DEAD_AIR_STATUS_FILE", "dead_air_status.json")

PAGE_PATH = Path(__file__).parent / "dashboard.html"

# How often the background thread asks the Caster.fm/Icecast server for
# listener stats. Polled off the request path so a slow or unreachable server
# never stalls the dashboard's own /status responses.
CASTER_STATS_INTERVAL = float(os.getenv("CASTER_STATS_INTERVAL", "15"))

# Manual override title, read by nowplaying_poller.py (see its OVERRIDE_FILE).
OVERRIDE_FILE = os.getenv("OVERRIDE_FILE", "override.txt")
MAX_OVERRIDE_CHARS = 200
MAX_OVERRIDE_REQUEST_BYTES = 4096

# Last successfully read contents per status file. A read can briefly fail on
# Windows while the owning script is swapping in a new version; falling back
# to the previous good read avoids flashing "Not running" for a healthy
# script. Staleness is still computed from its updated_at, so a genuinely
# dead script still shows as not running.
_last_good = {}


def _read_status(path):
    """Read one script's status file, or a stale/missing placeholder if it
    hasn't been written yet (script never started)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _last_good[path] = data
    except FileNotFoundError:
        return {"updated_at": None, "stale": True}
    except (OSError, json.JSONDecodeError):
        data = _last_good.get(path)
        if data is None:
            return {"updated_at": None, "stale": True}
        data = dict(data)

    updated_at = data.get("updated_at")
    data["stale"] = updated_at is None or (time.time() - updated_at) > STALE_SECONDS
    return data


_caster_stats = {"updated_at": None, "state": "starting"}


def _caster_stats_loop():
    global _caster_stats
    while True:
        stats = caster_stats.fetch_stats()
        stats["updated_at"] = time.time()
        _caster_stats = stats  # single rebind; readers copy it
        time.sleep(CASTER_STATS_INTERVAL)


def read_override():
    try:
        with open(OVERRIDE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return ""


def write_override(title):
    """Replace the override file. Empty title clears the override. Written to
    a temp file first so nowplaying_poller.py never reads a half-written one."""
    tmp_path = Path(str(OVERRIDE_FILE) + ".tmp")
    tmp_path.write_text(title, encoding="utf-8")
    tmp_path.replace(OVERRIDE_FILE)


class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._serve_page()
        elif self.path == "/status":
            self._serve_status()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        # The only write this server accepts, and only from this machine
        # (the socket is bound to 127.0.0.1).
        if self.path != "/override":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_OVERRIDE_REQUEST_BYTES:
            self.send_response(413)
            self.end_headers()
            return

        try:
            data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            title = str(data.get("title") or "").strip()[:MAX_OVERRIDE_CHARS]
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            self.send_response(400)
            self.end_headers()
            return

        try:
            write_override(title)
        except OSError as e:
            self._send_json({"ok": False, "error": str(e)}, status=500)
            return

        print(f"Override {'set to ' + repr(title) if title else 'cleared'}")
        self._send_json({"ok": True, "title": title})

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_page(self):
        try:
            body = PAGE_PATH.read_bytes()
        except FileNotFoundError:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"dashboard.html not found next to dashboard.py")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_status(self):
        payload = {
            "now_playing": _read_status(NOW_PLAYING_STATUS_FILE),
            "poller": _read_status(POLLER_STATUS_FILE),
            "dead_air": _read_status(DEAD_AIR_STATUS_FILE),
            "caster": dict(_caster_stats),
            "override": read_override(),
            "server_time": time.time(),
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # silence default request logging to stderr


def main():
    threading.Thread(target=_caster_stats_loop, daemon=True).start()
    server = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), StatusHandler)
    print(f"TelosFM dashboard: http://{HTTP_HOST}:{HTTP_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
