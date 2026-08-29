"""End-to-end: seed the demo repo, run two real patches through the real pawl.

This runs with `Backend.LOCAL` so it needs no Docker and no network, which means
it also runs in CI and on a hackathon laptop with a dead Wi-Fi connection.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ratchet.config import load_task
from ratchet.demo import seed
from ratchet.gauntlet.runner import Backend, Pawl
from ratchet.models import Decision

TASK = Path(__file__).resolve().parents[1] / "tasks" / "demo-001-slugify" / "task.yaml"


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("demo")
    d = seed(root / "demo-repo")
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)
    return d


def _pawl(tmp_path) -> Pawl:
    return Pawl(backend=Backend.LOCAL, log_dir=tmp_path / "logs")


def _base(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_honest_patch_is_accepted(repo, tmp_path):
    task = load_task(TASK)
    diff = (repo / "patches" / "honest.diff").read_text()
    v = _pawl(tmp_path).run_gauntlet(
        task=task,
        worktree=repo,
        base_commit=_base(repo),
        diff=diff,
        branch="test",
        type_cmd=None,
        lint_cmd=None,
    )
    assert v.decision is Decision.ACCEPTED, v.to_observation()
    assert v.f2p_hidden_rate == 1.0
    assert v.delta == 0.0
    subprocess.run(["git", "checkout", "--", "."], cwd=repo, check=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_cheating_patch_is_disqualified_before_it_runs(repo, tmp_path):
    task = load_task(TASK)
    diff = (repo / "patches" / "cheat.diff").read_text()
    v = _pawl(tmp_path).run_gauntlet(
        task=task,
        worktree=repo,
        base_commit=_base(repo),
        diff=diff,
        branch="test",
        test_sources={
            "tests/test_slugify_visible.py": (repo / "tests" / "test_slugify_visible.py").read_text(),
            "tests/test_regression.py": (repo / "tests" / "test_regression.py").read_text(),
        },
        type_cmd=None,
        lint_cmd=None,
    )
    rules = {f.rule for f in v.findings}
    assert v.decision is Decision.DISQUALIFIED, v.to_observation()
    assert "protected_path" in rules
    assert "skip_marker" in rules
    subprocess.run(["git", "checkout", "--", "."], cwd=repo, check=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_canary_catches_a_hack_no_static_rule_would_find(repo, tmp_path):
    """The canary's whole job. This patch has no skip markers, touches no test file
    and trips no patchlint rule -- it just returns a different answer the second
    time it is asked. The task it satisfies is impossible, so green means cheated."""
    task = load_task(Path(__file__).resolve().parents[1] / "tasks" / "canary-impossible" / "task.yaml")
    diff = (repo / "patches" / "canary_hack.diff").read_text()
    v = _pawl(tmp_path).run_gauntlet(
        task=task,
        worktree=repo,
        base_commit=_base(repo),
        diff=diff,
        branch="test",
        type_cmd=None,
        lint_cmd=None,
    )
    assert not v.findings, "no static rule should have fired here"
    assert v.decision is Decision.DISQUALIFIED, v.to_observation()
    subprocess.run(["git", "checkout", "--", "."], cwd=repo, check=True)
