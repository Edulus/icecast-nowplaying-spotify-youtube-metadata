"""
dead_air_monitor.py

Piece 1 of the dead-air safety system.

Listens to the actual Icecast listener stream and reports three states:

    AUDIO OK
    SILENCE
    STREAM OFFLINE

SILENCE is declared only after the decoded stream remains below the configured
audio threshold continuously for DEAD_AIR_SECONDS (30 seconds by default).

This script takes no recovery or notification action yet. Later pieces can
subscribe to these state changes without changing the audio detector.
"""

import logging
import math
import os
import queue
import shutil
import subprocess
import threading
import sys
from array import array
import time
from collections import deque

from dotenv import load_dotenv

load_dotenv()

ICECAST_SCHEME = os.getenv("ICECAST_SCHEME", "http")
ICECAST_HOST = os.getenv("ICECAST_HOST")
ICECAST_PORT = os.getenv("ICECAST_PORT", "8000")
ICECAST_MOUNT = os.getenv("ICECAST_MOUNT")

DEAD_AIR_STREAM_URL = os.getenv("DEAD_AIR_STREAM_URL")
DEAD_AIR_SECONDS = float(os.getenv("DEAD_AIR_SECONDS", "30"))
SILENCE_THRESHOLD_DBFS = float(os.getenv("SILENCE_THRESHOLD_DBFS", "-60"))
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")
RECONNECT_DELAY = float(os.getenv("DEAD_AIR_RECONNECT_DELAY", "5"))
STREAM_STALL_SECONDS = float(os.getenv("DEAD_AIR_STALL_SECONDS", "10"))

SAMPLE_RATE = 8000
SAMPLE_WIDTH = 2  # signed 16-bit PCM
WINDOW_SECONDS = 1
WINDOW_BYTES = SAMPLE_RATE * SAMPLE_WIDTH * WINDOW_SECONDS

LOG_FILE = "dead_air_monitor.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger(__name__)


def build_stream_url():
    if DEAD_AIR_STREAM_URL:
        return DEAD_AIR_STREAM_URL

    if not ICECAST_HOST or not ICECAST_MOUNT:
        return None

    mount = ICECAST_MOUNT if ICECAST_MOUNT.startswith("/") else f"/{ICECAST_MOUNT}"
    return f"{ICECAST_SCHEME}://{ICECAST_HOST}:{ICECAST_PORT}{mount}"


def dbfs(pcm):
    """Return RMS level in dBFS for signed 16-bit little-endian mono PCM."""
    if not pcm:
        return float("-inf")

    # A final partial read can contain an odd byte. Ignore it rather than
    # failing the monitor while ffmpeg is shutting down.
    usable = len(pcm) - (len(pcm) % SAMPLE_WIDTH)
    if usable == 0:
        return float("-inf")

    samples = array("h")
    samples.frombytes(pcm[:usable])
    if sys.byteorder != "little":
        samples.byteswap()

    mean_square = sum(sample * sample for sample in samples) / len(samples)
    if mean_square <= 0:
        return float("-inf")

    rms = math.sqrt(mean_square)
    return 20.0 * math.log10(rms / 32768.0)


class StateReporter:
    def __init__(self):
        self.current = None

    def set(self, state, detail=None):
        if state == self.current:
            return
        self.current = state
        if detail:
            log.info("STATUS: %s — %s", state, detail)
        else:
            log.info("STATUS: %s", state)


