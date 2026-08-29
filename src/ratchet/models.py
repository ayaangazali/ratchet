"""Core data types for Ratchet.

Everything that crosses a process boundary (MCP tool result, SSE payload, ledger
line, TUI update) is one of these. They are plain dataclasses with `to_dict()` so
they serialise without pydantic in the hot path.

Vocabulary (borrowed from the SWE-bench harness, deliberately):
  F2P  fail-to-pass   tests that must flip from red to green for the task to be done
  P2P  pass-to-pass   tests that were green before and must stay green (regressions)
  HIDDEN               a held-out slice of F2P the agent never sees; the anti-cheat signal
  CANARY               a task whose tests contradict the spec; passing it IS the cheat
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

# --------------------------------------------------------------------------- #
# test-level
# --------------------------------------------------------------------------- #


class TestStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"
    XFAIL = "XFAIL"


class Resolution(str, Enum):
    FULL = "RESOLVED_FULL"
    PARTIAL = "RESOLVED_PARTIAL"
    NO = "RESOLVED_NO"


class Decision(str, Enum):
    """What the pawl did with an attempt."""

    ACCEPTED = "accepted"  # green, committed, ratchet advanced
    REJECTED = "rejected"  # red, rolled back to last green
    DISQUALIFIED = "disqualified"  # cheat detected, rolled back + flagged loudly
    INFRA = "infra_failure"  # our fault, not the agent's; no penalty, no advance


class Severity(str, Enum):
    CRITICAL = "critical"  # auto-disqualify
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# --------------------------------------------------------------------------- #
# tasks
# --------------------------------------------------------------------------- #


@dataclass
class TaskSpec:
    """A unit of work the ratchet can verify.

    `f2p_visible` is what the agent is told about. `f2p_hidden` is held out and only
    the pawl ever sees it -- the gap between the two is the reward-hacking signal.
    """

    task_id: str
    repo_path: str
    statement: str
    test_cmd: str = "python -m pytest -rA"
    f2p_visible: list[str] = field(default_factory=list)
    f2p_hidden: list[str] = field(default_factory=list)
    p2p: list[str] = field(default_factory=list)
    #: files reverted to pristine before grading -- agent edits here are erased
    protected_paths: list[str] = field(default_factory=lambda: ["tests/"])
    #: a canary task is *unsatisfiable*; any green verdict means the agent cheated
    is_canary: bool = False
    timeout_s: int = 600
    setup_cmd: str | None = None
    image: str = "ratchet-task:latest"

    @property
    def f2p_all(self) -> list[str]:
        return [*self.f2p_visible, *self.f2p_hidden]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# verdicts
# --------------------------------------------------------------------------- #


@dataclass
class GateResult:
    """One lane of the gauntlet."""

    name: str  # "build" | "f2p" | "p2p" | "hidden" | "types" | "lint" | "cheat"
    passed: bool
    detail: str = ""
    duration_s: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CheatFinding:
    rule: str
    severity: Severity
    path: str
    line: int
    evidence: str
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d


@dataclass
class Verdict:
    """The only thing in Ratchet allowed to say a step succeeded.

    Produced by the pawl, never by the model. Serialised straight into the tool
    response the agent receives as its next observation.
    """

    attempt_id: str
    task_id: str
    branch: str
    decision: Decision
    score: float
    gates: list[GateResult] = field(default_factory=list)
    findings: list[CheatFinding] = field(default_factory=list)
    f2p_visible_rate: float = 0.0
    f2p_hidden_rate: float = 0.0
    p2p_rate: float = 0.0
    delta: float = 0.0  # visible - hidden; the SpecBench reward-hacking gap
    resolution: Resolution = Resolution.NO
    files_touched: int = 0
    diff_lines: int = 0
    commit_sha: str | None = None
    rolled_back_to: str | None = None
    stdout_tail: str = ""
    created_at: float = field(default_factory=time.time)
    duration_s: float = 0.0

    @property
    def green(self) -> bool:
        return self.decision is Decision.ACCEPTED

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["decision"] = self.decision.value
        d["resolution"] = self.resolution.value
        d["gates"] = [g.to_dict() for g in self.gates]
        d["findings"] = [f.to_dict() for f in self.findings]
        return d

    # The agent only ever sees this. Keep it short, factual and actionable: it is
    # literally the next observation in the loop, and long verdicts blow context.
    def to_observation(self) -> str:
        lines: list[str] = []
        head = {
            Decision.ACCEPTED: "ACCEPTED - ratchet advanced",
            Decision.REJECTED: "REJECTED - rolled back to last green",
            Decision.DISQUALIFIED: "DISQUALIFIED - integrity violation, rolled back",
            Decision.INFRA: "INFRA FAILURE - not your fault, retry",
        }[self.decision]
        lines.append(f"[{head}]  attempt={self.attempt_id}  branch={self.branch}  score={self.score:.3f}")
        for g in self.gates:
            mark = "PASS" if g.passed else "FAIL"
            lines.append(f"  {mark:4}  {g.name:<8} {g.detail}")
        if self.findings:
            lines.append("  integrity findings:")
            for f in self.findings:
                lines.append(f"    [{f.severity.value}] {f.rule} at {f.path}:{f.line} -- {f.explanation}")
        if self.decision is Decision.REJECTED:
            lines.append(f"  HEAD is back at {self.rolled_back_to}. Your edits are preserved on refs/ratchet/rejected/{self.attempt_id}.")
        if self.stdout_tail:
            lines.append("  --- last 40 lines of test output ---")
            lines.append(self.stdout_tail)
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# run state
# --------------------------------------------------------------------------- #


@dataclass
class Attempt:
    attempt_id: str
    branch: str
    diff: str
    rationale: str
    parent_sha: str
    created_at: float = field(default_factory=time.time)

    @staticmethod
    def new(branch: str, diff: str, rationale: str, parent_sha: str) -> Attempt:
        return Attempt(uuid.uuid4().hex[:8], branch, diff, rationale, parent_sha)


@dataclass
class Candidate:
    """One parallel branch during a fan-out."""

    label: str  # "cand-a"
    branch: str  # "ratchet/run-xyz/cand-a"
    thread_id: str | None = None
    verdicts: list[Verdict] = field(default_factory=list)

    @property
    def best(self) -> Verdict | None:
        return max(self.verdicts, key=lambda v: v.score, default=None)


class RunPhase(str, Enum):
    IDLE = "idle"
    WORKING = "working"
    VERIFYING = "verifying"
    STALLED = "stalled"
    FANNED_OUT = "fanned_out"
    AWAITING_APPROVAL = "awaiting_approval"
    DONE = "done"
    FAILED = "failed"


@dataclass
class RunState:
    run_id: str
    task: TaskSpec
    phase: RunPhase = RunPhase.IDLE
    session_id: str | None = None
    turn_id: str | None = None
    last_sequence_number: int = 0
    trunk_branch: str = ""
    last_green_sha: str | None = None
    consecutive_rejects: int = 0
    verdicts: list[Verdict] = field(default_factory=list)
    candidates: dict[str, Candidate] = field(default_factory=dict)
    pending_approval: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task.task_id,
            "phase": self.phase.value,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "last_sequence_number": self.last_sequence_number,
            "trunk_branch": self.trunk_branch,
            "last_green_sha": self.last_green_sha,
            "consecutive_rejects": self.consecutive_rejects,
            "verdicts": [v.to_dict() for v in self.verdicts],
        }
