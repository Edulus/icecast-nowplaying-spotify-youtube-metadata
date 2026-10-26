"""
airplay.py

Counts what Mixxx has played, from Mixxx's own history. Every Mixxx session
writes a hidden "history" playlist (Playlists.hidden = 2) with one row per
track played and the time it started (PlaylistTracks.pl_datetime_added, in
UTC). That is the station's play log, kept since 2019, so nothing new has to
be recorded: this module only reads it.

Read-only: the database is opened with mode=ro and each query is short, so
Mixxx keeps writing while the dashboard reads. Only Mixxx deck plays are
counted; Spotify and YouTube titles never reach Mixxx's history.

    python airplay.py [days]     prints the report for the last N days (default 7)
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

MIXXX_DB = os.getenv(
    "MIXXX_DB",
    str(Path(os.getenv("LOCALAPPDATA", "")) / "Mixxx" / "mixxxdb.sqlite"),
)

# Mixxx's playlist type for history sessions (PLHT_SET_LOG in Mixxx's source).
HISTORY_PLAYLIST = 2
TOP_N = 25
MAX_DAYS = 3650

_PLAYS = """
    FROM PlaylistTracks pt
    JOIN Playlists p ON p.id = pt.playlist_id
    JOIN library l ON l.id = pt.track_id
    WHERE p.hidden = ? AND pt.pl_datetime_added >= ?
"""


def _connect():
    # mode=ro never takes a write lock, and the short timeout means a busy
    # moment in Mixxx costs one missed report instead of a stalled dashboard.
    uri = Path(MIXXX_DB).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=2)


def _local(utc_text):
    """Mixxx's '2026-10-26 23:26:45' (UTC) as a local-time ISO string."""
    if not utc_text:
        return None
    moment = datetime.strptime(utc_text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return moment.astimezone().isoformat(timespec="minutes")


def report(days=7):
    """Airplay for the last `days` days, as plain JSON-ready data."""
    days = max(1, min(int(days), MAX_DAYS))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    args = (HISTORY_PLAYLIST, since)

    with _connect() as db:
        total, unique, seconds = db.execute(
            "SELECT count(*), count(DISTINCT l.id), coalesce(sum(l.duration), 0)" + _PLAYS, args
        ).fetchone()

        top_tracks = [
            {"artist": artist or "", "title": title or "", "album": album or "",
             "plays": plays, "last_played": _local(last)}
            for artist, title, album, plays, last in db.execute(
                "SELECT l.artist, l.title, l.album, count(*) AS n, max(pt.pl_datetime_added)"
                + _PLAYS + " GROUP BY l.id ORDER BY n DESC, max(pt.pl_datetime_added) DESC LIMIT ?",
                args + (TOP_N,),
            )
        ]

        def ranked(column):
            return [
                {"name": name or "(blank)", "plays": plays, "tracks": tracks}
                for name, plays, tracks in db.execute(
                    f"SELECT {column}, count(*) AS n, count(DISTINCT l.id)" + _PLAYS
                    + f" GROUP BY {column} ORDER BY n DESC LIMIT ?",
                    args + (TOP_N,),
                )
            ]

        top_artists = ranked("l.artist")
        top_albums = ranked("l.album")

        # Bucket by local calendar day, so "Friday" means Friday in Eastern time.
        per_day = {}
        for (stamp,) in db.execute("SELECT pt.pl_datetime_added" + _PLAYS, args):
            day = _local(stamp)[:10]
            per_day[day] = per_day.get(day, 0) + 1

        repeats = db.execute(
            "SELECT count(*) FROM (SELECT count(*) AS n" + _PLAYS + " GROUP BY l.id HAVING n >= 3)",
            args,
        ).fetchone()[0]

    top_20_plays = sum(t["plays"] for t in top_tracks[:20])
    return {
        "days": days,
        "since": _local(since),
        "total_plays": total,
        "unique_tracks": unique,
        "hours": round(seconds / 3600, 1),
        "tracks_played_3_plus": repeats,
        "top_20_share": round(100 * top_20_plays / total, 1) if total else 0,
        "top_tracks": top_tracks,
        "top_artists": top_artists,
        "top_albums": top_albums,
        "plays_per_day": [{"day": d, "plays": n} for d, n in sorted(per_day.items())],
    }


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    r = report(days)
    print(f"Last {r['days']} days: {r['total_plays']} plays of {r['unique_tracks']} tracks, "
          f"{r['hours']} h. {r['tracks_played_3_plus']} tracks played 3+ times; "
          f"the top 20 are {r['top_20_share']}% of airplay.")
    for t in r["top_tracks"][:15]:
        print(f"{t['plays']:4}  {t['artist']} - {t['title']}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
