"""
single_instance.py

Shared helper so each long-running TelosFM script runs only once. Starting a
script again (e.g. re-running the launcher) closes the copy that's already
running and the new one takes its place. Two copies at once is worse than
it looks: both now_playing.py servers can hold port 8765 together on Windows,
so the Chrome extension reports to one while the dashboard reads the other.

The running copy is found two ways: the PID recorded in <script>.pid, and any
python process whose command line runs the same script from this folder.
The PID file covers copies whose command line can't be read (e.g. started
from an elevated console).
"""

import os
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

import psutil

_PROJECT_DIR = Path(__file__).resolve().parent
_TERMINATE_WAIT_SECONDS = 5


def _same_path(a, b):
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


def _runs_script(proc, script_path):
    """True if `proc` is running `script_path` (best-effort)."""
    try:
        cmdline = proc.cmdline()
        cwd = None
        for arg in cmdline[1:]:
            if Path(arg).name.lower() != script_path.name.lower():
                continue
            if Path(arg).is_absolute():
                return _same_path(arg, script_path)
            if cwd is None:
                cwd = proc.cwd()
            return _same_path(Path(cwd) / arg, script_path)
    except (psutil.Error, OSError):
        pass
    return False


def _pid_file_process(pid_file):
    """The process recorded in `pid_file`, if it is still that same process."""
    try:
        pid_text, created_text = pid_file.read_text(encoding="utf-8").split()
        proc = psutil.Process(int(pid_text))
        # PIDs get reused; the creation time proves it's the process we recorded.
        if abs(proc.create_time() - float(created_text)) < 1:
            return proc
    except (OSError, ValueError, psutil.Error):
        pass
    return None


def take_over(script_file):
    """Close any running copy of `script_file`, then record this process.

    Call at the top of main(). Pass __file__.
    """
    script_path = Path(script_file).resolve()
    pid_file = _PROJECT_DIR / f"{script_path.stem}.pid"
    me = os.getpid()

    others = {}
    recorded = _pid_file_process(pid_file)
    if recorded and recorded.pid != me:
        others[recorded.pid] = recorded
    for proc in psutil.process_iter(["name"]):
        if proc.pid == me or proc.pid in others:
            continue
        if (proc.info["name"] or "").lower().startswith("python") and _runs_script(proc, script_path):
            others[proc.pid] = proc

    for proc in others.values():
        print(f"Closing the copy of {script_path.name} already running (PID {proc.pid}).")
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            print(
                f"[ERROR] Can't close PID {proc.pid}: it was started as administrator.\n"
                f"        Close its window yourself, or run this one as administrator too."
            )
            sys.exit(1)
    _, alive = psutil.wait_procs(list(others.values()), timeout=_TERMINATE_WAIT_SECONDS)
    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive, timeout=_TERMINATE_WAIT_SECONDS)

    try:
        created = psutil.Process(me).create_time()
        pid_file.write_text(f"{me} {created}", encoding="utf-8")
    except (OSError, psutil.Error):
        pass  # best-effort: the command-line match still finds us next time


def exclusive_http_server(address, handler):
    """A ThreadingHTTPServer that fails to start if the port is already taken.

    http.server sets SO_REUSEADDR, which on Windows lets a second process bind
    the same port silently instead of failing. Turning it off makes a stray
    duplicate fail loudly.
    """
    server = ThreadingHTTPServer(address, handler, bind_and_activate=False)
    server.allow_reuse_address = False
    try:
        server.server_bind()
        server.server_activate()
    except BaseException:
        server.server_close()
        raise
    return server
