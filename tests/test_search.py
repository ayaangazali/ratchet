"""The search machinery: tree, scheduler, budgets, and the full loop offline.

The loop test is the important one. It runs a complete search — root, expansion,
pruning, stall, fan-out, green — against a scripted generator and a real verifier,
with no model, no key and no network. If this passes, the machinery works; what a
live run adds is a model that writes better patches.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ratchet.config import load_task
from ratchet.demo import SLUGIFY_BUGGY, SLUGIFY_FIXED, seed
from ratchet.loop import SearchRun
from ratchet.models import GauntletResult, Outcome, StageResult
from ratchet.node import Node, Tree
from ratchet.sandbox import WorktreeProvider
from ratchet.scheduler import Budget, Scheduler, diff_distance, novelty, select_score
from ratchet.subagents import ScriptedBackend, Subagents

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "tasks" / "demo-001-slugify" / "task.yaml"


# --------------------------------------------------------------------- tree --


def _res(score: float, green: bool = False) -> GauntletResult:
    return GauntletResult(
        outcome=Outcome.GREEN if green else Outcome.PROGRESS,
        score=score,
        green=green,
        stages={"f2p": StageResult("f2p", green, score, f"{score:.2f}")},
    )


def _tree(tmp_path) -> Tree:
    t = Tree(tmp_path / "tree.json")
    t.add(Node.root(commit="abc123", image="img", result=_res(0.2)))
    return t


def test_tree_persists_and_reloads(tmp_path):
    t = _tree(tmp_path)
    child = Node.child_of(t.root, commit="def", image="img2", patch="+a", intent="try a", result=_res(0.5))
    t.add(child)
    again = Tree.load(t.path)
    assert len(again) == 2
    assert again.get(child.id).intent == "try a"
    assert again.best().id == child.id


def test_pruned_nodes_leave_the_frontier_but_stay_in_the_tree(tmp_path):
    t = _tree(tmp_path)
    bad = t.add(Node.child_of(t.root, commit="d1", image="i", patch="+x", intent="bad idea", result=_res(0.1)))
    t.prune(bad, "broke a passing test")
    assert bad.id not in {n.id for n in t.frontier()}
    assert len(t) == 2
    assert "bad idea" in bad.one_line()


def test_path_and_render(tmp_path):
    t = _tree(tmp_path)
    a = t.add(Node.child_of(t.root, commit="d1", image="i", patch="+x", intent="a", result=_res(0.4)))
    b = t.add(Node.child_of(a, commit="d2", image="i", patch="+y", intent="b", result=_res(0.6)))
    assert [n.id for n in t.path_to(b)] == ["root", a.id, b.id]
    lines = t.render(live_id=b.id)
    assert any("live" in line for line, _ in lines)


def test_concurrent_adds_do_not_corrupt_the_tree(tmp_path):
    """The fan-out path adds nodes from a thread pool. Unlocked, two saves race on
    one .tmp file and the nodes dict mutates mid-iteration."""
    import threading

    t = _tree(tmp_path)
    n = 32
    barrier = threading.Barrier(n)

    def add(i: int) -> None:
        barrier.wait()
        t.add(Node.child_of(t.root, commit=f"c{i}", image="i", patch=f"+line{i}", intent=f"i{i}", result=_res(0.5)))

    threads = [threading.Thread(target=add, args=(i,)) for i in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert len(t) == n + 1
    reloaded = Tree.load(t.path)  # the file on disk is whole, parseable json
    assert len(reloaded) == n + 1


# ---------------------------------------------------------------- scheduler --


def test_diff_distance_is_zero_for_identical_and_one_for_disjoint():
    assert diff_distance("+a\n+b", "+a\n+b") == 0.0
    assert diff_distance("+a", "+z") == 1.0


def test_novelty_rewards_a_branch_that_tried_something_else(tmp_path):
    t = _tree(tmp_path)
    a = t.add(Node.child_of(t.root, commit="1", image="i", patch="+same\n+lines", intent="a", result=_res(0.5)))
    b = t.add(Node.child_of(t.root, commit="2", image="i", patch="+same\n+lines", intent="b", result=_res(0.5)))
    c = t.add(Node.child_of(t.root, commit="3", image="i", patch="+totally\n+different", intent="c", result=_res(0.5)))
    assert novelty(c, t.siblings(c)) > novelty(a, t.siblings(a))
    # equal scores, so novelty is what breaks the tie
    assert select_score(c, t) > select_score(b, t)


def test_shallow_nodes_win_ties_over_deep_ones(tmp_path):
    t = _tree(tmp_path)
    shallow = t.add(Node.child_of(t.root, commit="1", image="i", patch="+a", intent="s", result=_res(0.6)))
    deep = shallow
    for i in range(4):
        deep = t.add(Node.child_of(deep, commit=f"d{i}", image="i", patch=f"+d{i}", intent="d", result=_res(0.6)))
    assert select_score(shallow, t) > select_score(deep, t)


def test_budget_stops_the_search():
    b = Budget(max_nodes=2, max_seconds=999, max_usd=999)
    b.spend(nodes=2)
    assert not b.ok()
    assert "node budget" in b.exhausted_reason()

    b = Budget(max_nodes=99, max_seconds=999, max_usd=0.5)
    b.spend(usd=0.5)
    assert not b.ok()
    assert "spend cap" in b.exhausted_reason()


def test_stall_detection_and_shallow_fork_target(tmp_path):
    t = _tree(tmp_path)
    s = Scheduler(Budget(), patience=3)
    shallow = t.add(Node.child_of(t.root, commit="1", image="i", patch="+a", intent="s", result=_res(0.55)))
    deep = shallow
    for i in range(3):
        deep = t.add(Node.child_of(deep, commit=f"x{i}", image="i", patch=f"+x{i}", intent="d", result=_res(0.5)))
    s.observe(t)          # the first call establishes the baseline
    for _ in range(3):    # three expansions after that with no improvement
        s.observe(t)
    assert s.stalled
    target = s.stall_target(t)
    assert target.depth <= shallow.depth + 1  # fork shallow, not from the tunnel


# --------------------------------------------------------------------- loop --


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    d = seed(tmp_path_factory.mktemp("search") / "demo-repo")
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)
    return d


def _patch(old: str, new: str) -> str:
    import difflib

    body = "".join(
        difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                             fromfile="a/src/textkit/slugify.py", tofile="b/src/textkit/slugify.py")
    )
    return "diff --git a/src/textkit/slugify.py b/src/textkit/slugify.py\n" + body


def _reply(patch: str, intent: str) -> str:
    return f"intent: {intent}\n\n```diff\n{patch}\n```"


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_search_finds_green_and_prunes_the_bad_branch(repo, tmp_path):
    """A full run: one wrong patch that regresses, then the real fix."""
    task = load_task(TASK)
    task.repo_path = str(repo)

    # regresses a pass-to-pass test: "already-a-slug" loses its hyphens
    broken = SLUGIFY_BUGGY.replace('_SEP.sub("-", ascii_only)', '_SEP.sub("", ascii_only)')
    responses = [
        "src/textkit/slugify.py — the slug is built here; truncation is the last line.",  # cartographer
        _reply(_patch(SLUGIFY_BUGGY, broken), "truncate harder"),
        _reply(_patch(SLUGIFY_BUGGY, SLUGIFY_FIXED), "fold accents and truncate on a word boundary"),
    ]
    run = SearchRun(
        task=task,
        repo=repo,
        provider=WorktreeProvider(repo, "t-search"),
        subagents=Subagents(ScriptedBackend(responses)),
        run_id="t-search",
        scheduler=Scheduler(Budget(max_nodes=9, max_seconds=300, max_usd=1)),
        parallel=False,
    )
    result = run.run()

    assert result.green, result.stopped_because
    assert any(n.pruned for n in result.tree), "the regressing patch should have been pruned"
    assert result.winner.score == pytest.approx(1.0)
    # the pruned node is still reachable: nothing the agent produced is destroyed
    assert any(n.pruned and n.intent == "truncate harder" for n in result.tree)


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_dead_ends_reach_the_next_prompt(repo):
    """Negative-sibling injection: a pruned attempt must appear in the next context."""
    task = load_task(TASK)
    task.repo_path = str(repo)
    broken = SLUGIFY_BUGGY.replace('_SEP.sub("-", ascii_only)', '_SEP.sub("", ascii_only)')
    backend = ScriptedBackend([
        "map",
        _reply(_patch(SLUGIFY_BUGGY, broken), "truncate harder"),
        _reply(_patch(SLUGIFY_BUGGY, SLUGIFY_FIXED), "the real fix"),
    ])
    run = SearchRun(
        task=task, repo=repo, provider=WorktreeProvider(repo, "t-dead"),
        subagents=Subagents(backend), run_id="t-dead",
        scheduler=Scheduler(Budget(max_nodes=9, max_seconds=300, max_usd=1)), parallel=False,
    )
    run.run()
    generator_prompts = [c for c in backend.calls if c[0] == "generator"]
    assert len(generator_prompts) >= 2


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_held_out_names_never_reach_a_prompt(repo):
    """CLAUDE.md invariant 5, end to end. The first patch passes the visible tests by
    hardcoding them, so the held-out tests are the only failures -- exactly the state
    in which their names used to flow through last_failure into the next context."""
    from ratchet import context as ctx_mod

    task = load_task(TASK)
    task.repo_path = str(repo)
    hardcoded = SLUGIFY_BUGGY.replace(
        "    lowered = text.lower()",
        '    if text == "Caf\u00e9 Life":\n        return "cafe-life"\n'
        '    if text == "the quick brown fox jumps" and max_length == 17:\n'
        '        return "the-quick-brown"\n'
        "    lowered = text.lower()",
    )
    backend = ScriptedBackend([
        "map",
        _reply(_patch(SLUGIFY_BUGGY, hardcoded), "hardcode the visible cases"),
        _reply(_patch(SLUGIFY_BUGGY, SLUGIFY_FIXED), "the real fix"),
    ])
    run = SearchRun(
        task=task, repo=repo, provider=WorktreeProvider(repo, "t-leak"),
        subagents=Subagents(backend), run_id="t-leak",
        scheduler=Scheduler(Budget(max_nodes=9, max_seconds=300, max_usd=1)), parallel=False,
    )
    result = run.run()

    forbidden = set()
    for t in task.f2p_hidden:
        path, _, name = t.partition("::")
        forbidden.update((t, path, name))

    assert result.green  # the run itself still works
    for node in result.tree:
        for tok in forbidden:
            assert tok not in node.last_failure, f"{tok!r} leaked via node {node.id}"
        ctx = ctx_mod.assemble(
            task=task.statement, node=node, tree=result.tree,
            repo_map=run.repo_map, diff_so_far="",
        )
        rendered = ctx.render()
        for tok in forbidden:
            assert tok not in rendered, f"{tok!r} leaked via the rendered context"


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_a_dry_generator_ends_the_run_instead_of_spinning(repo):
    """Empty candidates spend no budget, so a generator with nothing to say used to
    spin the loop until the wall clock -- 11 million bus events in one observed run.
    Five fruitless expansions in a row must end the run with a reason."""
    task = load_task(TASK)
    task.repo_path = str(repo)
    run = SearchRun(
        task=task, repo=repo, provider=WorktreeProvider(repo, "t-dry"),
        subagents=Subagents(ScriptedBackend(["map"])),  # nothing for the generator
        run_id="t-dry",
        scheduler=Scheduler(Budget(max_nodes=40, max_seconds=300, max_usd=3)), parallel=False,
    )
    result = run.run()
    assert not result.green
    assert "no usable candidate" in result.stopped_because


def test_sandbox_env_backs_python_with_the_running_interpreter(repo):
    """A brew/pipx install has no dev venv on PATH and macOS has no bare `python`;
    the sandbox must resolve `python` to the interpreter running ratchet."""
    provider = WorktreeProvider(repo, "t-env")
    sb = provider.fork(provider.base_image(), label="env")
    try:
        res = sb.exec("python -c 'import sys; print(sys.version_info[0])'", timeout=60)
    finally:
        sb.destroy()
        provider.cleanup()
    assert res.ok, res.out
    assert res.out.strip().endswith("3")


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_receipts_cover_every_graded_node(repo):
    task = load_task(TASK)
    task.repo_path = str(repo)
    run = SearchRun(
        task=task, repo=repo, provider=WorktreeProvider(repo, "t-rcpt"),
        subagents=Subagents(ScriptedBackend(["map", _reply(_patch(SLUGIFY_BUGGY, SLUGIFY_FIXED), "fix")])),
        run_id="t-rcpt", scheduler=Scheduler(Budget(max_nodes=4, max_seconds=300, max_usd=1)), parallel=False,
    )
    run.run()
    ok, problems = run.receipts.verify()
    assert ok, problems
    assert len(run.receipts.all()) >= 2  # the root and at least one candidate


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_tree_file_is_valid_json_after_a_run(repo):
    tree_files = sorted((repo / ".ratchet").glob("*.tree.json"))
    assert tree_files
    data = json.loads(tree_files[-1].read_text())
    assert "nodes" in data and "order" in data


def test_a_generator_that_returns_nothing_cannot_spin_forever(repo):
    """An empty reply consumes a call, so it has to consume budget.

    An empty candidate creates no node. If that costs nothing, the scheduler picks
    the same state again and the loop only stops when the wall clock does -- which
    in practice means a fifteen-minute hang writing `candidate.empty` to the bus.
    One run left a 929MB bus file behind before this was charged.
    """
    from ratchet.bus import Bus
    from ratchet.config import load_task
    from ratchet.loop import SearchRun
    from ratchet.sandbox import WorktreeProvider
    from ratchet.scheduler import Budget, Scheduler
    from ratchet.subagents import ScriptedBackend, Subagents

    max_nodes = 4
    # "map" answers the cartographer; every generator call after it returns "".
    backend = ScriptedBackend(["map"])
    run = SearchRun(
        task=load_task(TASK),
        repo=repo,
        provider=WorktreeProvider(repo, "empty-gen"),
        subagents=Subagents(backend),
        run_id="empty-gen",
        scheduler=Scheduler(Budget(max_nodes=max_nodes, max_seconds=120)),
        bus=Bus(repo / ".ratchet" / "empty-gen.bus.jsonl"),
    )
    result = run.run()

    assert not result.green
    assert "node budget" in result.stopped_because, result.stopped_because
    # The root costs one node; the rest are the empty expansions. Without the fix
    # this number is bounded only by how many calls fit in the wall clock.
    generator_calls = sum(1 for role, _m in backend.calls if role == "generator")
    assert generator_calls <= max_nodes, generator_calls


def test_a_run_whose_candidates_all_regress_still_spends_its_budget(repo):
    """`max_nodes` caps work attempted, not work that succeeded.

    Charging only the accepted path means a run whose every candidate is pruned --
    a model emitting diffs that do not apply, which is an ordinary Tuesday -- spends
    no node budget and stops only when the wall clock does.
    """
    from ratchet.bus import Bus
    from ratchet.config import load_task
    from ratchet.loop import SearchRun
    from ratchet.sandbox import WorktreeProvider
    from ratchet.scheduler import Budget, Scheduler
    from ratchet.subagents import ScriptedBackend, Subagents

    max_nodes = 3
    junk = "```diff\nintent: nonsense\n--- a/nope.py\n+++ b/nope.py\n@@ -1 +1 @@\n-a\n+b\n```"
    backend = ScriptedBackend(["map"] + [junk] * 40)
    run = SearchRun(
        task=load_task(TASK),
        repo=repo,
        provider=WorktreeProvider(repo, "all-broken"),
        subagents=Subagents(backend),
        run_id="all-broken",
        scheduler=Scheduler(Budget(max_nodes=max_nodes, max_seconds=180)),
        bus=Bus(repo / ".ratchet" / "all-broken.bus.jsonl"),
    )
    result = run.run()

    assert not result.green
    assert "node budget" in result.stopped_because, result.stopped_because
    generator_calls = sum(1 for role, _m in backend.calls if role == "generator")
    assert generator_calls <= max_nodes, generator_calls
