"""`ratchet build`: goal to graph to sub-agents to review to pull request.

The order is the argument, so most of these assert ordering: the review has to
happen before the commit exists, every blocking finding has to be answered before
the gate, and nothing may reach a pull request without a human clearing it.
"""

from __future__ import annotations

import io

from rich.console import Console

from ratchet.build import BuildRun, Pace, Target
from ratchet.buildview import BuildView
from ratchet.bus import Bus
from ratchet.qodo_mcp import SCRIPTED_FINDINGS, Finding, QodoMCP, Review


def _run(tmp_path):
    bus = Bus(tmp_path / "b.bus.jsonl")
    result = BuildRun(Target.parse("https://github.com/acme/api/issues/42"), tmp_path, bus,
                      run_id="t", pace=Pace(beat=0)).run()
    return bus.read_all(), result


# ------------------------------------------------------------------- intake --


def test_a_prompt_a_repo_and_an_issue_are_all_valid_targets():
    assert Target.parse("add rate limiting").kind == "prompt"
    repo = Target.parse("https://github.com/psf/requests")
    assert repo.kind == "repo" and repo.repo == "psf/requests"
    issue = Target.parse("https://github.com/psf/requests/issues/1234")
    assert issue.kind == "issue" and issue.issue == "#1234" and "1234" in issue.goal


# -------------------------------------------------------------------- shape --


def test_the_goal_becomes_a_graph_with_tests_on_every_node(tmp_path):
    events, _ = _run(tmp_path)
    plan = next(e for e in events if e.kind == "graph.planned")
    nodes = plan.payload["nodes"]
    assert len(nodes) >= 3
    assert all(n["tests"] > 0 for n in nodes), "a node without tests can never be fulfilled"
    assert any(n["deps"] for n in nodes), "a graph with no edges is a list"


def test_independent_nodes_run_in_the_same_wave(tmp_path):
    events, _ = _run(tmp_path)
    waves = [e.payload for e in events if e.kind == "wave.started"]
    assert any(w["parallel"] > 1 for w in waves), "nothing ran in parallel"
    last = waves[-1]["nodes"]
    assert last == ["middleware"], "a dependent node must wait for its dependencies"


def test_a_node_that_fails_its_tests_is_pruned_not_kept(tmp_path):
    events, _ = _run(tmp_path)
    kinds = [e.kind for e in events]
    assert "node.pruned" in kinds, "a search that never rejects anything is not a search"
    pruned = next(e for e in events if e.kind == "node.pruned")
    assert "restart" in pruned.payload["reason"]
    assert kinds.count("node.fulfilled") == 3


# ------------------------------------------------------------------- review --


def test_the_review_happens_before_the_commit_exists(tmp_path):
    """The whole point of putting Qodo behind MCP: a blocking finding never
    becomes a commit somebody has to revert."""
    events, _ = _run(tmp_path)
    kinds = [e.kind for e in events]
    first_review = kinds.index("review.started")
    assert first_review < kinds.index("commit.created")
    assert first_review < kinds.index("pr.opened")
    assert events[first_review].payload["scope"] == "diff"


def test_every_blocking_finding_is_answered_and_re_reviewed(tmp_path):
    events, result = _run(tmp_path)
    kinds = [e.kind for e in events]
    blocking = [f for f in SCRIPTED_FINDINGS if f.blocking]
    assert kinds.count("fix.started") == len(blocking)
    assert kinds.count("review.started") == 2, "the fixes have to be reviewed too"
    last_review = max(i for i, k in enumerate(kinds) if k == "review.done")
    assert events[last_review].payload["findings"] == 0
    assert last_review < kinds.index("commit.created")
    assert result["green"]


def test_nothing_reaches_a_pull_request_without_the_gate(tmp_path):
    events, _ = _run(tmp_path)
    kinds = [e.kind for e in events]
    assert kinds.index("approval.required") < kinds.index("commit.created") < kinds.index("pr.opened")
    assert kinds.index("approval.resolved") < kinds.index("pr.opened")


# --------------------------------------------------------------- qodo as mcp --


def test_qodo_is_exposed_as_an_mcp_tool():
    q = QodoMCP(scripted=True)
    tools = q.tools()
    assert [t["name"] for t in tools] == ["review_diff"]
    assert "diff" in tools[0]["inputSchema"]["required"]
    out = q.call_tool("review_diff", {"diff": "diff --git a/x b/x"})
    assert out["findings"] and out["blocking"] == 2 and out["scripted"] is True


def test_severity_decides_what_blocks_not_ratchet():
    assert Finding("high", "t", "d").blocking
    assert Finding("critical", "t", "d").blocking
    assert not Finding("medium", "t", "d").blocking
    assert Review(findings=[Finding("medium", "t", "d")]).clean


def test_a_reviewer_whose_output_drifts_does_not_take_the_run_down():
    from ratchet.qodo_mcp import _parse

    assert _parse("not json at all") == []
    assert _parse('{"findings": [{"nonsense": 1}]}')[0].severity == "medium"


# --------------------------------------------------------------------- view --


def test_the_run_reads_the_same_at_every_width(tmp_path):
    events, _ = _run(tmp_path)
    for width in (60, 80, 100, 140):
        buf = io.StringIO()
        view = BuildView(Console(file=buf, width=width, highlight=False), animate=False)
        for e in events:
            view.handle(e)
        out = buf.getvalue()
        for marker in ("objective graph", "parallel", "PASS", "pruned",
                       "qodo review", "high", "the gate", "Merged"):
            assert marker in out, f"{marker!r} missing at {width} columns"
