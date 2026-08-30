"""The stream console and the pipeline it renders.

The console it replaced painted fixed panes and hid the ones that would not fit,
so an 80-column terminal lost the search tree, the gauntlet rail and the
waiting-on panel without saying so. These tests exist to make that class of
failure impossible: a stream has no panes to lose, and every event has to produce
a line at every width.
"""

from __future__ import annotations

import io

from rich.console import Console

from ratchet.bus import Bus, Event
from ratchet.console import StreamConsole
from ratchet.pipeline import DEMO_FINDINGS, Pace, PipelineRun


def _render(events, width: int = 80) -> str:
    buf = io.StringIO()
    view = StreamConsole(Console(file=buf, width=width, highlight=False, soft_wrap=False))
    for kind, payload in events:
        view.handle(Event(kind, payload, 0.0))
    return buf.getvalue()


def test_every_event_family_produces_a_line():
    """A silent event is a pane that has gone dark: it happened and nobody saw."""
    families = [
        ("run.started", {"run_id": "r1", "task": "fix it", "provider": "trueforge"}),
        ("repo.mapped", {"lines": 24}),
        ("expand", {"node": "root", "fanout": 2, "dead_ends": 1}),
        ("verify.started", {"label": "root-0", "model": "gpt-5.2", "intent": "try a thing"}),
        ("stage.result", {"stage": "cheat", "passed": True, "detail": "0 critical"}),
        ("node.added", {"id": "ae2c", "score": 1.0, "green": True}),
        ("node.pruned", {"id": "9ba4", "reason": "a passing test broke"}),
        ("stall", {"node": "root", "fanout": 3}),
        ("approval.required", {"action": "open_pull_request", "summary": "the fix"}),
        ("approval.resolved", {"approved": True}),
        ("pr.opened", {"pr": "#118", "title": "the fix", "url": "http://x/118"}),
        ("review.started", {"pr": "#118", "files": 2}),
        ("review.finding", {"severity": "high", "title": "a real problem", "detail": "why"}),
        ("review.done", {"pr": "#118", "findings": 1}),
        ("fix.started", {"finding": "a real problem"}),
        ("fix.done", {"summary": "fixed it"}),
        ("pr.merged", {"pr": "#118"}),
        ("chat.turn", {"provider": "claude-code", "model": "sonnet", "prompt": "make a site"}),
        ("chat.step", {"text": "write index.html"}),
        ("chat.done", {"files": ["index.html"], "commit": "abc1234", "seconds": 3.2}),
        ("run.done", {"green": True, "reason": "merged"}),
    ]
    for kind, payload in families:
        out = _render([(kind, payload)])
        assert out.strip(), f"{kind} rendered nothing at all"


def test_the_whole_pipeline_reads_the_same_at_every_width(tmp_path):
    """The bug this file exists for: the console used to drop entire sections below
    104 columns. A stream has nothing to drop."""
    bus = Bus(tmp_path / "p.bus.jsonl")
    PipelineRun(tmp_path, bus, run_id="t", pace=Pace(beat=0)).run()
    events = [(e.kind, e.payload) for e in bus.read_all()]

    for width in (60, 80, 100, 120, 200):
        out = _render(events, width=width)
        for marker in ("Cartographer", "Verify", "pruned", "Gate", "Qodo",
                       "Pull request", "Merged", "PASS", "FAIL"):
            assert marker in out, f"{marker!r} missing at {width} columns"


def test_the_pipeline_tells_the_whole_story(tmp_path):
    """Harness routes it, the verifier rejects one attempt and accepts another, a
    human clears the gate, Qodo reviews, the findings become work, it merges."""
    bus = Bus(tmp_path / "p.bus.jsonl")
    result = PipelineRun(tmp_path, bus, run_id="t", pace=Pace(beat=0)).run()
    kinds = [e.kind for e in bus.read_all()]

    assert kinds[0] == "run.started" and kinds[-1] == "run.done"
    assert "node.pruned" in kinds, "a search that never rejects anything is not a search"
    assert "node.added" in kinds
    assert kinds.index("approval.required") < kinds.index("pr.opened"), "the PR must wait for the gate"
    assert kinds.index("review.started") < kinds.index("pr.merged"), "review must precede merge"
    assert kinds.count("review.started") == 2, "the fixes have to be re-reviewed"
    assert kinds.count("review.finding") == len(DEMO_FINDINGS)
    assert kinds.count("fix.started") == len(DEMO_FINDINGS), "every finding becomes work"
    assert result["green"] and result["findings"] == len(DEMO_FINDINGS)


def test_a_finding_is_answered_before_the_merge(tmp_path):
    """The point of the review stage: nothing merges over an open finding."""
    bus = Bus(tmp_path / "p.bus.jsonl")
    PipelineRun(tmp_path, bus, run_id="t", pace=Pace(beat=0)).run()
    events = bus.read_all()
    last_finding = max(i for i, e in enumerate(events) if e.kind == "review.finding")
    merged = next(i for i, e in enumerate(events) if e.kind == "pr.merged")
    fixes = [i for i, e in enumerate(events) if e.kind == "fix.done"]
    assert all(last_finding < i < merged for i in fixes)
    assert events[-2].kind == "pr.merged"


def test_totals_count_what_actually_happened(tmp_path):
    bus = Bus(tmp_path / "p.bus.jsonl")
    PipelineRun(tmp_path, bus, run_id="t", pace=Pace(beat=0)).run()
    view = StreamConsole(Console(file=io.StringIO(), width=100))
    for e in bus.read_all():
        view.handle(e)
    assert view.totals.nodes == 2 and view.totals.pruned == 1
    assert view.totals.reviews == 2 and view.totals.findings == len(DEMO_FINDINGS)
    assert view.totals.subagents >= 3


def test_the_demo_says_it_is_a_demo(tmp_path):
    """A rehearsal that pretends to be a performance is worth nothing."""
    bus = Bus(tmp_path / "p.bus.jsonl")
    PipelineRun(tmp_path, bus, run_id="t", pace=Pace(beat=0), demo=True).run()
    assert bus.read_all()[0].payload.get("demo") is True
