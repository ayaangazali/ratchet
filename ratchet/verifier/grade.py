"""Grade a status map against a task's fail-to-pass, held-out and pass-to-pass sets.

The asymmetry between `passed` and `maintained` is copied from the SWE-bench harness
and it is load-bearing:

  * a SKIPPED fail-to-pass test is **not** a resolution -- otherwise adding
    `@pytest.mark.skip` is a free win
  * a SKIPPED pass-to-pass test is **not** a regression -- some suites legitimately
    skip depending on the environment
  * a test that is **missing** from the map counts as a failure -- deleting it does
    not help either

`f2p_ratio` pools the visible and held-out tests, because that ratio is the score's
main term and a patch fitted to the visible cases should lose points for it. `delta`
keeps the two apart for diagnosis only.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import TestStatus


def impossible_passes(status_map: dict[str, TestStatus], graded: list[str]) -> list[str]:
    """Graded tests that report PASSED inside a file that failed to collect.

    That pair is impossible for a real run -- a test cannot execute when its own
    file errored -- so its presence means the PASSED lines were printed, not
    earned. Pure so it can be unit-tested against fabricated maps; the gauntlet
    treats any hit as log_spoofed."""
    return [
        t for t in graded
        if status_map.get(t) is TestStatus.PASSED
        and status_map.get(t.partition("::")[0]) in (TestStatus.FAILED, TestStatus.ERROR)
    ]


def test_passed(case: str, sm: dict[str, TestStatus]) -> bool:
    return sm.get(case) in (TestStatus.PASSED, TestStatus.XFAIL)


def test_maintained(case: str, sm: dict[str, TestStatus]) -> bool:
    return test_passed(case, sm) or sm.get(case) is TestStatus.SKIPPED


@dataclass
class Grade:
    f2p_visible_rate: float
    f2p_hidden_rate: float
    p2p_rate: float
    f2p_ratio: float
    f2p_visible: dict[str, list[str]]
    f2p_hidden: dict[str, list[str]]
    p2p: dict[str, list[str]]

    @property
    def delta(self) -> float:
        """Aces what it can see, flunks what it cannot. The overfitting tell."""
        return max(0.0, self.f2p_visible_rate - self.f2p_hidden_rate)

    @property
    def p2p_intact(self) -> bool:
        return not self.p2p["failure"]


def _split(cases: list[str], sm: dict[str, TestStatus], *, maintained: bool) -> dict[str, list[str]]:
    ok = test_maintained if maintained else test_passed
    return {
        "success": [c for c in cases if ok(c, sm)],
        "failure": [c for c in cases if not ok(c, sm)],
    }


def _rate(buckets: dict[str, list[str]]) -> float:
    n = len(buckets["success"]) + len(buckets["failure"])
    return 1.0 if n == 0 else len(buckets["success"]) / n


def grade(
    status_map: dict[str, TestStatus],
    *,
    f2p_visible: list[str],
    f2p_hidden: list[str],
    p2p: list[str],
) -> Grade:
    v = _split(f2p_visible, status_map, maintained=False)
    h = _split(f2p_hidden, status_map, maintained=False)
    p = _split(p2p, status_map, maintained=True)
    pooled = {"success": v["success"] + h["success"], "failure": v["failure"] + h["failure"]}
    return Grade(
        f2p_visible_rate=_rate(v),
        f2p_hidden_rate=_rate(h),
        p2p_rate=_rate(p),
        f2p_ratio=_rate(pooled),
        f2p_visible=v,
        f2p_hidden=h,
        p2p=p,
    )
