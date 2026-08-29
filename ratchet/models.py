"""Core data types.

Everything that crosses a process boundary -- a tool result, an SSE payload, a
tree file on disk, a TUI update -- is one of these. Plain dataclasses with
`to_dict()`, because the tree has to survive a restart and pydantic is not worth
the import time on the hot path.

Vocabulary, borrowed from the SWE-bench harness on purpose:
  F2P   fail-to-pass    tests that must flip red -> green for the task to be done
  P2P   pass-to-pass    tests that were green and must stay green (regressions)
  HIDDEN                a held-out slice of F2P the agent is never shown
  CANARY                a task whose tests contradict each other; green means cheated
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #


class TestStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"
    XFAIL = "XFAIL"


class Severity(str, Enum):
    CRITICAL = "critical"  # hard gate: blocks the commit
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Outcome(str, Enum):
    """What the gauntlet decided about a candidate patch."""

    GREEN = "green"  # every gate passed; this node can end the run
    PROGRESS = "progress"  # no regression, score improved or held; keep it in the tree
    REGRESSED = "regressed"  # pass-to-pass broke; prune
    CHEATED = "cheated"  # integrity violation; prune loudly
    BROKEN = "broken"  # build or patch-apply failed; prune
    INFRA = "infra"  # our fault; retry, do not count against the agent


# --------------------------------------------------------------------------- #
# tasks
# --------------------------------------------------------------------------- #


@dataclass
class TaskSpec:
    """A unit of work the gauntlet can grade.

    `f2p_hidden` is the held-out slice. It counts toward `f2p_ratio` exactly like
    the visible tests, so a patch that special-cases what it was shown loses score
    rather than winning -- but its names never appear in anything the agent reads.
    """

    task_id: str
    repo_path: str
    statement: str
    test_cmd: str = "python -m pytest -rA"
    framework: str = "pytest"  # pytest | jest | vitest | gotest | cargo
    build_cmd: str | None = None
    type_cmd: str | None = None
    lint_cmd: str | None = None
    f2p_visible: list[str] = field(default_factory=list)
    f2p_hidden: list[str] = field(default_factory=list)
    p2p: list[str] = field(default_factory=list)
    protected_paths: list[str] = field(default_factory=lambda: ["tests/"])
    allowed_paths: list[str] = field(default_factory=list)  # diff hygiene: empty = anything unprotected
    is_canary: bool = False
    timeout_s: int = 600
    image: str = "ratchet-task:latest"
    setup_cmd: str | None = None

    @property
    def f2p_all(self) -> list[str]:
        return [*self.f2p_visible, *self.f2p_hidden]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# verifier
# --------------------------------------------------------------------------- #


@dataclass
class StageResult:
    """One stage of the gauntlet."""

    name: str  # build | cheat | f2p | p2p | types | lint | hygiene
    passed: bool
    value: float = 0.0  # the stage's own 0..1 contribution before weighting
    detail: str = ""
    duration_s: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)
    skipped: bool = False

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

    def one_line(self) -> str:
        return f"{self.rule} at {self.path}:{self.line} -- {self.explanation}"


@dataclass
class GauntletResult:
    """The verifier's answer. The only thing allowed to say a patch worked."""

    outcome: Outcome
    score: float
    green: bool
    stages: dict[str, StageResult] = field(default_factory=dict)
    findings: list[CheatFinding] = field(default_factory=list)
    f2p_ratio: float = 0.0
    f2p_visible_ratio: float = 0.0
    f2p_hidden_ratio: float = 0.0
    p2p_intact: bool = True
    delta: float = 0.0  # visible minus hidden: the overfitting tell
    files_touched: int = 0
    diff_lines: int = 0
    last_failure: str = ""
    reason: str = ""
    duration_s: float = 0.0
    created_at: float = field(default_factory=time.time)

    @property
    def regressed(self) -> bool:
        return self.outcome in (Outcome.REGRESSED, Outcome.CHEATED, Outcome.BROKEN)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["outcome"] = self.outcome.value
        d["stages"] = {k: v.to_dict() for k, v in self.stages.items()}
        d["findings"] = [f.to_dict() for f in self.findings]
        return d

    def summary(self) -> str:
        f2p = self.stages.get("f2p")
        p2p = self.stages.get("p2p")
        bits = [f"score {self.score:.2f}"]
        if f2p:
            bits.append(f"f2p {f2p.detail}")
        if p2p:
            bits.append(f"p2p {p2p.detail}")
        if self.findings:
            bits.append(f"cheat: {self.findings[0].rule}")
        return " · ".join(bits)

    def to_observation(self) -> str:
        """What the model sees next. Short, factual, actionable -- this text is the
        agent's entire feedback channel and long verdicts blow the context window."""
        head = {
            Outcome.GREEN: "GREEN - every gate passed",
            Outcome.PROGRESS: "KEPT - no regression, node added to the tree",
            Outcome.REGRESSED: "PRUNED - a passing test broke; this branch is dead",
            Outcome.CHEATED: "PRUNED - integrity violation; this branch is dead",
            Outcome.BROKEN: "PRUNED - the patch did not apply or did not build",
            Outcome.INFRA: "INFRA FAILURE - not your fault, retry",
        }[self.outcome]
        lines = [f"[{head}]  score={self.score:.3f}"]
        for name in ("build", "cheat", "f2p", "p2p", "types", "lint", "hygiene"):
            st = self.stages.get(name)
            if not st:
                continue
            mark = "skip" if st.skipped else ("PASS" if st.passed else "FAIL")
            lines.append(f"  {mark:4}  {name:<8} {st.detail}")
        for f in self.findings:
            lines.append(f"  [{f.severity.value}] {f.one_line()}")
        if self.last_failure:
            lines.append("  --- failure ---")
            lines.append(self.last_failure)
        return "\n".join(lines)
