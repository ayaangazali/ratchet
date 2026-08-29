"""Parsing and grading — the two places where a bug turns a red run green.

Everything here is a pure function of a log string, which is why these tests run in
milliseconds and why the rules they encode are worth trusting.
"""

from __future__ import annotations

from ratchet.models import TestStatus
from ratchet.verifier.grade import grade
from ratchet.verifier.parsers import END, EXIT, START, exit_code_consistent, parse, parse_exit_code, suite_ran

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
