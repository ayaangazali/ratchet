"""Parsing and grading — the two places where a bug turns a red run green.

Everything here is a pure function of a log string, which is why these tests run in
milliseconds and why the rules they encode are worth trusting.
"""

from __future__ import annotations

from ratchet.models import TestStatus
from ratchet.verifier.grade import grade, impossible_passes
from ratchet.verifier.parsers import (
    END,
    EXIT,
    RESET_FAILED,
    START,
    exit_code_consistent,
    failure_excerpt,
    parse,
    parse_exit_code,
    reset_ok,
    suite_ran,
)

VISIBLE = ["tests/test_v.py::test_a", "tests/test_v.py::test_b"]
HIDDEN = ["tests/test_h.py::test_c", "tests/test_h.py::test_d"]
P2P = ["tests/test_r.py::test_e"]


def log(body: str, exit_code: int = 0) -> str:
    return f"\n{START}\n{body}\n{END}\n{EXIT} {exit_code}\n"


GOOD = log(
    "collected 5 items\n"
    "PASSED tests/test_v.py::test_a\nPASSED tests/test_v.py::test_b\n"
    "PASSED tests/test_h.py::test_c\nPASSED tests/test_h.py::test_d\n"
    "PASSED tests/test_r.py::test_e\n========== 5 passed in 0.10s"
)

OVERFIT = log(
    "collected 5 items\n"
    "PASSED tests/test_v.py::test_a\nPASSED tests/test_v.py::test_b\n"
    "FAILED tests/test_h.py::test_c\nFAILED tests/test_h.py::test_d\n"
    "PASSED tests/test_r.py::test_e\n========== 2 failed, 3 passed in 0.10s",
    exit_code=1,
)

SPOOFED = log(
    "collected 5 items\n"
    "PASSED tests/test_v.py::test_a\nPASSED tests/test_v.py::test_b\n"
    "PASSED tests/test_h.py::test_c\nPASSED tests/test_h.py::test_d\n"
    "PASSED tests/test_r.py::test_e",
    exit_code=1,
)

EMPTY = log("collected 0 items", exit_code=5)

ALL_SKIPPED = log(
    "collected 5 items\n"
    "SKIPPED tests/test_v.py::test_a\nSKIPPED tests/test_v.py::test_b\n"
    "SKIPPED tests/test_h.py::test_c\nSKIPPED tests/test_h.py::test_d\n"
    "SKIPPED tests/test_r.py::test_e\n========== 5 skipped in 0.05s"
)

DELETED = log("collected 1 items\nPASSED tests/test_r.py::test_e\n========== 1 passed in 0.01s")


def g(text: str):
    return grade(parse(text), f2p_visible=VISIBLE, f2p_hidden=HIDDEN, p2p=P2P)


# --------------------------------------------------------------------- parse --


def test_parser_reads_statuses_and_exit_code():
    sm = parse(GOOD)
    assert sm["tests/test_v.py::test_a"] is TestStatus.PASSED
    assert parse_exit_code(GOOD) == 0
    assert suite_ran(GOOD)


def test_only_text_between_the_markers_is_parsed():
    noise = "PASSED tests/test_h.py::test_c\n" + GOOD  # a line printed before the run
    sm = parse(noise)
    assert len(sm) == 5  # the pre-marker line is ignored, not counted


def test_other_frameworks_parse():
    jest = log("  ✓ renders the header\n  ✕ handles an empty list\n  Tests: 2 total")
    sm = parse(jest, "jest")
    assert sm["renders the header"] is TestStatus.PASSED
    assert sm["handles an empty list"] is TestStatus.FAILED

    go = log("--- PASS: TestSlugify/basic\n--- FAIL: TestSlugify/accents\nok  \tpkg/textkit\t0.01s")
    sm = parse(go, "gotest")
    assert sm["TestSlugify/accents"] is TestStatus.FAILED


# --------------------------------------------------------------------- grade --


def test_full_pass():
    gr = g(GOOD)
    assert gr.f2p_ratio == 1.0
    assert gr.p2p_intact
    assert gr.delta == 0.0


def test_overfit_shows_up_as_a_gap_and_costs_score():
    gr = g(OVERFIT)
    assert gr.f2p_visible_rate == 1.0
    assert gr.f2p_hidden_rate == 0.0
    assert gr.delta == 1.0
    assert gr.f2p_ratio == 0.5  # the pooled ratio is what the score uses


