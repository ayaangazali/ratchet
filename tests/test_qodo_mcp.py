"""The review gate's two rules.

Silence is not approval -- a reviewer that never answered must not read as a pass.
And a clean review is not silence -- a pull request Qodo looked at and liked has no
findings, and blocking on that would jam the gate shut on the good pull requests.
Confusing either direction breaks the gate, so both are tested.

The GitHub call is stubbed: no network, no token, no pull request.
"""

from __future__ import annotations

import pytest

from ratchet import qodo_mcp
from ratchet.qodo_mcp import REVIEW_DONE, QodoMCP, QodoUnavailable

BOT = {"login": "qodo-code-review[bot]"}
FINDING_BODY = (
    "**Qodo**\n1\\. Something is wrong\n<pre>the explanation</pre>\n"
    "![](https://img.shields.io/badge/High-orange)"
)


def _finding(cid: int) -> dict:
    return {"id": cid, "user": BOT, "path": "ratchet/loop.py", "line": 12, "body": FINDING_BODY}


def _summary(cid: int) -> dict:
    return {"id": cid, "user": BOT, "body": f"<h3>{REVIEW_DONE}</h3>"}


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch):
    """The real settle is six seconds of waiting for GitHub; the logic under test is
    not the sleep."""
    monkeypatch.setattr(qodo_mcp, "SETTLE", 0.0)


def _mcp(*, issues: list[dict], pulls: list[dict], on_post=None, posted: list | None = None) -> QodoMCP:
    """A pull request that already holds `issues` / `pulls`. `on_post` fires when the
    gate asks for a review and is where a new review is made to land -- so a test that
    supplies none is testing a reviewer that never answered."""
    q = QodoMCP("owner/repo")

    def fake_gh(*args: str):
        if "--method" in args:
            if posted is not None:
                posted.append(args)
            if on_post is not None:
                on_post()
            return {}
        return issues if "/issues/" in args[0] else pulls

    q._gh = fake_gh  # type: ignore[method-assign]
    return q


def test_a_review_from_before_we_asked_is_not_our_review() -> None:
    """The original bug: findings already on the pull request answered instantly."""
    posted: list = []
    q = _mcp(issues=[_summary(1)], pulls=[_finding(10)], posted=posted)
    with pytest.raises(QodoUnavailable, match="silence is not approval"):
        q.review_pr("1", wait=0.2, poll=0.05)
    assert posted, "the gate must actually ask for a review, not only read"


def test_a_new_review_with_no_findings_is_clean_not_a_timeout() -> None:
    """Qodo's summary comment is the completion signal. It arrives whether or not
    anything was found, so an empty review is an answer -- and it must not raise."""
    issues: list[dict] = []
    q = _mcp(issues=issues, pulls=[], on_post=lambda: issues.append(_summary(99)))
    review = q.review_pr("1", wait=0.5, poll=0.05)
    assert review.findings == [] and review.clean


def test_a_new_review_does_not_inherit_the_old_review_s_findings() -> None:
    """Qodo re-reviews and finds one thing; the four it flagged last week are gone.
    Reporting five would be reporting resolved findings as live."""
    issues, pulls = [_summary(1)], [_finding(i) for i in (10, 11, 12, 13)]

    def new_review() -> None:
        issues.append(_summary(2))          # the new review lands
        pulls.append(_finding(14))          # carrying exactly one new finding

    q = _mcp(issues=issues, pulls=pulls, on_post=new_review)
    review = q.review_pr("1", wait=0.5, poll=0.05)
    assert [f.title for f in review.findings] == ["Something is wrong"]
    assert review.blocking and not review.clean


def test_the_wait_is_a_deadline_not_a_suggestion() -> None:
    """A poll longer than the wait must not sleep past it."""
    import time

    q = _mcp(issues=[], pulls=[])
    t0 = time.time()
    with pytest.raises(QodoUnavailable):
        q.review_pr("1", wait=0.2, poll=30)
    assert time.time() - t0 < 2


def test_reading_without_waiting_still_shows_everything() -> None:
    """`fetch_findings` is the read path, not the gate, so it hides nothing."""
    assert len(_mcp(issues=[], pulls=[_finding(10)]).fetch_findings("1").findings) == 1
