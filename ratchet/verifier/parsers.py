"""Test output → a structured `{test_id: TestStatus}` map, per framework.

Adding a framework is adding one function and one registry entry. Verify a new one
against a real repo *before* the event, never on the day: parsing is fiddly, and a
parser that mis-reads a suite silently turns a red run green, which is the worst
failure this codebase has.

Two guards apply to every framework, and both exist because agents lie:

* `suite_ran` — if the status map is empty we must know whether the suite ran and
  reported nothing, or never started. Without it, "0 tests collected" grades as
  "no failures" and every empty patch looks green.
* `exit_code_consistent` — a patch can print its own `PASSED` lines from a
  conftest hook. If the log claims a clean sweep but the runner exited non-zero,
  the log is not evidence.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from ..models import TestStatus

START = ">>>>> ratchet test output start"
END = ">>>>> ratchet test output end"
EXIT = ">>>>> ratchet exit code:"
#: emitted by the eval script when the pre-run revert of a protected path fails
RESET_FAILED = ">>>>> ratchet reset failed"

_EXIT_RE = re.compile(re.escape(EXIT) + r"\s*(-?\d+)")

#: evidence that a runner actually executed at least one test
SUITE_RAN = [
    re.compile(r"collected [1-9]\d* items"),
    re.compile(r"=+ .*[1-9]\d* (passed|failed|error)"),
    re.compile(r"Ran [1-9]\d* tests?"),
    re.compile(r"Tests:\s+[1-9]\d*"),
    re.compile(r"[1-9]\d* passing"),
    re.compile(r"^(ok|FAIL)\s+\S+", re.M),  # go test
    re.compile(r"test result: (ok|FAILED)\."),  # cargo
]


def slice_output(log: str) -> str:
    """Only what sits between the markers is parsed. Everything else is untrusted."""
    if START in log and END in log:
        return log.split(START, 1)[1].split(END, 1)[0]
    return log


def parse_exit_code(log: str) -> int | None:
    """Echoed *outside* the markers, so a patch that fakes lines inside them cannot
    fake this."""
    m = _EXIT_RE.search(log)
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- #
# per-framework
# --------------------------------------------------------------------------- #


def parse_pytest(body: str) -> dict[str, TestStatus]:
    """`pytest -rA` short summary: `PASSED tests/test_x.py::test_y`."""
    statuses = {s.value for s in TestStatus}
    out: dict[str, TestStatus] = {}
    for raw in body.splitlines():
        parts = raw.strip().split()
        if len(parts) < 2 or parts[0] not in statuses:
            continue
        test_id, status = parts[1], TestStatus(parts[0])
        if out.get(test_id) in (TestStatus.FAILED, TestStatus.ERROR):
            continue  # a rerun plugin can report twice; failure wins
        out[test_id] = status
    return out


_JEST = re.compile(r"^\s*(✓|✔|√|✗|✕|×|○)\s+(.+?)(?:\s+\(\d+\s*m?s\))?$", re.M)
_JEST_STATUS = {"✓": TestStatus.PASSED, "✔": TestStatus.PASSED, "√": TestStatus.PASSED,
                "✗": TestStatus.FAILED, "✕": TestStatus.FAILED, "×": TestStatus.FAILED,
                "○": TestStatus.SKIPPED}


def parse_jest(body: str) -> dict[str, TestStatus]:
    out: dict[str, TestStatus] = {}
    for mark, name in _JEST.findall(body):
        out[name.strip()] = _JEST_STATUS.get(mark, TestStatus.FAILED)
    return out


_GO = re.compile(r"^\s*---\s+(PASS|FAIL|SKIP):\s+(\S+)", re.M)
_GO_STATUS = {"PASS": TestStatus.PASSED, "FAIL": TestStatus.FAILED, "SKIP": TestStatus.SKIPPED}


def parse_gotest(body: str) -> dict[str, TestStatus]:
    return {name: _GO_STATUS[status] for status, name in _GO.findall(body)}


_CARGO = re.compile(r"^test\s+(\S+)\s+\.\.\.\s+(ok|FAILED|ignored)", re.M)
_CARGO_STATUS = {"ok": TestStatus.PASSED, "FAILED": TestStatus.FAILED, "ignored": TestStatus.SKIPPED}


def parse_cargo(body: str) -> dict[str, TestStatus]:
    return {name: _CARGO_STATUS[status] for name, status in _CARGO.findall(body)}


PARSERS: dict[str, Callable[[str], dict[str, TestStatus]]] = {
    "pytest": parse_pytest,
    "jest": parse_jest,
    "vitest": parse_jest,
    "gotest": parse_gotest,
    "cargo": parse_cargo,
}


def parse(log: str, framework: str = "pytest") -> dict[str, TestStatus]:
    fn = PARSERS.get(framework)
    if fn is None:
        raise KeyError(f"no parser for framework {framework!r}; known: {sorted(PARSERS)}")
    return fn(slice_output(log))


# --------------------------------------------------------------------------- #
# guards
# --------------------------------------------------------------------------- #


def reset_ok(log: str) -> bool:
    """False when the pre-run revert of protected paths reported a failure.

    A reset that silently did not happen means the run would grade the agent's own
    edits to the graded tests. Fail closed: the gauntlet treats this as an
    infrastructure failure, never as a pass. The marker sits outside the parsed
    region, so the only thing a patch gains by printing it is an INFRA verdict
    against itself.
    """
    return RESET_FAILED not in log


def suite_ran(log: str) -> bool:
    body = slice_output(log)
    return any(p.search(body) for p in SUITE_RAN)


def exit_code_consistent(log: str, status_map: dict[str, TestStatus]) -> bool:
    """False when the log claims everything passed but the runner disagreed."""
    code = parse_exit_code(log)
    if code in (None, 0):
        return True
    if not status_map:
        return True  # nothing claimed; `suite_ran` handles that case
    return any(s in (TestStatus.FAILED, TestStatus.ERROR) for s in status_map.values())


def failure_excerpt(log: str, status_map: dict[str, TestStatus], limit: int = 40) -> str:
    """The most useful `limit` lines for the model: the failing names, then the tail.

    This is the agent's whole view of what went wrong, so it is worth being picky.
    """
    failing = [t for t, s in status_map.items() if s in (TestStatus.FAILED, TestStatus.ERROR)]
    body = slice_output(log).strip().splitlines()
    head = [f"failing: {', '.join(failing[:8])}"] if failing else []
    return "\n".join(head + body[-limit:])
