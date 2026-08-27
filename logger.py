"""Mirrors everything written to stdout/stderr into a timestamped daily log
file under logs/, so the bot's existing print() calls are also persisted to
disk without changing any print() call site in main.py/brokers.py/BarAggregator.py."""

import sys
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
LOG_DIR = Path(__file__).resolve().parent / "logs"


class _Tee:
    """File-like object: writes go to the original stream unchanged, and are
    also buffered and appended to the shared log file one full line at a
    time, each prefixed with a timestamp. Unknown attributes (encoding,
    isatty, ...) are delegated to the original stream so callers can't tell
    the difference."""

    def __init__(self, stream, log_file, lock):
        self._stream = stream
        self._log_file = log_file
        self._lock = lock
        self._buffer = ""

    def write(self, text):
        self._stream.write(text)
        with self._lock:
            self._buffer += text
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
                self._log_file.write(f"[{ts}] {line}\n")
            self._log_file.flush()

    def flush(self):
        self._stream.flush()
        with self._lock:
            self._log_file.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def enable_file_logging():
    """Redirect stdout/stderr so every print() (and any uncaught traceback)
    is also appended to logs/YYYY-MM-DD.log, one timestamped line per line.
    Call this once, as early as possible - before anything else prints."""
    LOG_DIR.mkdir(exist_ok=True)
    today = datetime.now(IST).strftime("%Y-%m-%d")
    log_file = open(LOG_DIR / f"{today}.log", "a", encoding="utf-8")
    lock = threading.Lock()

    sys.stdout = _Tee(sys.stdout, log_file, lock)
    sys.stderr = _Tee(sys.stderr, log_file, lock)


def tail_log(max_lines=250):
    """Return up to the last `max_lines` lines of today's log file, newest
    last, each without its trailing newline. Empty list if nothing has been
    logged yet today. Used to embed a live log panel in the dashboard."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    path = LOG_DIR / f"{today}.log"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    return [ln.rstrip("\n") for ln in lines[-max_lines:]]
