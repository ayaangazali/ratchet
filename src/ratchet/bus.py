"""An append-only JSONL event bus.

Three processes need to see the same run: the MCP server (which the harness calls),
the orchestrator (which drives the session) and the TUI (which draws it). A socket
would be the obvious answer and the wrong one for a one-day build -- a file that
everyone appends to and tails is crash-safe, restart-safe, greppable after the demo,
and impossible to get subtly wrong at 3am.

Ordering is guaranteed by O_APPEND on a single file. Readers poll by byte offset,
so a reader that starts late still sees the whole run.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Event:
    kind: str
    payload: dict[str, Any]
    ts: float

    @staticmethod
    def from_line(line: str) -> Event:
        d = json.loads(line)
        return Event(d["kind"], d.get("payload", {}), d.get("ts", 0.0))


class Bus:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._offset = 0

    def emit(self, kind: str, **payload: Any) -> None:
        line = json.dumps({"kind": kind, "payload": payload, "ts": time.time()}, default=str)
        with open(self.path, "a") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def read_all(self) -> list[Event]:
        return [Event.from_line(line) for line in self.path.read_text().splitlines() if line.strip()]

    def tail(self) -> Iterator[Event]:
        """Yield events appended since the last call. Non-blocking."""
        with open(self.path) as fh:
            fh.seek(self._offset)
            for line in fh:
                if line.endswith("\n") and line.strip():
                    yield Event.from_line(line)
            self._offset = fh.tell()


# Event kinds the TUI knows how to draw. Keep this list short and stable.
RUN_STARTED = "run.started"
PHASE = "run.phase"
AGENT_TEXT = "agent.text"
AGENT_TOOL = "agent.tool"
ATTEMPT_SUBMITTED = "attempt.submitted"
GATE_STARTED = "gate.started"
GATE_RESULT = "gate.result"
VERDICT = "verdict"
ROLLBACK = "rollback"
STALL = "stall"
FANOUT = "fanout"
ARBITRATION = "arbitration"
APPROVAL_REQUIRED = "approval.required"
APPROVAL_RESOLVED = "approval.resolved"
DOCS_FETCH = "docs.fetch"
DOCS_HEAL = "docs.heal"
RUN_DONE = "run.done"
