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
from collections.abc import Callable, Iterable

from ..models import TestStatus

START = ">>>>> ratchet test output start"
END = ">>>>> ratchet test output end"
EXIT = ">>>>> ratchet exit code:"
#: emitted by the eval script when the pre-run revert of a protected path fails
RESET_FAILED = ">>>>> ratchet reset failed"

_EXIT_RE = re.compile(re.escape(EXIT) + r"\s*(-?\d+)")

#: ANSI escapes. A runner can be told to colour in its own config file, where no
#: environment variable reaches it, and a coloured `PASSED` does not match a status
#: token -- so a green suite grades as zero. Stripping is safe: an escape sequence
#: is never part of a test id, and a patch that injects one gains nothing, because
#: the exit code is still read from outside the parsed region.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

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
    """Only what sits between the markers is parsed. Everything else is untrusted.

    The LAST end marker bounds the region: suite output all lands before the real
    END, so a forged END printed from inside a test becomes inert text instead of
    truncating the parsed region and hiding the real results after it (found by
    review)."""
    body = log.split(START, 1)[1].rsplit(END, 1)[0] if (START in log and END in log) else log
    # Escapes are stripped last, and only inside the trusted region. Safe: an escape
    # sequence is never part of a test id, and a patch gains nothing by injecting
    # one, because the exit code is read from the region after END.
    return _ANSI.sub("", body)


def parse_exit_code(log: str) -> int | None:
    """Echoed *outside* the markers -- specifically AFTER the end marker, so only
    that region is parsed. A patch can print an identical-looking marker line from
    inside the suite, but everything it prints lands before END; parsing the tail
    region means the forged copy is never read. (Found by review: first-match
    parsing over the whole log accepted a forged `exit code: 0`.)"""
    # last END for the same reason as slice_output: a forged END inside the suite
    # must not promote a forged exit line into the trusted region
    region = log.rsplit(END, 1)[1] if END in log else log
    m = _EXIT_RE.search(region)
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
    infrastructure failure, never as a pass. Only the region BEFORE the start
    marker is checked -- the reset runs before the suite, and suite output (which
    the agent controls) all lands after START, so printing the marker from a test
    buys nothing at all.
    """
    region = log.split(START, 1)[0] if START in log else log
    return RESET_FAILED not in region


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


def failure_excerpt(
    log: str,
    status_map: dict[str, TestStatus],
    limit: int = 40,
    *,
    redact: Iterable[str] = (),
) -> str:
    """The most useful `limit` lines for the model: the failing names, then the tail.

    This is the agent's whole view of what went wrong, so it is worth being picky --
    and it is the one string that flows from the grader into the next prompt, so it
    is also where held-out test names would leak. `redact` takes the task's
    `f2p_hidden` ids: they are dropped from the failing header, and every appearance
    of an id, its file path or its bare function name in the log body is replaced.
    The *count* of held-out failures is kept -- the agent may know it is failing
    hidden tests (the f2p stage already says so); it may never learn which.
    """
    hidden = set(redact)
    is_red = lambda s: s in (TestStatus.FAILED, TestStatus.ERROR)  # noqa: E731
    failing = [t for t, s in status_map.items() if is_red(s) and t not in hidden]
    n_hidden_red = sum(1 for t, s in status_map.items() if is_red(s) and t in hidden)

    tokens: set[str] = set()
    for t in hidden:
        # every ::-segment separately: `file.py::MyTest::test_m` must also redact
        # pytest's unittest heading form `MyTest.test_m` and the bare method name
        tokens.add(t)
        tokens.update(seg for seg in t.split("::") if seg)
        tokens.update(seg.replace("::", ".") for seg in (t.partition("::")[2],) if seg)

    # Names are not enough: failure sections echo the failing test's *source and
    # rendered values* -- the exact inputs a patch would special-case. So a whole
    # failure block whose header names a held-out test is dropped, and any
    # remaining line that mentions a held-out token is replaced outright rather
    # than token-substituted (the short-summary line carries the assertion message).
    # Header shapes per shipped framework: pytest `___ name ___`, cargo
    # `---- name stdout ----`, jest/vitest `● name`, go `--- FAIL: name` / `=== RUN name`.
    def _is_header(stripped: str) -> bool:
        return (
            (len(stripped) > 6 and stripped.startswith("_") and stripped.endswith("_"))
            or (stripped.startswith("----") and stripped.endswith("----"))
            or stripped.startswith("●")
            or stripped.startswith(("--- FAIL:", "--- PASS:", "--- SKIP:", "=== RUN"))
        )

    kept: list[str] = []
    dropping = False
    for ln in slice_output(log).strip().splitlines():
        stripped = ln.strip()
        is_hdr = _is_header(stripped)
        if is_hdr or stripped.startswith("="):
            dropping = is_hdr and any(tok in ln for tok in tokens)
            if dropping:
                kept.append("<held-out test failed; details withheld>")
                continue
        if dropping:
            continue
        if any(tok in ln for tok in tokens):
            kept.append("<held-out test>")
            continue
        kept.append(ln)

    head = []
    if failing or n_hidden_red:
        shown = ", ".join(failing[:8])
        extra = f" (+{n_hidden_red} held-out)" if n_hidden_red else ""
        head = [f"failing: {shown}{extra}" if shown else f"failing: {n_hidden_red} held-out test(s)"]
    return "\n".join(head + kept[-limit:])