def test_skipping_every_target_test_is_not_a_pass():
    gr = g(ALL_SKIPPED)
    assert gr.f2p_ratio == 0.0
    assert gr.p2p_intact  # a skipped pass-to-pass test is not a regression


def test_deleting_a_target_test_does_not_help():
    gr = g(DELETED)
    assert gr.f2p_ratio == 0.0  # missing from the map counts as failure


# -------------------------------------------------------------------- guards --


def test_spoofed_log_contradicts_the_exit_code():
    sm = parse(SPOOFED)
    assert not exit_code_consistent(SPOOFED, sm)


def test_honest_failing_log_is_consistent():
    assert exit_code_consistent(OVERFIT, parse(OVERFIT))


def test_empty_suite_is_not_a_clean_sweep():
    assert not suite_ran(EMPTY)
    assert parse(EMPTY) == {}


def test_a_forged_exit_marker_inside_the_region_is_ignored():
    """A patch can print an identical marker line from inside the suite; only the
    region after END -- which the suite cannot write into -- is parsed."""
    forged = log(f"{EXIT} 0\ncollected 1 items\nPASSED tests/test_v.py::test_a", exit_code=2)
    assert parse_exit_code(forged) == 2


def test_a_forged_end_marker_does_not_truncate_or_promote():
    """A test that prints its own END + exit-code lines must not hide the real
    results after it, nor promote its forged exit code into the trusted region."""
    forged = (
        f"\n{START}\n"
        "collected 2 items\n"
        "PASSED tests/test_v.py::test_a\n"
        f"{END}\n{EXIT} 0\n"           # forged, printed by the suite
        "FAILED tests/test_v.py::test_b\n"
        f"{END}\n{EXIT} 1\n"           # the real pair
    )
    assert parse_exit_code(forged) == 1          # last END bounds the trusted tail
    sm = parse(forged)
    assert sm["tests/test_v.py::test_b"] is TestStatus.FAILED  # real result still parsed


def test_impossible_passes_names_the_spoofed_tests():
    sm = parse(log(
        "collected 1 items\n"
        "PASSED tests/test_v.py::test_a\n"
        "ERROR tests/test_v.py\n"
        "!!!! Interrupted: 1 error during collection !!!!", exit_code=2))
    assert impossible_passes(sm, ["tests/test_v.py::test_a"]) == ["tests/test_v.py::test_a"]


def test_a_partial_suite_with_honest_failures_is_not_impossible():
    """An objective-graph node grades only its slice; other tests failing honestly
    must not read as a spoof."""
    sm = parse(OVERFIT)
    assert impossible_passes(sm, VISIBLE) == []


def test_a_reset_marker_printed_by_the_suite_is_ignored():
    """The reset runs before START; a test that prints the failure marker cannot
    force INFRA verdicts (self-DoS) because only the pre-START region is checked."""
    spoof = log(RESET_FAILED + "\ncollected 1 items\nPASSED tests/test_v.py::test_a")
    assert reset_ok(spoof)


def test_non_pytest_failure_blocks_are_withheld_too():
    """Cargo (and jest/go) failure sections must be suppressed like pytest's."""
    body = log(
        "test result: FAILED.\n"
        "---- test_c stdout ----\n"
        "thread panicked at 'assertion failed: secret-cargo-input'\n"
        "================\n"
        "failures: test_c",
        exit_code=101,
    )
    out = failure_excerpt(body, {}, redact=["test_c"])
    assert "secret-cargo-input" not in out


def test_class_based_hidden_ids_are_fully_redacted():
    hidden = ["tests/test_h.py::MyTest::test_method"]
    body = log(
        "collected 1 items\n"
        "FAILED tests/test_h.py::MyTest::test_method\n"
        "____ MyTest.test_method ____\n"
        "E   AssertionError: secret-class-value",
        exit_code=1,
    )
    out = failure_excerpt(body, parse(body), redact=hidden)
    assert "test_method" not in out and "secret-class-value" not in out


def test_a_failed_protected_path_reset_is_not_ok():
    # the suite may look perfectly green -- if the revert failed, it graded the
    # agent's own edits to the tests, and nothing in that log is evidence
    assert not reset_ok(RESET_FAILED + "\n" + GOOD)


def test_a_clean_log_passes_the_reset_guard():
    assert reset_ok(GOOD)


# ------------------------------------------------------------------ redaction --


