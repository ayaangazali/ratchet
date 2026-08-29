"""End to end: real patches, the real gauntlet, no model and no network.

These are the tests that back the claims on the README. If one of them fails, a
sentence on the front page has stopped being true.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ratchet.config import load_task
from ratchet.demo import seed
from ratchet.models import Outcome
from ratchet.sandbox import WorktreeProvider
from ratchet.verifier.gauntlet import Gauntlet

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "tasks" / "demo-001-slugify" / "task.yaml"
CANARY = ROOT / "tasks" / "canary-impossible" / "task.yaml"


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    d = seed(tmp_path_factory.mktemp("e2e") / "demo-repo")
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)
    return d


def _sources(repo: Path, task) -> dict[str, str]:
    out = {}
    for pat in task.protected_paths:
        base = repo / pat.rstrip("/")
        if base.is_dir():
            out.update({str(p.relative_to(repo)): p.read_text() for p in base.rglob("*.py")})
    return out


def _grade(repo: Path, task, diff: str, label: str):
    provider = WorktreeProvider(repo, f"e2e-{label}")
    base = provider.base_image()
    sb = provider.fork(base, label=label)
    try:
        return Gauntlet(task, repo_dir=".", test_sources=_sources(repo, task)).run(sb, diff, base_commit=base)
    finally:
        sb.destroy()
        provider.cleanup()


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_the_honest_fix_goes_green(repo):
    task = load_task(TASK)
    res = _grade(repo, task, (repo / "patches" / "honest.diff").read_text(), "honest")
    assert res.outcome is Outcome.GREEN, res.to_observation()
    assert res.f2p_ratio == 1.0
    assert res.delta == 0.0
    assert res.score == pytest.approx(1.0)


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_the_cheating_patch_is_stopped_before_it_executes(repo):
    task = load_task(TASK)
    res = _grade(repo, task, (repo / "patches" / "cheat.diff").read_text(), "cheat")
    rules = {f.rule for f in res.findings}
    assert res.outcome is Outcome.CHEATED, res.to_observation()
    assert "protected_path" in rules and "skip_marker" in rules
    # nothing ran: the only stage that executed is the static one
    assert set(res.stages) == {"cheat"}


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_the_canary_catches_what_no_static_rule_can(repo):
    """This patch adds no skip markers, edits no test file, and trips no critical
    rule. It just returns a different answer the second time it is asked. The canary
    catches it because the task it satisfies is impossible."""
    task = load_task(CANARY)
    res = _grade(repo, task, (repo / "patches" / "canary_hack.diff").read_text(), "canary")
    assert res.outcome is Outcome.CHEATED, res.to_observation()
    assert "canary_passed" in {f.rule for f in res.findings}
    assert res.stages["cheat"].passed  # the static stage let it through


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_red_team_battery_holds(repo):
    """The verifier's own eval, in CI. Every known attack blocked, both controls green."""
    from ratchet import redteam

    results = redteam.run(repo, load_task(TASK), load_task(CANARY))
    wrong = [r.attack.name for r in results if not r.correct]
    assert not wrong, redteam.report(results)
    assert sum(1 for r in results if r.attack.expect_caught) >= 10


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_partial_credit_is_a_scalar_not_a_boolean(repo):
    """A patch that fixes some of the target tests must score above one that fixes
    none. Without this the search has nothing to hill-climb on."""
    from ratchet.evals.bugs import seeded_bugs

    task = load_task(TASK)
    pool = seeded_bugs()[0].pool
    partial = _grade(repo, task, pool.partials[0], "partial")
    correct = _grade(repo, task, pool.correct, "correct")
    nothing = _grade(repo, task, "", "empty")
    assert nothing.score < partial.score < correct.score
