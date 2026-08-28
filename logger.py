"""Mirrors everything written to stdout/stderr into a timestamped daily log
file under logs/, so the bot's existing print() calls are also persisted to
disk without changing any print() call site in main.py/brokers.py/BarAggregator.py."""

import json
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


_events = []
_events_lock = threading.Lock()
_MAX_EVENTS = 500


def event(msg):
    """Record a trade-relevant line - fresh entries, adds, exits, orders
    placed, fills, rejections, position changes, square-off. Behaves like
    print() (still goes to the console and the daily log file), and also
    appends to an in-memory buffer that is the ONLY thing the dashboard's
    bottom panel shows. Plain print() calls stay out of that panel."""
    print(msg)
    ts = datetime.now(IST).strftime("%H:%M:%S")
    with _events_lock:
        _events.append((ts, str(msg)))
        if len(_events) > _MAX_EVENTS:
            del _events[: len(_events) - _MAX_EVENTS]


def recent_events(max_items=250):
    """Return recent (HH:MM:SS, message) event pairs, oldest first."""
    with _events_lock:
        return list(_events[-max_items:])


# --------------------------------------------------------------------------
# Delivery-conversion log
#
# Separate from the daily catch-all log: one JSON line per intraday position
# converted to delivery (MIS -> CNC), recording ticker, side, qty and the
# price it was carried at. File: logs/delivery-YYYY-MM-DD.log
# --------------------------------------------------------------------------

_delivery_lock = threading.Lock()


def delivery_log(record):
    """Append one delivery-conversion record as a JSON line to
    logs/delivery-YYYY-MM-DD.log, and emit a human-readable event() line so
    the same conversion also shows in the console, the daily log, and the
    dashboard's LIVE panel.

    Expected record keys: time, ticker, tsym, side, qty, avg_price, ltp,
    source ('auto' = bot's 15:05 gate, 'script' = convert_to_delivery.py)."""
    LOG_DIR.mkdir(exist_ok=True)
    day = datetime.now(IST).strftime("%Y-%m-%d")
    path = LOG_DIR / f"delivery-{day}.log"
    with _delivery_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    who = record.get("ticker") or record.get("tsym") or "?"
    px = record.get("avg_price")
    event(f"[delivery] {who} {record.get('side', '')} x{record.get('qty', '')} "
          f"-> DELIVERY" + (f" @ avg {px}" if px is not None else ""))


def read_delivery_log(day=None, max_items=200):
    """Return the delivery-conversion records for `day` (default today) as a
    list of dicts, oldest first. Empty list if nothing was converted or the
    file doesn't exist."""
    day = day or datetime.now(IST).strftime("%Y-%m-%d")
    path = LOG_DIR / f"delivery-{day}.log"
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except FileNotFoundError:
        return []
    return out[-max_items:]
