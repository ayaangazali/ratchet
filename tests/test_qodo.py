"""The Qodo wrap: parsing a real bot comment, the oracle's no-op guarantees, and
the wait loop's refusal to accept the bot's ACK edit. All offline — the fixture
is a captured review comment; every gh call is a mocked subprocess."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from ratchet import context as ctx_mod
from ratchet import qodo
from ratchet.bus import Bus
from ratchet.qodo import (
    QodoFinding,
    QodoOracle,
    QodoReview,
    oracle_or_none,
    parse_counts,
    parse_findings,
    repo_slug,
)

FIXTURE = (Path(__file__).parent / "fixtures" / "qodo_review_comment.md").read_text()


# ------------------------------------------------------------------- parsing --


def test_parse_findings_extracts_title_tags_description_and_agent_prompt():
    findings = parse_findings(FIXTURE)
    assert len(findings) >= 2
    first = findings[0]
    assert first.n == 1
    assert first.title  # strikethrough <s> wrappers are stripped by _clean
    assert "<" not in first.title
    assert first.tags
    prompted = [f for f in findings if f.agent_prompt]
    assert prompted, "fixture must carry at least one Agent prompt block"
    assert prompted[0].agent_prompt.startswith("The issue below was found")
    assert not any(line.startswith(">") for line in prompted[0].agent_prompt.splitlines())


def test_resolved_findings_are_flagged_and_kept_out_of_open_findings():
    # the bot keeps fixed findings in the comment, agent prompt and all; re-running
    # those prompts asks the model to redo work that already landed
    findings = parse_findings(FIXTURE)
    resolved = [f for f in findings if f.resolved]
    assert resolved, "fixture must carry at least one '✓ Resolved' finding"
    assert all("Resolved" in f.tags for f in resolved)
    review = QodoReview(pr=1, reviewed_at="t", findings=findings)
    assert review.open_findings == [f for f in findings if not f.resolved]
    assert len(review.open_findings) < len(findings)


def test_parse_findings_dedupes_repeated_summaries():
    body = FIXTURE + FIXTURE  # the bot repeats finding summaries in later sections
    once = parse_findings(FIXTURE)
    doubled = parse_findings(body)
    assert [f.title for f in doubled] == [f.title for f in once]


def test_parse_counts_first_match_per_category_wins():
    body = "<code>🐞 Bugs (8)</code> ... <code>Bugs (2)</code> <code>Rule violations (4)</code>"
    assert parse_counts(body) == {"bugs": 8, "rule violations": 4}
    assert parse_counts(FIXTURE).get("bugs") == 8


# ----------------------------------------------------------------- repo slug --


def test_repo_slug_handles_ssh_and_https_remotes(monkeypatch, tmp_path):
    def fake_run(argv, **kw):
        return SimpleNamespace(stdout=fake_run.url + "\n", returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    fake_run.url = "git@github.com:owner/repo.git"
    assert repo_slug(tmp_path) == "owner/repo"
    fake_run.url = "https://github.com/owner/repo"
    assert repo_slug(tmp_path) == "owner/repo"
    fake_run.url = ""
    assert repo_slug(tmp_path) is None


# -------------------------------------------------------------------- oracle --


def test_oracle_is_a_noop_without_gh(monkeypatch, tmp_path):
    monkeypatch.setattr(qodo.shutil, "which", lambda _: None)
    oracle = QodoOracle(tmp_path, slug="owner/repo")
    assert not oracle.available()
    assert oracle.findings_for_prompt() == ""
    assert oracle_or_none(SimpleNamespace(qodo=True), tmp_path) is None
    # and the kill switch wins even with gh present
    monkeypatch.setattr(qodo.shutil, "which", lambda _: "/usr/bin/gh")
    assert oracle_or_none(SimpleNamespace(qodo=False), tmp_path) is None


def _comments_json(body: str, updated_at: str) -> str:
    return json.dumps([{
        "user": {"login": "qodo-code-review[bot]"},
        "body": "Code Review by Qodo\n" + body,
        "updated_at": updated_at,
        "created_at": "2026-01-01T00:00:00Z",
    }])


def test_trigger_emits_requested_and_wait_rejects_the_ack_edit(monkeypatch, tmp_path):
    monkeypatch.setattr(qodo.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(qodo.time, "sleep", lambda _s: None)
    bus = Bus(tmp_path / "bus.jsonl")
    oracle = QodoOracle(tmp_path, bus, slug="owner/repo")

    # the ~7s ACK edit: newer timestamp but nothing parseable in the body
    ack = _comments_json("Qodo is busy working on this review", "2026-01-01T00:01:00Z")
    full = _comments_json(FIXTURE, "2026-01-01T00:02:00Z")
    responses = iter([
        SimpleNamespace(stdout="ok", returncode=0),   # gh pr comment /review
        SimpleNamespace(stdout=ack, returncode=0),    # poll 1: ACK -> rejected
        SimpleNamespace(stdout=full, returncode=0),   # poll 2: the real review
    ])
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: next(responses))

    assert oracle.trigger_review(14)
    review = oracle.wait_for_review(14, since="2026-01-01T00:00:30Z", timeout_s=60, poll_s=0)
    assert review is not None
    assert review.reviewed_at == "2026-01-01T00:02:00Z"
    assert review.counts.get("bugs") == 8

    kinds = [e.kind for e in Bus(tmp_path / "bus.jsonl").read_all()]
    assert "qodo.review.requested" in kinds
    assert "qodo.review.done" in kinds


def test_findings_for_prompt_respects_cap_and_serves_stale_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(qodo.shutil, "which", lambda _: "/usr/bin/gh")
    oracle = QodoOracle(tmp_path, slug="owner/repo", ttl_s=0)  # everything expires instantly
    oracle._cache_put(QodoReview(
        pr=7, reviewed_at="2026-01-01T00:00:00Z",
        findings=[QodoFinding(n=1, title="t" * 50, tags=["Bug"],
                              description="d", agent_prompt="p" * 5000)],
        counts={"bugs": 1},
    ))
    # gh is "down": every call fails, so latest_review falls back to the stale cache
    monkeypatch.setattr(subprocess, "run",
                        lambda argv, **kw: (_ for _ in ()).throw(OSError("offline")))
    text = oracle.findings_for_prompt(pr=7, cap=300)
    assert text
    assert len(text) <= 300


def test_context_render_includes_capped_qodo_section():
    ctx = ctx_mod.Context(task="t", repo_map="", failure="", diff_so_far="",
                          dead_ends=[], review="x" * (ctx_mod.MAX_REVIEW + 500))
    out = ctx.render()
    assert "Latest Qodo review" in out
    assert "x" * ctx_mod.MAX_REVIEW in out
    assert "x" * (ctx_mod.MAX_REVIEW + 1) not in out


def test_wait_for_review_timeout_is_none_not_the_previous_pass(monkeypatch, tmp_path):
    """A timeout must not surface the last review: qodo-fix reads that as
    'clean, nothing to fix' and exits 0 on a review that never happened."""
    monkeypatch.setattr(qodo.shutil, "which", lambda _: "/usr/bin/gh")
    oracle = QodoOracle(tmp_path, slug="owner/repo")
    oracle._cache_put(QodoReview(pr=9, reviewed_at="2026-01-01T00:00:00Z",
                                 findings=[], counts={"bugs": 0}))

    assert oracle.wait_for_review(9, since="2026-01-01T00:00:00Z", timeout_s=0) is None


def test_qodo_mcp_exposes_four_tools():
    from ratchet import qodo_mcp

    tools = {t.name for t in qodo_mcp.mcp._tool_manager.list_tools()}
    assert tools == {"qodo_status", "qodo_findings", "qodo_request_review", "qodo_wait_review"}
