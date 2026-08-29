"""The Ratchet MCP server -- the only door between the agent and the graded tree.

This is registered with TrueForge as a custom MCP server over streamable-http, so
the harness owns the loop, the context and the approvals, and Ratchet owns the
question "did that actually work". Nothing here calls a model.

Tool surface, and why each one exists:

  task_brief        what the agent is being asked to do, plus the VISIBLE test names
  repo_tree/read/grep   unrestricted reads; cheap, safe, no adjudication
  dry_run           run the suite without committing -- feedback without consequence
  propose_patch     THE state-advancing tool. Runs the full gauntlet, then either
                    commits (ratchet clicks forward) or rolls back to the last green
                    commit and hands the failure back as the next observation
  docs_lookup       current docs/changelog for the exact version in the lockfile
  fan_out           cut N candidate branches; the parent then spawns N subagents
  arbitrate         score every candidate, adopt the winner, discard the rest
  open_pull_request the single irreversible action -- gated on human approval

Note what is missing: there is no `done`, no `finish`, no `mark_complete`. The
agent cannot declare success. `propose_patch` returning ACCEPTED with a FULL
resolution is the only thing that ends a run, and only the pawl can produce that.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .bus import (
    APPROVAL_REQUIRED,
    ARBITRATION,
    ATTEMPT_SUBMITTED,
    FANOUT,
    GATE_RESULT,
    ROLLBACK,
    STALL,
    VERDICT,
    Bus,
)
from .config import Settings, load_task
from .docs_oracle import DocsOracle
from .gauntlet.runner import Backend, Pawl, docker_available
from .ledger import Ledger, git
from .models import Candidate, Decision, RunPhase, RunState
from .workspace import Workspace

try:  # the server is optional at import time so tests and the TUI can import this module
    from mcp.server.fastmcp import FastMCP
except Exception:  # pragma: no cover
    FastMCP = None  # type: ignore


STALL_THRESHOLD = 3


class RatchetService:
    """All state and logic. The MCP layer below is a thin adapter over this, which
    keeps every rule unit-testable without standing up a server."""

    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.repo = Path(settings.repo_path).resolve()
        self.run_id = settings.run_id or f"run-{uuid.uuid4().hex[:6]}"
        self.bus = Bus(self.repo / ".ratchet" / f"{self.run_id}.bus.jsonl")
        self.task = load_task(settings.task_path)
        self.ledger = Ledger.start(self.repo, self.run_id, base=settings.base_ref)
        self.ws = Workspace.create(self.repo, self.run_id)
        backend = Backend.DOCKER if (settings.backend == "docker" and docker_available()) else Backend.LOCAL
        self.pawl = Pawl(backend=backend, image=self.task.image, log_dir=self.repo / ".ratchet" / "logs")
        self.docs = DocsOracle(self.repo, self.bus, settings)
        self.state = RunState(
            run_id=self.run_id,
            task=self.task,
            trunk_branch=self.ledger.trunk,
            last_green_sha=self.ledger.head(),
        )
        self.bus.emit(
            "run.started",
            run_id=self.run_id,
            task=self.task.task_id,
            backend=backend.value,
            trunk=self.ledger.trunk,
            base=self.state.last_green_sha,
        )

    # ------------------------------------------------------------- context --

    def task_brief(self) -> str:
        t = self.task
        visible = "\n".join(f"  - {n}" for n in t.f2p_visible)
        return (
            f"# Task {t.task_id}\n\n{t.statement}\n\n"
            f"## Tests that must go from failing to passing\n{visible}\n\n"
            f"## Rules of this workspace\n"
            f"- You cannot edit anything under {', '.join(t.protected_paths)}. Those paths are reverted to pristine before grading.\n"
            f"- There are additional held-out tests you cannot see. Passing only the visible ones is not enough.\n"
            f"- The only way to change the repository is `propose_patch`. There is no way to declare yourself finished.\n"
            f"- Every rejected attempt is rolled back and returned to you as an observation. Read it before trying again.\n"
        )

    def status(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "phase": self.state.phase.value,
            "task": self.task.task_id,
            "attempts": len(self.state.verdicts),
            "consecutive_rejects": self.state.consecutive_rejects,
            "last_green": self.state.last_green_sha,
            "candidates": {k: (c.best.score if c.best else None) for k, c in self.state.candidates.items()},
            "backend": self.pawl.backend.value,
        }

    # ---------------------------------------------------------------- core --

    def propose_patch(self, diff: str, rationale: str, branch: str = "trunk") -> str:
        """Run the gauntlet. Commit on green, roll back on red. Return the observation."""
        wt = self.ws.path_for(branch if branch != "trunk" else None)
        attempt_id = uuid.uuid4().hex[:8]
        base = self.state.last_green_sha or self.ledger.head()
        self.state.phase = RunPhase.VERIFYING
        self.bus.emit(
            ATTEMPT_SUBMITTED,
            attempt=attempt_id,
            branch=branch,
            rationale=rationale[:400],
            diff_lines=diff.count("\n"),
        )

        verdict = self.pawl.run_gauntlet(
            task=self.task,
            worktree=wt,
            base_commit=base,
            diff=diff,
            branch=branch,
            attempt_id=attempt_id,
            test_sources=self.ws.read_test_sources(None if branch == "trunk" else branch, self.task.protected_paths),
            type_cmd=self.s.type_cmd,
            lint_cmd=self.s.lint_cmd,
        )
        for g in verdict.gates:
            self.bus.emit(GATE_RESULT, attempt=attempt_id, gate=g.name, passed=g.passed, detail=g.detail)

        led = self._ledger_for(branch)
        if verdict.decision is Decision.ACCEPTED:
            sha = led.commit_attempt(f"ratchet: {rationale[:60]} [{attempt_id}]", verdict)
            verdict.commit_sha = sha
            if branch == "trunk":
                self.state.last_green_sha = sha
                self.state.consecutive_rejects = 0
            self.state.phase = RunPhase.WORKING
        else:
            led.park_rejected(attempt_id)
            target = self.state.last_green_sha or base
            led.rollback(target)
            verdict.rolled_back_to = target[:10]
            self.bus.emit(ROLLBACK, attempt=attempt_id, to=target[:10], reason=verdict.decision.value)
            if branch == "trunk":
                self.state.consecutive_rejects += 1
            led.append(verdict, None)

        self.state.verdicts.append(verdict)
        if branch in self.state.candidates:
            self.state.candidates[branch].verdicts.append(verdict)
        self.bus.emit(VERDICT, **verdict.to_dict())

        obs = verdict.to_observation()

        # augment the observation with fresh docs when the failure smells like drift
        hint = self.docs.hint_for_failure(verdict.stdout_tail)
        if hint:
            obs += "\n\n" + hint

        if self.state.consecutive_rejects >= STALL_THRESHOLD and branch == "trunk":
            self.state.phase = RunPhase.STALLED
            self.bus.emit(STALL, attempts=self.state.consecutive_rejects)
            obs += (
                f"\n\n[STALL DETECTED] {self.state.consecutive_rejects} consecutive rejections on trunk. "
                "Stop trying variations of the same idea. Call fan_out with 3 materially different "
                "hypotheses, spawn one sub-agent per candidate branch with create_sub_agent, then call "
                "arbitrate when they report back."
            )
        if verdict.decision is Decision.ACCEPTED and verdict.resolution.value == "RESOLVED_FULL":
            obs += (
                "\n\n[TASK COMPLETE] Every gate is green, including the held-out tests. "
                "The only remaining step is open_pull_request, which requires a human to approve it."
            )
        return obs

    def dry_run(self, branch: str = "trunk") -> str:
        """Grade the working tree without committing or rolling back. Free feedback."""
        wt = self.ws.path_for(branch if branch != "trunk" else None)
        led = self._ledger_for(branch)
        diff = led.working_diff()
        if not diff.strip():
            return "Working tree is identical to the last green commit; nothing to grade."
        v = self.pawl.run_gauntlet(
            task=self.task,
            worktree=wt,
            base_commit=self.state.last_green_sha or led.head(),
            diff=diff,
            branch=branch,
            test_sources=self.ws.read_test_sources(None if branch == "trunk" else branch, self.task.protected_paths),
            type_cmd=None,
            lint_cmd=None,
        )
        self.bus.emit(VERDICT, dry_run=True, **v.to_dict())
        return "[DRY RUN - nothing was committed or rolled back]\n" + v.to_observation()

    # ------------------------------------------------------------ fan-out ---

    def fan_out(self, labels: list[str], plan: str) -> str:
        base = self.state.last_green_sha or self.ledger.head()
        made = []
        for label in labels[:5]:
            branch = f"ratchet/{self.run_id}/{label}"
            self.ws.add(label, branch, base)
            self.state.candidates[label] = Candidate(label=label, branch=branch)
            made.append(label)
        self.state.phase = RunPhase.FANNED_OUT
        self.bus.emit(FANOUT, labels=made, base=base[:10], plan=plan[:500])
        lines = [
            f"Cut {len(made)} candidate branches from the last green commit {base[:10]}.",
            "Each one is an isolated worktree with its own history. Spawn one sub-agent per candidate now.",
            "Every sub-agent must pass its own label to propose_patch, e.g. propose_patch(branch=\"cand-a\", ...).",
            "Sub-agents cannot see this conversation, so restate the task and the hypothesis in full.",
            "",
        ]
        for label in made:
            lines.append(f"  {label}: branch {self.state.candidates[label].branch}")
        lines.append("\nWhen they have all reported, call arbitrate.")
        return "\n".join(lines)

    def arbitrate(self) -> str:
        """Score every candidate and adopt the best. The model does not get a vote."""
        rows = []
        for label, c in self.state.candidates.items():
            b = c.best
            rows.append(
                {
                    "label": label,
                    "score": round(b.score, 4) if b else 0.0,
                    "hidden": round(b.f2p_hidden_rate, 3) if b else 0.0,
                    "visible": round(b.f2p_visible_rate, 3) if b else 0.0,
                    "p2p": round(b.p2p_rate, 3) if b else 0.0,
                    "delta": round(b.delta, 3) if b else 0.0,
                    "findings": [f.rule for f in b.findings] if b else [],
                    "decision": b.decision.value if b else "no attempt",
                    "files": b.files_touched if b else 0,
                }
            )
        rows.sort(key=lambda r: (-r["score"], r["files"]))
        self.bus.emit(ARBITRATION, rows=rows)
        if not rows or rows[0]["score"] <= 0:
            self.state.phase = RunPhase.WORKING
            return "No candidate scored above zero. Nothing adopted; the trunk is unchanged.\n" + _table(rows)
        winner = rows[0]["label"]
        sha = self.ledger.adopt(self.state.candidates[winner].branch)
        self.state.last_green_sha = sha
        self.state.consecutive_rejects = 0
        self.state.phase = RunPhase.WORKING
        for label in list(self.state.candidates):
            if label != winner:
                self.ws.remove(label)
        return (
            _table(rows)
            + f"\n\nAdopted {winner} onto {self.ledger.trunk} at {sha[:10]}. "
            "The other candidates are discarded but still reachable by branch name."
        )

    # ---------------------------------------------------------- irreversible --

    def open_pull_request(self, title: str, body: str) -> str:
        """The one action that leaves the machine. Approval is enforced by the harness."""
        self.state.phase = RunPhase.AWAITING_APPROVAL
        self.bus.emit(APPROVAL_REQUIRED, title=title, branch=self.ledger.trunk)
        remote_branch = self.ledger.trunk
        git("push", "-u", self.s.remote, remote_branch, cwd=self.repo)
        env = {**os.environ}
        proc = subprocess.run(
            ["gh", "pr", "create", "--title", title, "--body", body, "--head", remote_branch, "--base", self.s.base_branch],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            env=env,
        )
        self.state.phase = RunPhase.DONE
        out = (proc.stdout or "") + (proc.stderr or "")
        self.bus.emit("approval.resolved", approved=True, result=out.strip()[:400])
        return out.strip() or "pull request created"

    # -------------------------------------------------------------- helpers --

    def _ledger_for(self, branch: str) -> Ledger:
        if branch == "trunk":
            return self.ledger
        wt = self.ws.path_for(branch)
        return Ledger(repo=wt, run_id=self.run_id, trunk=f"ratchet/{self.run_id}/{branch}")


def _table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no candidates)"
    head = f"{'cand':<8}{'score':>8}{'hidden':>8}{'visible':>9}{'p2p':>7}{'delta':>7}  findings"
    lines = [head, "-" * len(head)]
    for r in rows:
        lines.append(
            f"{r['label']:<8}{r['score']:>8.3f}{r['hidden']:>8.2f}{r['visible']:>9.2f}"
            f"{r['p2p']:>7.2f}{r['delta']:>7.2f}  {','.join(r['findings']) or '-'}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# MCP adapter
# --------------------------------------------------------------------------- #


def build_server(settings: Settings) -> Any:
    if FastMCP is None:  # pragma: no cover
        raise RuntimeError("pip install 'mcp[cli]' to run the Ratchet MCP server")
    svc = RatchetService(settings)
    mcp = FastMCP("ratchet", stateless_http=True)

    @mcp.tool()
    def task_brief() -> str:
        """The task you are working on, the visible tests, and the rules of this workspace."""
        return svc.task_brief()

    @mcp.tool()
    def repo_tree(path: str = ".", depth: int = 3, branch: str = "trunk") -> str:
        """List files in the graded worktree."""
        return svc.ws.tree(None if branch == "trunk" else branch, path, depth)

    @mcp.tool()
    def repo_read(path: str, start: int = 1, end: int = 0, branch: str = "trunk") -> str:
        """Read a file with line numbers. `end=0` means to the end of the file."""
        return svc.ws.read(None if branch == "trunk" else branch, path, start, end or None)

    @mcp.tool()
    def repo_grep(pattern: str, glob: str = "*", branch: str = "trunk") -> str:
        """Regex search across the graded worktree."""
        return svc.ws.grep(None if branch == "trunk" else branch, pattern, glob)

    @mcp.tool()
    def dry_run(branch: str = "trunk") -> str:
        """Grade the current working tree WITHOUT committing or rolling back."""
        return svc.dry_run(branch)

    @mcp.tool()
    def propose_patch(diff: str, rationale: str, branch: str = "trunk") -> str:
        """Submit a unified diff for adjudication. This is the only way to change the
        repository. On green the commit sticks; on red everything is rolled back to the
        last green commit and the failure comes back to you as your next observation."""
        return svc.propose_patch(diff, rationale, branch)

    @mcp.tool()
    def docs_lookup(library: str, symbol: str = "", topic: str = "") -> str:
        """Current upstream documentation or changelog for a dependency, pinned to the
        exact version in this repository's lockfile."""
        return svc.docs.lookup(library, symbol=symbol, topic=topic)

    @mcp.tool()
    def fan_out(labels: list[str], plan: str) -> str:
        """Cut isolated candidate branches from the last green commit, one per hypothesis."""
        return svc.fan_out(labels, plan)

    @mcp.tool()
    def arbitrate() -> str:
        """Score every candidate branch with the verifier and adopt the highest."""
        return svc.arbitrate()

    @mcp.tool()
    def status() -> str:
        """Where the run currently stands."""
        return json.dumps(svc.status(), indent=2)

    @mcp.tool()
    def open_pull_request(title: str, body: str) -> str:
        """Push the branch and open a pull request. IRREVERSIBLE: leaves this machine.
        The harness will hold this call until a human approves it."""
        return svc.open_pull_request(title, body)

    return mcp, svc


def main() -> None:  # pragma: no cover
    settings = Settings.from_env()
    mcp, _svc = build_server(settings)
    mcp.settings.host = settings.mcp_host
    mcp.settings.port = settings.mcp_port
    mcp.run(transport="streamable-http")


if __name__ == "__main__":  # pragma: no cover
    main()
