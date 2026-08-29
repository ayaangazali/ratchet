"""The approval gate: the last node in the state machine.

Nothing irreversible happens without a human seeing the diff first — no push, no
pull request, no destructive git operation. In a live run this is the harness's
approval interrupt, which is the point: the hold is enforced by the runtime, not by
a sentence in a prompt that a model can talk itself out of.

Locally the decision travels as a file. That is not laziness — if the console dies
mid-demo you can still approve with one `echo`, and that has saved more than one
demo:

    echo '{"allow": true}' > .ratchet/approvals/<request-id>.json
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

POLL_S = 0.3
DEFAULT_TIMEOUT_S = 900


@dataclass
class ApprovalRequest:
    id: str
    action: str  # "open_pull_request" | "push" | ...
    summary: str
    diff: str
    stats: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "action": self.action,
            "summary": self.summary,
            "stats": self.stats,
            "diff_preview": self.diff[:4000],
            "created_at": self.created_at,
        }


@dataclass
class Decision:
    allow: bool
    reason: str = ""
    decided_at: float = field(default_factory=time.time)


class Gate:
    def __init__(self, repo: Path, bus=None) -> None:
        self.dir = Path(repo) / ".ratchet" / "approvals"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.bus = bus

    def request(self, *, action: str, summary: str, diff: str, stats: dict | None = None) -> ApprovalRequest:
        req = ApprovalRequest(uuid.uuid4().hex[:8], action, summary, diff, stats or {})
        (self.dir / f"{req.id}.request.json").write_text(json.dumps(req.to_dict(), indent=2))
        if self.bus:
            self.bus.emit("approval.required", **req.to_dict())
        return req

    def decide(self, request_id: str, allow: bool, reason: str = "") -> None:
        (self.dir / f"{request_id}.json").write_text(json.dumps({"allow": allow, "reason": reason}))

    def wait(self, req: ApprovalRequest, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> Decision:
        path = self.dir / f"{req.id}.json"
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if path.exists():
                try:
                    d = json.loads(path.read_text())
                except json.JSONDecodeError:
                    time.sleep(POLL_S)
                    continue
                dec = Decision(bool(d.get("allow")), d.get("reason", ""))
                if self.bus:
                    self.bus.emit("approval.resolved", id=req.id, approved=dec.allow, reason=dec.reason)
                return dec
            time.sleep(POLL_S)
        dec = Decision(False, "no human responded within the approval window")
        if self.bus:
            self.bus.emit("approval.resolved", id=req.id, approved=False, reason=dec.reason)
        return dec

    def pending(self) -> list[str]:
        return [p.stem.replace(".request", "") for p in self.dir.glob("*.request.json")
                if not (self.dir / f"{p.stem.replace('.request', '')}.json").exists()]
