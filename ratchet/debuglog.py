"""A debug channel with two ends: an in-memory ring the console can draw, and a
file under `.ratchet/` you can tail after the fact.

Exists because a swallowed worker exception is invisible: the activity pane keeps
its last line forever and the user cannot tell a slow model from a dead one. Every
provider request, worker transition and exception goes through here, redacted.
"""

from __future__ import annotations

import logging
import os
import time
import traceback
from collections import deque
from collections.abc import Callable
from pathlib import Path

from .providers import redact

MAX_LINES = 500
_ring: deque[tuple[float, str, str]] = deque(maxlen=MAX_LINES)  # (ts, level, text)
_subscribers: list[Callable[[float, str, str], None]] = []
_file: Path | None = None


def configure(repo: Path) -> Path:
    """Point the file end at this run's directory. Idempotent."""
    global _file
    d = Path(repo) / ".ratchet"
    d.mkdir(parents=True, exist_ok=True)
    _file = d / "debug.log"
    return _file


def subscribe(fn: Callable[[float, str, str], None]) -> None:
    _subscribers.append(fn)


def log(level: str, text: str) -> None:
    """Never raises: a debug channel that can break the thing it observes is worse
    than none. Everything is redacted before it is stored or written."""
    entry = (time.time(), level.upper(), redact(str(text))[:400])
    _ring.append(entry)
    if _file is not None:
        try:
            with _file.open("a") as fh:
                fh.write(f"{time.strftime('%H:%M:%S', time.localtime(entry[0]))} {entry[1]:<5} {entry[2]}\n")
        except OSError:
            pass
    for fn in list(_subscribers):
        try:
            fn(*entry)
        except Exception:
            pass


def exception(prefix: str, exc: BaseException) -> None:
    log("error", f"{prefix}: {type(exc).__name__}: {exc}")
    for line in traceback.format_exception(type(exc), exc, exc.__traceback__)[-4:]:
        log("trace", line.rstrip())


def lines() -> list[tuple[float, str, str]]:
    return list(_ring)


def enabled() -> bool:
    return os.environ.get("RATCHET_DEBUG", "") not in ("", "0", "false", "no")


class _Handler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        # our own POST/← lines already say model, cap, status and timing
        if record.name == "httpx" and msg.startswith("HTTP Request:"):
            return
        log(record.levelname, f"{record.name}: {msg}")


def install_logging() -> None:
    """Route library logging into the panel, at levels that stay readable.

    httpcore narrates every socket state change ("request=<Request [b'POST']>"
    fourteen times a call), which buried the lines that matter. It is kept for
    warnings and errors -- where a connection problem actually shows up -- while
    httpx's one-line-per-request stays visible.
    """
    h = _Handler()
    levels = {"httpx": logging.INFO, "httpcore": logging.WARNING, "ratchet": logging.DEBUG}
    for name, level in levels.items():
        lg = logging.getLogger(name)
        lg.setLevel(level)
        lg.propagate = False
        if not any(isinstance(x, _Handler) for x in lg.handlers):
            lg.addHandler(h)
