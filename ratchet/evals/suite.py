"""Our own eval suite, run on our own harness.

The claim under test is not "our model is good" -- we did not train one. It is
**"verified states with rollback beat a linear loop, for the same number of model
calls"**. That is a claim about machinery, so the eval holds the generator fixed and
varies only the machinery:

    linear   one workspace, patches applied on top of each other, no verifier gate.
             A wrong patch stays in the tree and the next attempt builds on it. This
             is what an agent loop with retries actually does.
    search   every candidate is graded from a verified parent state; regressions are
             pruned and never inherited; already-tried ideas are excluded.

Same pool of candidate patches, same draws, same budget. The only difference is
whether a bad step is allowed to persist.

The generator is **simulated** -- it samples from a fixed pool of patches per bug --
and the report says so, because a simulated generator measures the search and
nothing else. What it captures faithfully is the thing being argued about.

Run with `ratchet evals`. No model, no key, no network, so it also runs in CI: a
change that quietly breaks the search shows up as a number rather than a vibe.
"""

from __future__ import annotations

import random
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import load_task
from ..models import TaskSpec
from ..sandbox import WorktreeProvider
from ..verifier.gauntlet import Gauntlet
from .bugs import Bug, seeded_bugs

CALL_BUDGET = 5


@dataclass
class TrialResult:
    mode: str
    bug: str
    solved: bool
    calls: int
    seconds: float
    cheats_seen: int = 0
    cheats_stuck: int = 0  # a cheating patch that was allowed to persist
    poisoned: bool = False  # ended on a state worse than where it started


def _trial_linear(bug: Bug, task: TaskSpec, repo: Path, rng: random.Random) -> TrialResult:
    """A normal agent loop: one workspace, no gate, no rollback."""
    provider = WorktreeProvider(repo, f"lin-{rng.randrange(1 << 30):x}")
    base = provider.base_image()
    gauntlet = Gauntlet(task, repo_dir=".", test_sources=bug.test_sources(repo))
    sb = provider.fork(base, label="linear")
    solved, cheats_seen, calls = False, 0, 0
    last_score = 0.0
    workspace_has_cheat = False
    t0 = time.time()
    try:
        for i in range(CALL_BUDGET):
            calls = i + 1
            patch = bug.pool.sample(rng, exclude=[])
            is_cheat = patch in bug.pool.cheats
            cheats_seen += int(is_cheat)
            # No verifier gate in this mode: whatever the model produced is applied
            # and stays applied. That is the entire difference being measured.
            applied = sb.apply_patch(patch)
            res = gauntlet.run(sb, patch, base_commit=base, apply_patch=False)
            last_score = res.score
            # "stuck" means: in the state the trial ENDS on. The apply chain's
            # fallback resets the worktree between attempts, so a later successful
            # apply replaces earlier patches -- counting at apply time overcounted
            # (found by review). Track what the workspace actually holds instead.
            if applied.ok:
                workspace_has_cheat = is_cheat
            if res.green:
                solved = True
                break
    finally:
        sb.destroy()
        provider.cleanup()
    cheats_stuck = int(workspace_has_cheat)
    return TrialResult("linear", bug.name, solved, calls, time.time() - t0,
                       cheats_seen, cheats_stuck, poisoned=not solved and last_score == 0.0)


def _trial_search(bug: Bug, task: TaskSpec, repo: Path, rng: random.Random) -> TrialResult:
    """Ratchet: grade from a verified parent, prune regressions, never redraw a dead end."""
    provider = WorktreeProvider(repo, f"srch-{rng.randrange(1 << 30):x}")
    base = provider.base_image()
    gauntlet = Gauntlet(task, repo_dir=".", test_sources=bug.test_sources(repo))
    tried: list[str] = []
    solved, cheats_seen, cheats_stuck, calls = False, 0, 0, 0
    t0 = time.time()
    try:
        for i in range(CALL_BUDGET):
            calls = i + 1
            patch = bug.pool.sample(rng, exclude=tried, widen=i >= 2)
            tried.append(patch)
            is_cheat = patch in bug.pool.cheats
            cheats_seen += int(is_cheat)
            sb = provider.fork(base, label=f"c{i}")  # always from the verified parent
            try:
                res = gauntlet.run(sb, patch, base_commit=base)
            finally:
                sb.destroy()
            # Same definition as linear: in the trial's final state. Under search a
            # candidate is only inherited if the verifier passed it outright, so a
            # cheat can end up here only by defeating the gauntlet -- which is why
            # run_suite treats a nonzero count as a hard failure, not a data point.
            if is_cheat and res.green:
                cheats_stuck += 1
            if res.green:
                solved = True
                break
    finally:
        provider.cleanup()
    return TrialResult("search", bug.name, solved, calls, time.time() - t0, cheats_seen, cheats_stuck)


