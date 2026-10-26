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

from mixxx_window import mixxx_deck_title
from single_instance import take_over
from status_writer import write_status

load_dotenv()

ICECAST_SCHEME = os.getenv("ICECAST_SCHEME", "http")
ICECAST_HOST = os.getenv("ICECAST_HOST")
ICECAST_PORT = os.getenv("ICECAST_PORT", "8000")
ICECAST_MOUNT = os.getenv("ICECAST_MOUNT")
ICECAST_USERNAME = os.getenv("ICECAST_USERNAME")
ICECAST_TOKEN = os.getenv("ICECAST_TOKEN")
NOWPLAYING_FILE = os.getenv("NOWPLAYING_FILE", "now_playing.txt")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "5"))
STATUS_FILE = os.getenv("POLLER_STATUS_FILE", "poller_status.json")

# Manual override: while this file holds text, that text is the on-air title
# and every automatic source is held back. Written by dashboard.py's override
# box; an empty or missing file means no override.
OVERRIDE_FILE = os.getenv("OVERRIDE_FILE", "override.txt")

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

# Any bracketed/parenthesized tag whose contents begin with "Official" is
# metadata noise rather than part of the track title. This intentionally
# catches variants we have not enumerated, e.g. (Official HD Video),
# [Official 4K Video], etc., while leaving bare words such as "Official"
# elsewhere in a legitimate title untouched.
OFFICIAL_TAG_RE = re.compile(
    r"\(\s*Official\b[^)]*\)|\[\s*Official\b[^\]]*\]",
    re.IGNORECASE,
)

# Explicit bracketed/parenthesized tag forms only. Deliberately NOT doing
# bare-word matching (e.g. stripping any word "Video") because that mangles
# real titles like "Video Games" or "Radio Edit". This list is meant to be
# edited by whoever runs the pipeline -- add/remove patterns to taste.
# Matching is case-insensitive; (Remix) and (feat. ...) are intentionally NOT
# in this list since they're meaningful title content.
#
# Note both "Visualiser" (British) and "Visualizer" (American) spellings are
# present -- the British form does show up in real release titles.
STRIP_PATTERNS = [
    # Bare (no brackets), unlike everything else in this list -- some
    # uploaders tag titles with "Official Video" untagged. Kept to this
    # specific two-word phrase rather than bare-word matching "Official" or
    # "Video" alone, which would mangle real titles. OFFICIAL_TAG_RE above
    # only catches the bracketed/parenthesized forms.
    "Official Video",
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
    cleaned = OFFICIAL_TAG_RE.sub("", cleaned)

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


def format_display(title):
    """Reformat a cleaned 'Artist - Track' string as '"Track" by Artist' for
    on-air display, so it's unambiguous which part is the musician and which
    is the song. Both now_playing.py's Spotify and YouTube paths already
    produce an "Artist - Track"-shaped string by the time it reaches this
    file, so splitting on the first ' - ' works for either source.
    Falls back to the original string unchanged if there's no separator to
    split on (nothing to reformat)."""
    sep_idx = title.find(" - ")
    if sep_idx <= 0:
        return title

    artist = title[:sep_idx].strip()
    track = title[sep_idx + 3:].strip()
    if not artist or not track:
        return title

    return f'"{track}" by {artist}'


def read_override():
    """Current manual override title, or "" when there is none."""
    try:
        with open(OVERRIDE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return ""


def push_metadata(title):
    """Push a title to Icecast. Returns None on success, otherwise a short
    plain-language reason for the dashboard."""
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
                return None
            log.warning("Unexpected status %s pushing metadata: %s", response.status, body)
            return f"Caster.fm answered with HTTP {response.status}"
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if "Source does not exist" in body or e.code == 404:
            # Stream isn't live right now -- not an error, just skip.
            log.info("Stream offline (source does not exist); skipping push.")
            return "The stream is offline, so there is nothing to attach a title to"
        log.error("HTTP error %s pushing metadata: %s", e.code, body)
        if e.code in (401, 403):
            return f"Caster.fm rejected the login (HTTP {e.code}); check ICECAST_USERNAME and ICECAST_TOKEN"
        return f"Caster.fm returned HTTP {e.code}"
    except urllib.error.URLError as e:
        log.error("Could not reach Icecast server: %s", e.reason)
        return "Can't reach the Caster.fm server (it may be shut down)"


def main():
    take_over(__file__)
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
        write_status(STATUS_FILE, state="config_error", detail=", ".join(missing))
        return

    log.info("Polling %s every %.1fs", NOWPLAYING_FILE, POLL_INTERVAL)

    last_pushed = None
    # Unlike last_pushed, these survive idle periods so the dashboard can
    # still say what was sent most recently.
    last_sent_title = None
    last_sent_kind = None  # "source" (Spotify/YouTube) or "mixxx_handback"
    last_push_at = None
    push_error = None
    state = "idle"
    while True:
        try:
            with open(NOWPLAYING_FILE, "r", encoding="utf-8") as f:
                raw_title = f.read().strip()
        except FileNotFoundError:
            raw_title = ""

        title = clean_title(raw_title)
        title = format_display(title) if title else title
        attempted_title = title  # what this loop tried to put on air

        override = read_override()
        if override:
            # Manual override wins over every automatic source, and is sent
            # exactly as typed -- no cleaning, no reformatting.
            attempted_title = override
            state = "override"
            if override != last_pushed:
                push_error = push_metadata(override)
                if push_error is None:
                    last_pushed = override
                    last_sent_title = override
                    last_sent_kind = "override"
                    last_push_at = time.time()
                else:
                    state = "retrying"
        elif title and title != last_pushed:
            # Only advance last_pushed on a SUCCESSFUL push, so a transient
            # failure (stream offline, network blip) retries next loop
            # instead of silently dropping this track forever.
            push_error = push_metadata(title)
            if push_error is None:
                last_pushed = title
                last_sent_title = title
                last_sent_kind = "source"
                last_push_at = time.time()
                state = "pushed"
            else:
                state = "retrying"
        elif not title and last_pushed:
            # A Spotify/YouTube title we sent is still on air, but that source
            # just went off air (Aux PLAY off, paused, closed). Mixxx only
            # sends its own deck title when the deck track changes, so hand
            # the on-air title back by sending the current deck title now,
            # formatted the way Mixxx's broadcast profile does.
            deck_title = mixxx_deck_title()
            if deck_title:
                display = format_display(deck_title)
                attempted_title = display
                push_error = push_metadata(display)
                if push_error is None:
                    log.info("Handed on-air title back to Mixxx deck: %r", display)
                    last_pushed = None
                    last_sent_title = display
                    last_sent_kind = "mixxx_handback"
                    last_push_at = time.time()
                    state = "handed_back"
                else:
                    # Keep last_pushed so the handback retries next loop.
                    state = "retrying"
            else:
                # No deck playing: nothing to hand back to.
                last_pushed = None
                push_error = None
                state = "idle"
        elif not title:
            push_error = None
            state = "idle"
        else:
            state = "pushed"

        write_status(
            STATUS_FILE,
            state=state,
            title=attempted_title or None,
            last_sent_title=last_sent_title,
            last_sent_kind=last_sent_kind,
            last_push_at=last_push_at,
            override=override or None,
            error=push_error,
        )
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
