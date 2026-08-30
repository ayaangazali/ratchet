"""The review gate's one rule: silence is not approval.

A review gate that answers instantly with a review from an hour ago is worse than
no gate at all -- it reports "reviewed, clean" for a diff Qodo has never seen. These
tests stub the GitHub call so they need no network, no token and no pull request.
"""

from __future__ import annotations

import pytest

from ratchet.qodo_mcp import QodoMCP, QodoUnavailable

STALE = {
    "user": {"login": "qodo-code-review[bot]"},
    "path": "ratchet/loop.py",
    "line": 12,
    "created_at": "2020-01-01T00:00:00Z",
    "body": "**Qodo**\n1\\. Stale finding\n<pre>from an old review</pre>\n"
            "![](https://img.shields.io/badge/High-orange)",
}


def _mcp(comments: list[dict], posted: list | None = None) -> QodoMCP:
    q = QodoMCP("owner/repo")

    def fake_gh(*args: str):
        if "--method" in args:           # the `/review` comment going out
            if posted is not None:
                posted.append(args)
            return {}
        return comments

    q._gh = fake_gh  # type: ignore[method-assign]
    return q


def test_a_review_from_before_the_request_does_not_count() -> None:
    """The whole bug: findings that predate our `/review` are not our review."""
    posted: list = []
    q = _mcp([STALE], posted)
    with pytest.raises(QodoUnavailable, match="silence is not approval"):
        q.review_pr("1", wait=0.2, poll=0.05)
    assert posted, "the gate must actually ask for a review, not only read"


def test_findings_posted_after_the_request_are_the_review() -> None:
    fresh = {**STALE, "created_at": "2099-01-01T00:00:00Z"}
    review = _mcp([fresh]).review_pr("1", wait=0.2, poll=0.05)
    assert len(review.findings) == 1 and review.blocking


def test_reading_without_waiting_still_shows_everything() -> None:
    """`fetch_findings` is the read path -- it is not a gate, so it hides nothing."""
    assert len(_mcp([STALE]).fetch_findings("1").findings) == 1
