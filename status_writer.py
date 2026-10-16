"""
status_writer.py

Tiny shared helper used by now_playing.py, nowplaying_poller.py, and
dead_air_monitor.py to publish their current state as a small JSON file for
dashboard.py to read. Each script owns exactly one status file and is its
only writer; writes go to a temp file first and are then swapped in, so
dashboard.py never reads a half-written file.
"""

import json
import time
from pathlib import Path

_REPLACE_ATTEMPTS = 5
_REPLACE_RETRY_SECONDS = 0.05


def write_status(path, **fields):
    """Atomically write `fields` plus an `updated_at` timestamp to `path`.

    Best-effort by design: the dashboard is optional, so a failed status
    write must never take down the script calling it.
    """
    data = {"updated_at": time.time(), **fields}
    tmp_path = Path(str(path) + ".tmp")
    try:
        tmp_path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        return

    # On Windows, replacing a file fails with PermissionError if another
    # process (dashboard.py) has it open for reading at that instant. The
    # read is over in milliseconds, so a few short retries clear it; if they
    # don't, skip this update -- the next tick writes a fresh one anyway.
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            tmp_path.replace(path)
            return
        except PermissionError:
            if attempt < _REPLACE_ATTEMPTS - 1:
                time.sleep(_REPLACE_RETRY_SECONDS)
        except OSError:
            return