def test_failure_excerpt_never_names_a_held_out_test():
    """CLAUDE.md invariant 5: this string flows into the next prompt."""
    out = failure_excerpt(OVERFIT, parse(OVERFIT), redact=HIDDEN)
    for test_id in HIDDEN:
        assert test_id not in out
        path, _, name = test_id.partition("::")
        assert path not in out
        assert name not in out
    assert "held-out" in out  # the count survives; the names do not


def test_failure_excerpt_withholds_held_out_failure_details():
    """Names are not the only leak: pytest's FAILURES section echoes the held-out
    test's source and rendered values -- the exact inputs a patch would special-case."""
    block = log(
        "collected 5 items\n"
        "____________________ test_c ____________________\n"
        '    assert slugify("Cafe Creme secret-input") == "cafe-creme"\n'
        "E   AssertionError: assert 'cafe-crme secret-rendered' == 'cafe-creme'\n"
        "=========================== short test summary info ===========================\n"
        "FAILED tests/test_h.py::test_c - AssertionError: assert 'secret-rendered'\n"
        "========== 1 failed in 0.10s",
        exit_code=1,
    )
    out = failure_excerpt(block, parse(block), redact=HIDDEN)
    assert "secret-input" not in out
    assert "secret-rendered" not in out
    assert "withheld" in out


def test_failure_excerpt_still_names_visible_failures():
    """The must-not-trip half: redaction must not eat the agent's real feedback."""
    log = OVERFIT.replace(
        "PASSED tests/test_v.py::test_a", "FAILED tests/test_v.py::test_a"
    )
    out = failure_excerpt(log, parse(log), redact=HIDDEN)
    assert "tests/test_v.py::test_a" in out


# --------------------------------------------------------------------------- #
# colour
# --------------------------------------------------------------------------- #

COLOURED = log(
    "collected 2 items\n"
    "\x1b[32mPASSED\x1b[0m tests/test_v.py::\x1b[1mtest_a\x1b[0m\n"
    "\x1b[31mFAILED\x1b[0m tests/test_v.py::\x1b[1mtest_b\x1b[0m - AssertionError\n"
    "\x1b[31m===== \x1b[1m1 failed\x1b[0m, \x1b[32m1 passed\x1b[0m in 0.03s",
    exit_code=1,
)


def test_a_coloured_log_still_grades():
    """The patch that must pass.

    `FORCE_COLOR` in an inherited environment makes pytest wrap every status token
    in escapes. The grader splits those lines on whitespace, so a fully green suite
    scored 0/6, the patch was called broken, nothing could go green and the search
    spun until its wall clock. This was live on macOS, not hypothetical.
    """
    statuses = parse(COLOURED, "pytest")
    assert statuses == {
        "tests/test_v.py::test_a": TestStatus.PASSED,
        "tests/test_v.py::test_b": TestStatus.FAILED,
    }
    assert suite_ran(COLOURED)


def test_stripping_colour_does_not_weaken_the_exit_code_cross_check():
    """The patch that must NOT pass.

    Stripping escapes is normalisation, and normalisation must not become a hole:
    a patch that prints its own green summary in colour still contradicts a non-zero
    exit code, and the exit code is written outside the region a patch can reach.
    """
    spoofed = log(
        "collected 2 items\n"
        "\x1b[32mPASSED\x1b[0m tests/test_v.py::test_a\n"
        "\x1b[32mPASSED\x1b[0m tests/test_v.py::test_b\n"
        "\x1b[32m===== 2 passed\x1b[0m",
        exit_code=1,
    )
    statuses = parse(spoofed, "pytest")
    assert all(s is TestStatus.PASSED for s in statuses.values())
    assert not exit_code_consistent(spoofed, statuses), "a colourful lie is still a lie"


def test_the_timeout_wrapper_is_portable():
    """`timeout(1)` is GNU coreutils; macOS ships neither it nor a stub, so every
    graded command died with "command not found" and read as a failed build."""
    from ratchet.verifier.eval_script import build_test_command

    cmd = build_test_command(
        repo_dir=".", base_commit="HEAD", protected_paths=["tests/"],
        test_cmd="python -m pytest -rA", timeout_s=60,
    )
    assert "_ratchet_run 60 python -m pytest -rA" in cmd
    assert "command -v gtimeout" in cmd, "the macOS fallback has to be in the emitted shell"
    assert "NO_COLOR=1" in cmd, "colour off inside the sandbox, where the config lives"
