"""
site_publisher.py

Publishes what is on air, plus a short "About this track" note, to the
telosfm.com now-playing Worker (Telos-FM repo, worker/now-playing/).

It reads the stream's own title from the Caster.fm/Icecast public stats, so
it follows everything listeners hear: Mixxx decks and Auto DJ as well as
Spotify/YouTube through the metadata poller. On each change it publishes the
track at once (with its note if one is already stored), and if the track is
new it researches a note in the background (track_notes.py) and publishes
again once the note is ready. Nothing here can delay the broadcast: it only
reads the stream's stats and talks to the website.

.env:
    NOW_PLAYING_PUBLISH_URL    the Worker URL (blank = dry run: log only)
    NOW_PLAYING_PUBLISH_TOKEN  the Worker's PUBLISH_TOKEN secret
    SITE_PUBLISH_POLL_SECONDS  how often to read the stream title (default 15)
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

from dotenv import load_dotenv

import caster_stats
from status_writer import write_status
from track_notes import NoteStore, parse_on_air

load_dotenv()

PUBLISH_URL = (os.getenv("NOW_PLAYING_PUBLISH_URL") or "").strip()
PUBLISH_TOKEN = (os.getenv("NOW_PLAYING_PUBLISH_TOKEN") or "").strip()
POLL_SECONDS = int(os.getenv("SITE_PUBLISH_POLL_SECONDS", "15"))
STATUS_FILE = os.getenv("SITE_PUBLISHER_STATUS_FILE", "site_publisher_status.json")
LOG_FILE = "site_publisher.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger(__name__)


def publish(payload):
    """PUT the payload to the Worker. Returns None on success, else an error
    string. Dry run (no URL configured) only logs."""
    if not PUBLISH_URL:
        log.info("dry run, would publish: %s", payload)
        return None
    request = urllib.request.Request(
        PUBLISH_URL, method="PUT", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {PUBLISH_TOKEN}"})
    try:
        with urllib.request.urlopen(request, timeout=10):
            return None
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return str(e)


class Publisher:
    def __init__(self, store):
        self.store = store
        self.current = None        # on-air title string last seen
        self.payload = None        # what we last published
        self.last_error = None
        self.lock = threading.Lock()

    def on_title(self, on_air):
        artist, title = parse_on_air(on_air)
        payload = {
            "title": title or on_air,
            "artist": artist or "",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "about": self.store.public_note(artist, title) if artist else "",
        }
        with self.lock:
            self.current, self.payload = on_air, payload
        self._send(payload)
        if artist and self.store.needs_research(artist, title):
            threading.Thread(target=self._research, args=(on_air, artist, title),
                             daemon=True).start()

    def _research(self, on_air, artist, title):
        note = self.store.get_note(artist, title, log=log.info)
        with self.lock:
            if not note or self.current != on_air:   # track already changed
                return
            self.payload = {**self.payload, "about": note}
            payload = self.payload
        self._send(payload)

    def _send(self, payload):
        error = publish(payload)
        self.last_error = error
        if error:
            log.warning("publish failed (%s): %s - %s", error, payload["artist"], payload["title"])
        elif PUBLISH_URL:
            log.info("published: %s - %s%s", payload["artist"], payload["title"],
                     " (with note)" if payload["about"] else "")


def main():
    if PUBLISH_URL and not PUBLISH_TOKEN:
        log.error("NOW_PLAYING_PUBLISH_TOKEN is not set; running as a dry run")
    publisher = Publisher(NoteStore())
    log.info("site publisher started (%s)", PUBLISH_URL or "dry run")
    while True:
        stats = caster_stats.fetch_stats()
        on_air = stats.get("title") if stats.get("state") == "on_air" else None
        if on_air and on_air != publisher.current:
            publisher.on_title(on_air)
        write_status(STATUS_FILE, state=stats.get("state"), on_air=on_air,
                     published=publisher.payload, error=publisher.last_error,
                     dry_run=not PUBLISH_URL)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
