"""The objective graph: a goal decomposed into nodes that only tests can fulfil.

The core claim under test: an ObjectiveNode's status flips to fulfilled on a green
GauntletResult and on nothing else -- not on the agent's say-so, not on a claim of
completion, not on a plausible-looking patch that fails its contract.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from ratchet.demo import SLUGIFY_BUGGY, SLUGIFY_FIXED, seed
from ratchet.objective import GraphRun, decompose, load_graph
from ratchet.sandbox import WorktreeProvider
from ratchet.scheduler import Budget
from ratchet.subagents import ScriptedBackend, Subagents

ROOT = Path(__file__).resolve().parents[1]
GRAPH_YAML = ROOT / "objectives" / "demo-graph.yaml"


@pytest.fixture()
def repo(tmp_path) -> Path:
    d = seed(tmp_path / "demo-repo")
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)
    return d


def _unified(old: str, new: str) -> str:
    import difflib

    path = "src/textkit/slugify.py"
    body = "".join(
        difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                             fromfile=f"a/{path}", tofile=f"b/{path}")
    )
    return f"diff --git a/{path} b/{path}\n" + body


ACCENTS_ONLY = SLUGIFY_BUGGY.replace(
    '    lowered = text.lower()\n    ascii_only = lowered.encode("ascii", "ignore").decode()',
    '    folded = unicodedata.normalize("NFKD", text.lower())\n'
    '    ascii_only = "".join(c for c in folded if not unicodedata.combining(c))\n'
    '    ascii_only = ascii_only.encode("ascii", "ignore").decode()',
)
NAIVE_TRUNCATE = ACCENTS_ONLY.replace("    return slug[:max_length]", '    return slug[:max_length].strip("-")')


def _reply(patch: str, intent: str) -> str:
    return f"intent: {intent}\n\n```diff\n{patch}\n```"


def _run(repo: Path, responses: list[str], *, graph_path: Path = GRAPH_YAML, budget: Budget | None = None):
    graph = load_graph(graph_path, repo)
    graph.repo_path = str(repo)
    run = GraphRun(
        graph=graph,
        repo=repo,
        provider=WorktreeProvider(repo, "t-graph"),
        subagents=Subagents(ScriptedBackend(responses)),
        run_id="t-graph",
        escalation_budget=budget or Budget(max_nodes=6, max_seconds=120, max_usd=1),
        parallel=False,
    )
    return run, run.run()


# ------------------------------------------------------------------- loading --


def test_load_validates_and_orders():
    g = load_graph(GRAPH_YAML, ROOT / "demo-repo") if (ROOT / "demo-repo").exists() else None
    if g is None:
        pytest.skip("demo repo not seeded at repo root")
    assert g.order.index("accents") < g.order.index("truncation")


def test_cycle_and_unknown_dep_are_config_errors(tmp_path, repo):
    cyc = textwrap.dedent("""
        graph_id: g
        nodes:
          - {id: a, goal: g, deps: [b], f2p_visible: [tests/test_regression.py::test_basic]}
          - {id: b, goal: g, deps: [a], f2p_visible: [tests/test_regression.py::test_basic]}
    """)
    p = tmp_path / "cyc.yaml"
    p.write_text(cyc)
    with pytest.raises(ValueError, match="cycle"):
        load_graph(p, repo)
    p.write_text(cyc.replace("deps: [b]", "deps: [ghost]"))
    with pytest.raises(ValueError, match="unknown node"):
        load_graph(p, repo)


def test_a_node_without_tests_is_refused(tmp_path, repo):
    """A step whose completion cannot be checked is not a step."""
    p = tmp_path / "g.yaml"
    p.write_text("graph_id: g\nnodes:\n  - {id: a, goal: vibes}\n")
    with pytest.raises(ValueError, match="no fail-to-pass tests"):
        load_graph(p, repo)


def test_a_node_naming_a_missing_test_is_refused(tmp_path, repo):
    p = tmp_path / "g.yaml"
    p.write_text("graph_id: g\nnodes:\n  - {id: a, goal: g, f2p_visible: [tests/test_nope.py::test_x]}\n")
    with pytest.raises(ValueError, match="does not exist"):
        load_graph(p, repo)


# ----------------------------------------------------------------- execution --


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_graph_fulfils_nodes_in_order_and_composes_state(repo):
    """accents fulfils first try; truncation's first candidate is objectively
    rejected by its own visible test and the retry lands. The final state carries
    both patches -- node B built on node A's commit."""
    run, summary = _run(repo, [
        "map",
        _reply(_unified(SLUGIFY_BUGGY, ACCENTS_ONLY), "fold accents"),
        _reply(_unified(ACCENTS_ONLY, NAIVE_TRUNCATE), "strip after cut"),
        _reply(_unified(ACCENTS_ONLY, SLUGIFY_FIXED), "word-boundary truncate"),
    ])
    assert summary.all_fulfilled, summary
    assert summary.fulfilled == ["accents", "truncation"]
    assert run.graph.nodes["truncation"].attempts == 2  # the first was rejected
    ok, problems = run.receipts.verify()
    assert ok, problems
    squashed = run.squashed()
    assert "unicodedata.normalize" in squashed and "rindex" in squashed


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_exhausted_node_escalates_to_the_search_engine(repo):
    """More than max_attempts failures hands the node, whole, to the tree search --
    per-branch sandboxes, same verifier -- and only a green search fulfils it."""
    bad = _reply(_unified(ACCENTS_ONLY, NAIVE_TRUNCATE), "strip after cut")
    run, summary = _run(repo, [
        "map",
        _reply(_unified(SLUGIFY_BUGGY, ACCENTS_ONLY), "fold accents"),
        bad, bad, bad,                                            # linear attempts exhausted
        bad,                                                       # first search candidate
        _reply(_unified(ACCENTS_ONLY, SLUGIFY_FIXED), "the fix"),  # search reaches green
    ])
    assert summary.all_fulfilled, summary
    node = run.graph.nodes["truncation"]
    assert node.escalated and node.status == "fulfilled" and node.sub_run_id


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_a_claim_of_completion_fulfils_nothing(repo, tmp_path):
    """The point of the whole module. A confident sentence with no patch consumes
    an attempt and changes no status; with nothing but claims, the node fails --
    it is never fulfilled, because fulfilment only flows from a green verdict."""
    p = tmp_path / "one.yaml"
    p.write_text(textwrap.dedent("""
        graph_id: one
        statement: fix accent folding
        test_cmd: python -m pytest -rA tests/test_slugify_visible.py tests/test_regression.py
        p2p: [tests/test_regression.py::test_basic]
        protected_paths: [tests/]
        nodes:
          - id: accents
            goal: fold accents
            max_attempts: 1
            f2p_visible: [tests/test_slugify_visible.py::test_folds_a_simple_accent]
    """))
    run, summary = _run(
        repo,
        ["map", "The task is complete. All tests now pass and the work is finished."],
        graph_path=p,
        budget=Budget(max_nodes=2, max_seconds=15, max_usd=1),
    )
    assert not summary.all_fulfilled
    assert run.graph.nodes["accents"].status == "failed"


# --------------------------------------------------------------- decomposition --


def test_decompose_goes_through_the_same_validator(repo):
    plan = textwrap.dedent("""
        Here is the plan:
        ```yaml
        graph_id: auto
        statement: fix slugify
        nodes:
          - id: accents
            goal: fold accents
            f2p_visible: [tests/test_slugify_visible.py::test_folds_a_simple_accent]
        ```
    """)
    agents = Subagents(ScriptedBackend([plan]))
    g = decompose("fix slugify", repo, agents)
    assert g.order == ["accents"]


def test_decompose_refuses_invented_tests(repo):
    plan = "```yaml\ngraph_id: auto\nnodes:\n  - {id: a, goal: g, f2p_visible: [tests/test_fake.py::test_x]}\n```"
    agents = Subagents(ScriptedBackend([plan]))
    with pytest.raises(ValueError, match="does not exist"):
        decompose("fix slugify", repo, agents)
