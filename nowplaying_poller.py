"""
nowplaying_poller.py

Watches the now-playing text file (written by now_playing.py) and pushes
changes to an Icecast 2 server's metadata endpoint:

    GET /admin/metadata?mode=updinfo&mount=<mount>&charset=UTF-8&song=<title>

using HTTP Basic Auth. This is the standard Icecast 2 "updinfo" mechanism --
it works against vanilla Icecast and most hosted providers that expose it
(some shared hosts disable this endpoint entirely; see README).
"""

import base64
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from dotenv import load_dotenv

load_dotenv()

ICECAST_SCHEME = os.getenv("ICECAST_SCHEME", "http")
ICECAST_HOST = os.getenv("ICECAST_HOST")
ICECAST_PORT = os.getenv("ICECAST_PORT", "8000")
ICECAST_MOUNT = os.getenv("ICECAST_MOUNT")
ICECAST_USERNAME = os.getenv("ICECAST_USERNAME")
ICECAST_TOKEN = os.getenv("ICECAST_TOKEN")
NOWPLAYING_FILE = os.getenv("NOWPLAYING_FILE", "now_playing.txt")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "5"))

LOG_FILE = "nowplaying_poller.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# Chrome prepends an unread-notification count like "(2) " to tab titles.
NOTIFICATION_PREFIX_RE = re.compile(r"^\(\d+\)\s*")

# Chrome tab titles end in " - YouTube" / " - YouTube Music". The separator
# may be a hyphen or an en/em dash depending on the page. Anchored to the end
# of the string so a song legitimately containing the word isn't mangled.
PLATFORM_SUFFIX_RE = re.compile(r"\s*[-–—]\s*YouTube(?:\s+Music)?\s*$", re.IGNORECASE)

# Explicit bracketed/parenthesized tag forms only. Deliberately NOT doing
# bare-word matching (e.g. stripping any word "Official" or "Video") because
# that mangles real titles like "Video Games" or "Radio Edit". This list is
# meant to be edited by whoever runs the pipeline -- add/remove patterns to
# taste. Matching is case-insensitive; (Remix) and (feat. ...) are
# intentionally NOT in this list since they're meaningful title content.
#
# Note both "Visualiser" (British) and "Visualizer" (American) spellings are
# present -- the British form does show up in real release titles.
STRIP_PATTERNS = [
    "(Official Audio)",
    "(Official Video)",
    "(Official Music Video)",
    "[Official Audio]",
    "[Official Video]",
    "[Official Music Video]",
    "(Official Live Video)",
    "[Official Live Video]",
    "(Official Lyric Video)",
    "[Official Lyric Video]",
    "(Official Visualiser)",
    "[Official Visualiser]",
    "(Official Visualizer)",
    "[Official Visualizer]",
    "(Music Video)",
    "[Music Video]",
    "(Lyric Video)",
    "[Lyric Video]",
    "(Lyrics)",
    "[Lyrics]",
    "(Visualiser)",
    "[Visualiser]",
    "(Visualizer)",
    "[Visualizer]",
    "(Audio Video)",
    "[Audio Video]",
    "(Audio)",
    "[Audio]",
    "(Video)",
    "[Video]",
    "(Edit)",
    "[Edit]",
    "(HD)",
    "[HD]",
    "(4K)",
    "[4K]",
]


def clean_title(title):
    """Strip known YouTube tag noise from a raw tab/window title."""
    if not title:
        return title

    cleaned = NOTIFICATION_PREFIX_RE.sub("", title)
    cleaned = PLATFORM_SUFFIX_RE.sub("", cleaned)

    for pattern in STRIP_PATTERNS:
        # Case-insensitive literal replace.
        start = 0
        lower_cleaned = cleaned.lower()
        lower_pattern = pattern.lower()
        while True:
            idx = lower_cleaned.find(lower_pattern, start)
            if idx == -1:
                break
            cleaned = cleaned[:idx] + cleaned[idx + len(pattern):]
            lower_cleaned = cleaned.lower()
            start = idx

    # Collapse leftover whitespace and stray separators from stripped tags.
    cleaned = " ".join(cleaned.split())
    cleaned = cleaned.strip(" -|")
    return cleaned


def push_metadata(title):
    """Push a title to Icecast. Returns True on success, False otherwise."""
    query = urllib.parse.urlencode({
        "mode": "updinfo",
        "mount": ICECAST_MOUNT,
        "charset": "UTF-8",
        "song": title,
    })
    url = f"{ICECAST_SCHEME}://{ICECAST_HOST}:{ICECAST_PORT}/admin/metadata?{query}"

    # Send Basic Auth PREEMPTIVELY -- on the first request, unconditionally.
    # urllib's HTTPBasicAuthHandler is *reactive*: it only sends credentials
    # after the server answers with a 401 carrying a WWW-Authenticate
    # challenge. Not every Icecast host issues that challenge reliably, and
    # when it doesn't, the credentials are simply never sent and the failure
    # is indistinguishable from a wrong username/password. Setting the header
    # directly works in both cases.
    credentials = f"{ICECAST_USERNAME}:{ICECAST_TOKEN}"
    encoded = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
    request = urllib.request.Request(url)
    request.add_header("Authorization", f"Basic {encoded}")

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8", errors="replace")
            if response.status == 200:
                log.info("Pushed metadata: %r", title)
                return True
            log.warning("Unexpected status %s pushing metadata: %s", response.status, body)
            return False
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if "Source does not exist" in body or e.code == 404:
            # Stream isn't live right now -- not an error, just skip.
            log.info("Stream offline (source does not exist); skipping push.")
            return False
        log.error("HTTP error %s pushing metadata: %s", e.code, body)
        return False
    except urllib.error.URLError as e:
        log.error("Could not reach Icecast server: %s", e.reason)
        return False


def main():
    missing = [
        name for name, value in [
            ("ICECAST_HOST", ICECAST_HOST),
            ("ICECAST_MOUNT", ICECAST_MOUNT),
            ("ICECAST_USERNAME", ICECAST_USERNAME),
            ("ICECAST_TOKEN", ICECAST_TOKEN),
        ] if not value
    ]
    if missing:
        log.error("Missing required .env values: %s", ", ".join(missing))
        return

    log.info("Polling %s every %.1fs", NOWPLAYING_FILE, POLL_INTERVAL)

    last_pushed = None
    while True:
        try:
            with open(NOWPLAYING_FILE, "r", encoding="utf-8") as f:
                raw_title = f.read().strip()
        except FileNotFoundError:
            raw_title = ""

        title = clean_title(raw_title)

        if title and title != last_pushed:
            # Only advance last_pushed on a SUCCESSFUL push, so a transient
            # failure (stream offline, network blip) retries next loop
            # instead of silently dropping this track forever.
            if push_metadata(title):
                last_pushed = title
        elif not title:
            last_pushed = None

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
