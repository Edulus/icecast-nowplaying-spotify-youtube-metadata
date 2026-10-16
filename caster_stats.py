"""
caster_stats.py

Shared helper used by dashboard.py and dead_air_monitor.py to read the
Caster.fm/Icecast server's public stats (/status-json.xsl). Public on purpose:
Caster.fm's free plan requires a per-IP listener token to open the audio
stream itself, but these stats need no authentication at all, so the private
API token is never sent here.
"""

import json
import os
import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv()

ICECAST_SCHEME = os.getenv("ICECAST_SCHEME", "http")
ICECAST_HOST = os.getenv("ICECAST_HOST")
ICECAST_PORT = os.getenv("ICECAST_PORT", "8000")
ICECAST_MOUNT = os.getenv("ICECAST_MOUNT")

# Standard Icecast stats endpoint, which Caster.fm also serves. (Caster's own
# /admin/publicstats.json uses a different layout and has fewer fields, e.g.
# no bitrate or listener_peak, so it isn't used here.)
STATS_PATH = "/status-json.xsl"


def _pick_source(icestats):
    """Return the source entry for our mount. Icecast gives a single object
    when one mount is live, a list when several are, and omits it when none."""
    sources = icestats.get("source")
    if isinstance(sources, dict):
        sources = [sources]
    if not sources:
        return None
    mount = (ICECAST_MOUNT or "").lstrip("/")
    for source in sources:
        listenurl = str(source.get("listenurl") or source.get("mount") or "")
        if mount and listenurl.rstrip("/").endswith("/" + mount):
            return source
    # A mount is configured but isn't among the live sources.
    return None if mount else sources[0]


def _source_bitrate(source):
    """Nominal kbps. Caster.fm doesn't send "bitrate"; it only reports it
    inside audio_info ("bitrate=96") and as total_bitrate in bits/sec."""
    if source.get("bitrate"):
        return source["bitrate"]
    for part in str(source.get("audio_info") or "").split(";"):
        key, _, value = part.partition("=")
        if key.strip() == "bitrate" and value.strip():
            return value.strip()
    total = source.get("total_bitrate")
    if isinstance(total, (int, float)) and total > 0:
        return round(total / 1000)
    return None


def fetch_stats(timeout=8):
    """Return a dict whose "state" is one of:

        on_air          our mount has a live source (plus listener fields)
        no_source       server is up but nothing is streaming to our mount
        server_offline  server unreachable -- Caster.fm shuts it down when
                        nothing is broadcasting
        error           server answered but the stats couldn't be read
        config_error    ICECAST_HOST isn't set
    """
    if not ICECAST_HOST:
        return {"state": "config_error", "detail": "ICECAST_HOST is not set"}

    url = f"{ICECAST_SCHEME}://{ICECAST_HOST}:{ICECAST_PORT}{STATS_PATH}"
    request = urllib.request.Request(url, headers={"User-Agent": "TelosFM"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            icestats = json.load(response).get("icestats") or {}
    except urllib.error.HTTPError as e:
        return {"state": "error", "detail": f"HTTP {e.code} from {STATS_PATH}"}
    except urllib.error.URLError:
        return {"state": "server_offline"}
    except (OSError, ValueError, AttributeError) as e:
        return {"state": "error", "detail": f"{type(e).__name__} reading {STATS_PATH}"}

    source = _pick_source(icestats)
    if source is None:
        return {"state": "no_source", "server_start": icestats.get("server_start_iso8601")}

    title = source.get("title") or source.get("yp_currently_playing")
    current_bitrate = source.get("current_bitrate")
    return {
        "state": "on_air",
        "listeners": source.get("listeners"),
        "listener_peak": source.get("listener_peak"),
        "bitrate": _source_bitrate(source),
        # Measured rate right now, vs the nominal bitrate above.
        "current_bitrate": round(current_bitrate / 1000) if current_bitrate else None,
        "title": title,
        "history": _history(source, title),
        "genre": source.get("genre"),
        "server_name": source.get("server_name"),
        "listen_url": source.get("listenurl"),
        "content_type": source.get("server_type") or source.get("content-type"),
        "stream_start": source.get("stream_start_iso8601") or source.get("stream_start"),
        "server_start": icestats.get("server_start_iso8601") or icestats.get("server_start"),
    }


def _history(source, current_title):
    """Recently played titles, newest first, without the current one. Icecast
    keeps the last ~10 in playlist.trackList, oldest first, and repeats a
    title whenever it is re-sent."""
    playlist = source.get("playlist") or {}
    tracks = playlist.get("trackList")
    if isinstance(tracks, dict):  # some builds nest it one level deeper
        tracks = tracks.get("track")
    if not isinstance(tracks, list):
        return []

    titles = []
    for track in reversed(tracks):
        title = track.get("title") if isinstance(track, dict) else track
        if title and (not titles or titles[-1] != title):
            titles.append(title)
    if titles and titles[0] == current_title:
        titles.pop(0)
    return titles[:6]
