"""The pipeline: what working on a repository looks like, start to finish.

Harness routes the work, sub-agents write the patch, the verifier decides whether
it may stick, a human clears the gate, the pull request opens, Qodo reviews it,
the findings come back as more work, and the loop closes. Every stage is a bus
event, so the console, the dashboard and a replay all see the same run.

`ratchet pipeline --demo` drives it with a scripted script instead of live
services, which is the only way to show the whole shape in a minute without four
accounts and a network. The demo is labelled as one on screen -- a rehearsal that
pretends to be a performance is worth nothing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .bus import Bus

# Findings the review stage reports in demo mode. These are real: every one was
# raised by Qodo against this repository today, and each is quoted as it arrived.
DEMO_FINDINGS = [
    {
        "severity": "high",
        "title": "Ignored protected files survive the reset",
        "detail": "git clean -fdq preserves ignored files, so a created ignored file under a "
                  "protected directory survives the pre-run reset and can still affect grading.",
        "fix": "clean with -x inside protected paths",
    },
    {
        "severity": "high",
        "title": "End marker is forgeable",
        "detail": "parse_exit_code splits on the first end marker, so suite code can print a "
                  "forged END followed by exit code 0 and be graded on it.",
        "fix": "bound the trusted region at the last end marker",
    },
    {
        "severity": "medium",
        "title": "Empty truncation audits clean",
        "detail": "verify() only requires a seal when at least one receipt remains, so truncating "
                  "the receipt file to zero bytes returns success.",
        "fix": "an empty chain is a problem, not a pass",
    },
]

DEMO_STAGES = [
    ("cheat", True, "0 finding(s), 0 critical"),
    ("build", True, "ok"),
    ("f2p", True, "6/6"),
    ("p2p", True, "118/118"),
    ("types", True, "clean"),
    ("lint", True, "clean"),
    ("hygiene", True, "2 file(s), 61 added lines"),
]


@dataclass
class Pace:
    """How long the demo dwells on each beat. Zero runs it instantly, which is
    what the tests use; the default is readable at a desk."""

    beat: float = 0.45

    def wait(self, factor: float = 1.0) -> None:
        if self.beat:
            time.sleep(self.beat * factor)


class PipelineRun:
    def __init__(self, repo: Path, bus: Bus, *, run_id: str, pace: Pace | None = None,
                 task: str = "fix the protected-path reset", demo: bool = True) -> None:
        self.repo = Path(repo)
        self.bus = bus
        self.run_id = run_id
        self.pace = pace or Pace()
        self.task = task
        self.demo = demo

    def emit(self, kind: str, **payload) -> None:
        self.bus.emit(kind, **payload)
        self.pace.wait()

    # --------------------------------------------------------------- the run --

    def run(self) -> dict:
        self.emit("run.started", run_id=self.run_id, task=self.task,
                  provider="trueforge", snapshots=True, demo=self.demo)

        # 1. the harness maps the repository once, on a cheap model
        self.emit("repo.mapped", lines=24)

        # 2. a candidate: sub-agent writes it, the verifier decides
        self.emit("expand", node="root", fanout=1, depth=0, dead_ends=0)
        self.emit("sandbox.created", label="root-0", provider="trueforge")
        self.emit("verify.started", label="root-0", model="truefoundry/openai/gpt-5.2",
                  intent="revert protected paths one at a time so a missing path cannot abort the rest")
        for stage, ok, detail in DEMO_STAGES[:1]:
            self.emit("stage.result", label="root-0", stage=stage, passed=ok, detail=detail)
        # the first attempt regresses: the search is a search because this happens
        self.emit("stage.result", label="root-0", stage="build", passed=True, detail="ok")
        self.emit("stage.result", label="root-0", stage="f2p", passed=False, detail="4/6  (hidden failing)")
        self.emit("stage.result", label="root-0", stage="p2p", passed=False, detail="117/118")
        self.emit("node.pruned", id="9ba4", score=0.46, green=False,
                  reason="1 previously-passing test now fails", model="truefoundry/openai/gpt-5.2")

        # 3. the dead end is fed back, and a second provider tries a different idea
        self.emit("stall", node="root", fanout=2, depth=0)
        self.emit("expand", node="root", fanout=2, depth=0, dead_ends=1)
        self.emit("sandbox.created", label="root-1", provider="trueforge")
        self.emit("verify.started", label="root-1", model="trueforge/claude-sonnet-4-6",
                  intent="checkout each protected path separately, guarded by ls-tree")
        for stage, ok, detail in DEMO_STAGES:
            self.emit("stage.result", label="root-1", stage=stage, passed=ok, detail=detail)
        self.emit("node.added", id="ae2c", score=1.0, green=True,
                  intent="per-path revert, guarded", model="trueforge/claude-sonnet-4-6")

        # 4. nothing leaves the machine without a human
        self.emit("approval.required", action="open_pull_request",
                  summary="fix(verifier): revert protected paths one at a time",
                  stats={"nodes": 3, "pruned": 1, "score": 1.0})
        self.pace.wait(2)
        self.emit("approval.resolved", approved=True)

        # 5. the pull request, and the review that decides whether it merges
        pr = "#118"
        self.emit("pr.opened", pr=pr, title="fix(verifier): revert protected paths one at a time",
                  url="https://github.com/ayaangazali/ratchet/pull/118")
        self.emit("review.started", pr=pr, files=2, reviewer="qodo")
        for f in DEMO_FINDINGS:
            self.emit("review.finding", pr=pr, severity=f["severity"], title=f["title"], detail=f["detail"])
        self.emit("review.done", pr=pr, findings=len(DEMO_FINDINGS))

        # 6. a finding is work, not a verdict: it goes back through the same loop
        for f in DEMO_FINDINGS:
            self.emit("fix.started", finding=f["title"])
            self.emit("sandbox.created", label=f"fix-{f['title'][:8]}", provider="trueforge")
            self.emit("verify.started", label="fix", model="truefoundry/openai/gpt-5.2", intent=f["fix"])
            self.emit("stage.result", label="fix", stage="cheat", passed=True, detail="0 finding(s), 0 critical")
            self.emit("stage.result", label="fix", stage="f2p", passed=True, detail="7/7")
            self.emit("fix.done", summary=f"{f['fix']} — pushed to {pr}")

        # 7. clean review, then merge
        self.emit("review.started", pr=pr, files=3, reviewer="qodo")
        self.emit("review.done", pr=pr, findings=0)
        self.emit("pr.merged", pr=pr)
        self.emit("run.done", winner="ae2c", green=True,
                  reason="merged after review; every finding answered", nodes=3)
        return {"pr": pr, "findings": len(DEMO_FINDINGS), "green": True}