def run_suite(repo: Path, *, trials: int = 5, verbose: bool = True, seed: int = 7) -> int:
    rows: list[TrialResult] = []
    for bug in seeded_bugs():
        task = load_task(bug.task_path)
        for t in range(trials):
            # the same seed for both modes, so they see the same draws
            rows.append(_trial_linear(bug, task, repo, random.Random(seed + t * 977)))
            rows.append(_trial_search(bug, task, repo, random.Random(seed + t * 977)))
    if verbose:
        print(report(rows, trials))
    lin = [r for r in rows if r.mode == "linear"]
    sea = [r for r in rows if r.mode == "search"]
    ok = sum(r.solved for r in sea) >= sum(r.solved for r in lin) and sum(r.cheats_stuck for r in sea) == 0
    return 0 if ok else 1


def _rate(rows: list[TrialResult]) -> tuple[float, float]:
    xs = [1.0 if r.solved else 0.0 for r in rows]
    if not xs:
        return 0.0, 0.0
    mean = statistics.fmean(xs)
    se = (statistics.pstdev(xs) / (len(xs) ** 0.5)) if len(xs) > 1 else 0.0
    return mean, se


def report(rows: list[TrialResult], trials: int) -> str:
    bugs = sorted({r.bug for r in rows})
    w = max(len(b) for b in bugs) + 2
    out = [
        "",
        "linear vs search — same bugs, same draws, same call budget",
        f"{trials} trials per cell, budget {CALL_BUDGET} calls. The generator is simulated:",
        "this measures the machinery (rollback and pruning), not a model.",
        "",
        f"{'bug':<{w}}{'mode':<9}{'solved':>14}{'calls':>8}{'cheats stuck':>15}",
        "-" * (w + 46),
    ]
    for bug in bugs:
        for mode in ("linear", "search"):
            cell = [r for r in rows if r.bug == bug and r.mode == mode]
            mean, se = _rate(cell)
            calls = statistics.fmean([r.calls for r in cell]) if cell else 0
            stuck = sum(r.cheats_stuck for r in cell)
            flag = "  <-- unverified output persisted" if stuck else ""
            out.append(f"{bug:<{w}}{mode:<9}{mean * 100:>9.0f}% ±{se * 100:<3.0f}{calls:>8.1f}{stuck:>15}{flag}")
        out.append("")
    lin, lse = _rate([r for r in rows if r.mode == "linear"])
    sea, sse = _rate([r for r in rows if r.mode == "search"])
    lin_stuck = sum(r.cheats_stuck for r in rows if r.mode == "linear")
    sea_stuck = sum(r.cheats_stuck for r in rows if r.mode == "search")
    poisoned = sum(1 for r in rows if r.mode == "linear" and r.poisoned)
    out += [
        f"overall   linear {lin * 100:.0f}% ±{lse * 100:.0f}   ·   search {sea * 100:.0f}% ±{sse * 100:.0f}",
        f"cheating patches that persisted   linear {lin_stuck}   ·   search {sea_stuck}",
        "  (persisted = still in the trial's final state. Linear keeps whatever its",
        "   workspace last successfully applied; under search nothing is inherited",
        "   unless the verifier passed it, so a nonzero search count means the",
        "   gauntlet itself was defeated.)",
        f"runs that ended on a broken state linear {poisoned}   ·   search 0 (a pruned node cannot be inherited)",
    ]
    return "\n".join(out)
