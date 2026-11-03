"""Logging setup — a dedicated file per run, in an easy-to-read format.

Usage:
    from log_setup import setup_run_logging
    log_path = setup_run_logging(ctx)   # ctx is the JobContext

Every run (every call/job) gets its own file inside the logs/ folder, in the
form: logs/2026-07-21_14-30-05_room-name_AJ_xxxx.log
"""

import logging
import re
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"

# The names of all standard fields generated with any LogRecord, so we can
# tell apart the "extra" fields (like room or job_id) and print them
# separately at the end of the line.
_STANDARD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {
    "message", "asctime", "taskName",
}


class ReadableFormatter(logging.Formatter):
    """A clear text format: time | level | logger name | message, plus any extra fields at the end of the line."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in _STANDARD_ATTRS and not k.startswith("_")
        }
        if extras:
            extras_str = " ".join(f"{k}={v}" for k, v in sorted(extras.items()))
            base = f"{base}  ⟨{extras_str}⟩"
        return base


class _StaticFieldsFilter(logging.Filter):
    """Adds fixed fields (room, job_id) to every log line, even if the message comes from another library."""

    def __init__(self, fields: dict) -> None:
        super().__init__()
        self._fields = fields

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in self._fields.items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


def _safe_slug(text: str) -> str:
    """Converts any text into a safe filename (letters/digits/hyphen only)."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-") or "unknown"


def setup_run_logging(ctx, *, quiet_loggers: tuple[str, ...] = ("httpx", "httpcore", "openai")) -> Path:
    """Sets up a dedicated log file for this run and returns its path.

    - Adds a FileHandler to the root logger, so it captures logs from every
      library (agent.py, db.py, livekit, etc.) in the same file.
    - Leaves any handlers that already existed (like the console) working normally.
    - quiet_loggers: names of loggers known to be very verbose (like
      httpx, which prints a line per HTTP request); we raise their
      level to INFO in the file only, to keep the file readable. Pass
      quiet_loggers=() if you want absolutely everything.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    room = _safe_slug(getattr(ctx.room, "name", "no-room"))
    job_id = _safe_slug(getattr(ctx.job, "id", "no-job"))
    log_path = LOG_DIR / f"{ts}_{room}_{job_id}.log"

    formatter = ReadableFormatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-24s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)
    file_handler.addFilter(_StaticFieldsFilter({"room": room, "job_id": job_id}))

    root = logging.getLogger()
    root.addHandler(file_handler)
    # Make sure messages are generated at DEBUG level so they reach the file
    if root.level > logging.DEBUG:
        root.setLevel(logging.DEBUG)

    for name in quiet_loggers:
        logging.getLogger(name).setLevel(logging.INFO)

    logging.getLogger("log_setup").info(
        "run log file: %s", log_path
    )
    return log_path