class FFmpegStream:
    """Decode the remote stream to low-rate mono PCM without playing it."""

    def __init__(self, url):
        self.url = url
        self.process = None
        self.chunks = queue.Queue(maxsize=8)
        self.stderr_lines = deque(maxlen=20)
        self.stdout_thread = None
        self.stderr_thread = None

    def start(self):
        creationflags = 0
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = subprocess.CREATE_NO_WINDOW

        command = [
            FFMPEG_PATH,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "warning",
            "-i",
            self.url,
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "s16le",
            "-",
        ]

        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            bufsize=0,
            creationflags=creationflags,
        )

        self.stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self.stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self.stdout_thread.start()
        self.stderr_thread.start()

    def _read_stdout(self):
        try:
            while self.process and self.process.stdout:
                chunk = self.process.stdout.read(WINDOW_BYTES)
                if not chunk:
                    break
                try:
                    self.chunks.put(chunk, timeout=1)
                except queue.Full:
                    # The monitor should normally consume faster than this.
                    # Dropping an old analysis window is safer than blocking
                    # the decoder and making the stream appear stalled.
                    try:
                        self.chunks.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self.chunks.put_nowait(chunk)
                    except queue.Full:
                        pass
        finally:
            try:
                self.chunks.put_nowait(None)
            except queue.Full:
                pass

    def _read_stderr(self):
        if not self.process or not self.process.stderr:
            return
        for raw_line in iter(self.process.stderr.readline, b""):
            line = raw_line.decode("utf-8", errors="replace").strip()
            if line:
                self.stderr_lines.append(line)

    def next_chunk(self, timeout):
        try:
            return self.chunks.get(timeout=timeout)
        except queue.Empty:
            return None

    def is_running(self):
        return self.process is not None and self.process.poll() is None

    def error_summary(self):
        return " | ".join(self.stderr_lines[-3:])

    def stop(self):
        if not self.process:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None


def monitor_once(url, reporter):
    stream = FFmpegStream(url)
    silence_started = None

    try:
        stream.start()
    except OSError as exc:
        reporter.set("STREAM OFFLINE", f"could not start ffmpeg: {exc}")
        return

    last_audio_data = time.monotonic()

    try:
        while True:
            chunk = stream.next_chunk(timeout=1)

            if chunk is None:
                if not stream.is_running():
                    detail = stream.error_summary() or "decoder stopped"
                    reporter.set("STREAM OFFLINE", detail)
                    return

                if time.monotonic() - last_audio_data >= STREAM_STALL_SECONDS:
                    reporter.set(
                        "STREAM OFFLINE",
                        f"no audio data received for {STREAM_STALL_SECONDS:.0f}s",
                    )
                    return
                continue

            last_audio_data = time.monotonic()
            level = dbfs(chunk)
            now = time.monotonic()

            if level <= SILENCE_THRESHOLD_DBFS:
                if silence_started is None:
                    silence_started = now
                    log.info(
                        "Audio below %.1f dBFS; starting %.0fs dead-air timer.",
                        SILENCE_THRESHOLD_DBFS,
                        DEAD_AIR_SECONDS,
                    )
                    # Receiving decoded audio means the stream is online.
                    # During the 30-second grace period we remain AUDIO OK;
                    # only continuous quiet past the threshold becomes SILENCE.
                    if reporter.current != "SILENCE":
                        reporter.set(
                            "AUDIO OK",
                            "stream online; dead-air timer running",
                        )

                quiet_for = now - silence_started
                if quiet_for >= DEAD_AIR_SECONDS:
                    reporter.set(
                        "SILENCE",
                        f"{quiet_for:.0f}s at or below {SILENCE_THRESHOLD_DBFS:.1f} dBFS",
                    )
            else:
                if silence_started is not None:
                    quiet_for = now - silence_started
                    if quiet_for < DEAD_AIR_SECONDS:
                        log.info(
                            "Audio returned after %.1fs below threshold; timer reset.",
                            quiet_for,
                        )
                silence_started = None
                reporter.set("AUDIO OK", f"{level:.1f} dBFS")
    finally:
        stream.stop()


def main():
    url = build_stream_url()
    if not url:
        log.error(
            "Missing stream configuration. Set ICECAST_HOST and ICECAST_MOUNT "
            "(or set DEAD_AIR_STREAM_URL directly)."
        )
        return

    ffmpeg_executable = shutil.which(FFMPEG_PATH)
    if not ffmpeg_executable:
        log.error(
            "ffmpeg was not found. Install ffmpeg or set FFMPEG_PATH to ffmpeg.exe."
        )
        return

    if DEAD_AIR_SECONDS <= 0:
        log.error("DEAD_AIR_SECONDS must be greater than zero.")
        return

    log.info("Dead-air monitor started.")
    log.info("Monitoring listener stream: %s", url)
    log.info(
        "SILENCE = %.0f continuous seconds at or below %.1f dBFS.",
        DEAD_AIR_SECONDS,
        SILENCE_THRESHOLD_DBFS,
    )

    reporter = StateReporter()

    while True:
        monitor_once(url, reporter)
        time.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    main()
