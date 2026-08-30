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
        log(record.levelname, f"{record.name}: {record.getMessage()}")


def install_logging() -> None:
    """Route httpx's own logging (connect, timeout, retry) into the panel."""
    h = _Handler()
    for name in ("httpx", "httpcore", "ratchet"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.DEBUG if enabled() else logging.INFO)
        if not any(isinstance(x, _Handler) for x in lg.handlers):
            lg.addHandler(h)
