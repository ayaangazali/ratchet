"""Turn raw test output into a {test_id: TestStatus} map, defensively.

Two things here are not obvious and both exist because agents lie:

* `SUITE_RAN` -- if the status map is empty we must know whether the suite ran and
  reported nothing, or never started at all. Without this, "0 tests collected"
  grades as "no F2P failures" and every empty patch scores green.
* `exit_code_consistent` -- a patch can print its own `PASSED` lines from a
  conftest hook. If the log claims a clean sweep but the runner exited non-zero,
  the log is not trustworthy and the run is invalid.
"""

from __future__ import annotations

import re

from ..models import TestStatus
from .eval_script import END, EXIT, START

_STATUSES = {s.value for s in TestStatus}

#: evidence that a runner actually executed at least one test
SUITE_RAN = [
    re.compile(r"collected [1-9]\d* items"),
    re.compile(r"=+ .*[1-9]\d* (passed|failed|error)"),
    re.compile(r"Ran [1-9]\d* tests?"),
    re.compile(r"Tests:\s+[1-9]\d*"),
    re.compile(r"[1-9]\d* passing"),
]

_EXIT_RE = re.compile(re.escape(EXIT) + r"\s*(-?\d+)")


def slice_test_output(log: str) -> str:
    """Return only what sits between the markers. Everything else is untrusted."""
    if START in log and END in log:
        return log.split(START, 1)[1].split(END, 1)[0]
    return log


def parse_exit_code(log: str) -> int | None:
    """The exit code is echoed *outside* the parsed region, so a patch that fakes
    log lines inside the markers cannot fake this."""
    m = _EXIT_RE.search(log)
    return int(m.group(1)) if m else None


def parse_pytest_log(log: str) -> dict[str, TestStatus]:
    """`pytest -rA` short-summary lines look like `PASSED tests/test_x.py::test_y`."""
    body = slice_test_output(log)
    status_map: dict[str, TestStatus] = {}
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2 or parts[0] not in _STATUSES:
            continue
        test_id = parts[1]
        status = TestStatus(parts[0])
        prev = status_map.get(test_id)
        # a test can be reported twice (e.g. rerun plugins); failure wins
        if prev in (TestStatus.FAILED, TestStatus.ERROR):
            continue
        status_map[test_id] = status
    return status_map


def suite_ran(log: str) -> bool:
    body = slice_test_output(log)
    return any(p.search(body) for p in SUITE_RAN)


def exit_code_consistent(log: str, status_map: dict[str, TestStatus]) -> bool:
    """False when the log claims everything passed but the runner disagreed."""
    code = parse_exit_code(log)
    if code in (None, 0):
        return True
    if not status_map:
        return True  # nothing claimed; handled by suite_ran instead
    return any(s in (TestStatus.FAILED, TestStatus.ERROR) for s in status_map.values())


def tail(log: str, n: int = 40) -> str:
    body = slice_test_output(log).strip().splitlines()
    return "\n".join(body[-n:])
