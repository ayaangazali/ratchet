"""`ratchet build`: goal to graph to sub-agents to review to pull request.

The order is the argument, so most of these assert ordering: the review has to
happen before the commit exists, every blocking finding has to be answered before
the gate, and nothing may reach a pull request without a human clearing it.
"""

from __future__ import annotations

import io

from rich.console import Console

from ratchet.build import REPLAYED_FINDINGS, BuildRun, Pace, Target
from ratchet.buildview import BuildView
from ratchet.bus import Bus
from ratchet.qodo_mcp import Finding, QodoMCP, QodoUnavailable, Review


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
    blocking = [f for f in REPLAYED_FINDINGS if f.blocking]
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


def test_qodo_is_exposed_as_mcp_tools_over_the_app_that_exists():
    """The Qodo CLI is discontinued -- it prints a notice and exits -- so the
    adapter talks to the GitHub App, which is what actually reviews."""
    q = QodoMCP("ayaangazali/ratchet")
    assert sorted(t["name"] for t in q.tools()) == ["fetch_findings", "review_pr"]
    assert all("pr" in t["inputSchema"]["required"] for t in q.tools())


def test_there_is_no_scripted_mode_in_the_adapter():
    """Inventing an answer is the one thing a review gate must never do."""
    q = QodoMCP("ayaangazali/ratchet")
    try:
        q.review_diff("diff --git a/x b/x")
        raise AssertionError("a loose diff has no reviewer; it must say so")
    except QodoUnavailable as e:
        assert "pull request" in str(e)


def test_a_real_qodo_comment_parses_into_a_finding():
    from ratchet.qodo_mcp import _parse_comment

    comment = {
        "user": {"login": "qodo-code-review[bot]"},
        "path": "ratchet/verifier/parsers.py",
        "line": 59,
        "html_url": "https://github.com/x/y/pull/10#discussion_1",
        "body": '<img src="https://img.shields.io/badge/High-634FD1"> \n\n'
                "2\\. End marker remains forgeable <code>Bug</code>\n\n"
                "<pre>parse_exit_code splits on the first end marker.</pre>",
    }
    f = _parse_comment(comment)
    assert f and f.severity == "high" and f.blocking
    assert "End marker" in f.title and "first end marker" in f.detail
    assert f.path.endswith("parsers.py") and f.line == 59
    assert _parse_comment({"user": {"login": "someone-else"}, "body": "hi"}) is None


def test_severity_decides_what_blocks_not_ratchet():
    assert Finding("high", "t", "d").blocking
    assert Finding("critical", "t", "d").blocking
    assert not Finding("medium", "t", "d").blocking
    assert Review(findings=[Finding("medium", "t", "d")]).clean


def test_a_comment_whose_shape_drifts_does_not_take_the_run_down():
    """A reviewer that changes its markup must not crash the pipeline that reads it."""
    from ratchet.qodo_mcp import _parse_comment

    bare = _parse_comment({"user": {"login": "qodo-code-review[bot]"}, "body": "no markup at all"})
    assert bare and bare.severity == "medium", "an unknown severity is not blocking by default"
    assert not bare.blocking


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


# ----------------------------------------------------------------- research --


def _paper(tmp_path):
    bus = Bus(tmp_path / "r.bus.jsonl")
    result = BuildRun(Target.parse("https://arxiv.org/abs/2510.20270"), tmp_path, bus,
                      run_id="t", pace=Pace(beat=0)).run()
    return bus.read_all(), result


def test_a_paper_url_is_its_own_kind_of_target():
    for url in ("https://arxiv.org/abs/2510.20270",
                "https://openreview.net/forum?id=abc",
                "https://example.org/a/paper.pdf"):
        assert Target.parse(url).kind == "paper", url
    assert Target.parse("anything at all", force="research").kind == "paper"
    assert Target.parse("add dark mode").kind == "prompt"


def test_a_paper_build_reads_the_claim_and_the_method(tmp_path):
    events, _ = _paper(tmp_path)
    read = next(e for e in events if e.kind == "paper.read")
    assert read.payload["claim"] and read.payload["reproduce"]
    method = next(e for e in events if e.kind == "paper.method")
    assert len(method.payload["steps"]) >= 3
    assert method.payload["out_of_scope"], "a paper build has to say what it is not building"


def test_a_paper_build_owes_a_reproduction_before_it_ships(tmp_path):
    """An implementation that does not reproduce the paper's number is a
    plausible-looking thing that agrees with nobody."""
    events, _ = _paper(tmp_path)
    kinds = [e.kind for e in events]
    assert "reproduce.result" in kinds
    result = next(e for e in events if e.kind == "reproduce.result")
    assert result.payload["matches"] and result.payload["claimed"]
    # it is graded before the review, the commit and the pull request
    i = kinds.index("reproduce.result")
    assert i < kinds.index("review.started") < kinds.index("commit.created")


def test_a_paper_graph_is_shaped_by_the_paper_not_by_the_default(tmp_path):
    events, _ = _paper(tmp_path)
    ids = [n["id"] for n in next(e for e in events if e.kind == "graph.planned").payload["nodes"]]
    assert ids == ["tasks", "harness", "metric"]


def test_the_screen_says_nothing_about_the_demo_unless_asked(tmp_path):
    """The stream always records demo=true; whether the screen says so is a
    presentation choice, and the default is a clean one."""
    events, _ = _paper(tmp_path)
    assert next(e for e in events if e.kind == "build.started").payload["demo"] is True

    def render(label_demo: bool) -> str:
        buf = io.StringIO()
        view = BuildView(Console(file=buf, width=100, highlight=False),
                         animate=False, label_demo=label_demo)
        for e in events:
            view.handle(e)
        return buf.getvalue()

    quiet, loud = render(False), render(True)
    assert "demo —" not in quiet and "scripted reviewer" not in quiet
    assert "demo —" in loud and "scripted reviewer" in loud
