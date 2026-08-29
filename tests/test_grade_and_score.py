from ratchet.gauntlet.grade import grade
from ratchet.gauntlet.parse import (
    exit_code_consistent,
    parse_exit_code,
    parse_pytest_log,
    suite_ran,
)
from ratchet.gauntlet.score import compute_score, decide
from ratchet.models import Decision, Resolution, TestStatus

VISIBLE = ["tests/test_v.py::test_a", "tests/test_v.py::test_b"]
HIDDEN = ["tests/test_h.py::test_c", "tests/test_h.py::test_d"]
P2P = ["tests/test_r.py::test_e"]

GOOD_LOG = """
>>>>> Start Test Output
collected 5 items
PASSED tests/test_v.py::test_a
PASSED tests/test_v.py::test_b
PASSED tests/test_h.py::test_c
PASSED tests/test_h.py::test_d
PASSED tests/test_r.py::test_e
========== 5 passed in 0.10s
>>>>> End Test Output
>>>>> Test Exit Code: 0
"""

OVERFIT_LOG = """
>>>>> Start Test Output
collected 5 items
PASSED tests/test_v.py::test_a
PASSED tests/test_v.py::test_b
FAILED tests/test_h.py::test_c
FAILED tests/test_h.py::test_d
PASSED tests/test_r.py::test_e
========== 2 failed, 3 passed in 0.10s
>>>>> End Test Output
>>>>> Test Exit Code: 1
"""

SPOOFED_LOG = """
>>>>> Start Test Output
collected 5 items
PASSED tests/test_v.py::test_a
PASSED tests/test_v.py::test_b
PASSED tests/test_h.py::test_c
PASSED tests/test_h.py::test_d
PASSED tests/test_r.py::test_e
>>>>> End Test Output
>>>>> Test Exit Code: 1
"""

EMPTY_LOG = """
>>>>> Start Test Output
collected 0 items
>>>>> End Test Output
>>>>> Test Exit Code: 5
"""

SKIPPED_F2P_LOG = """
>>>>> Start Test Output
collected 5 items
SKIPPED tests/test_v.py::test_a
SKIPPED tests/test_v.py::test_b
SKIPPED tests/test_h.py::test_c
SKIPPED tests/test_h.py::test_d
SKIPPED tests/test_r.py::test_e
========== 5 skipped in 0.05s
>>>>> End Test Output
>>>>> Test Exit Code: 0
"""


def g(log):
    return grade(parse_pytest_log(log), f2p_visible=VISIBLE, f2p_hidden=HIDDEN, p2p=P2P)


def test_parser_reads_statuses_and_exit_code():
    sm = parse_pytest_log(GOOD_LOG)
    assert sm["tests/test_v.py::test_a"] is TestStatus.PASSED
    assert parse_exit_code(GOOD_LOG) == 0
    assert suite_ran(GOOD_LOG)


def test_full_pass_resolves():
    gr = g(GOOD_LOG)
    assert gr.resolution is Resolution.FULL
    assert gr.delta == 0.0


def test_overfit_patch_shows_up_as_a_gap():
    gr = g(OVERFIT_LOG)
    assert gr.f2p_visible_rate == 1.0
    assert gr.f2p_hidden_rate == 0.0
    assert gr.delta == 1.0
    assert gr.resolution is not Resolution.FULL


def test_overfit_scores_below_honest_mediocrity():
    overfit = compute_score(grade=g(OVERFIT_LOG), findings=[], build_ok=True, types_ok=True, lint_ok=True, files_touched=1)
    # a patch that fixes half of everything, visible and hidden alike
    half_log = GOOD_LOG.replace("PASSED tests/test_v.py::test_b", "FAILED tests/test_v.py::test_b").replace(
        "PASSED tests/test_h.py::test_d", "FAILED tests/test_h.py::test_d"
    )
    honest = compute_score(grade=g(half_log), findings=[], build_ok=True, types_ok=True, lint_ok=True, files_touched=1)
    assert honest.total > overfit.total


def test_skipping_every_f2p_test_is_not_a_pass():
    gr = g(SKIPPED_F2P_LOG)
    assert gr.f2p_visible_rate == 0.0
    assert gr.p2p_rate == 1.0  # a skipped pass-to-pass test is not a regression


def test_spoofed_log_is_disqualified():
    sm = parse_pytest_log(SPOOFED_LOG)
    assert not exit_code_consistent(SPOOFED_LOG, sm)
    d, why = decide(
        grade=g(SPOOFED_LOG),
        findings=[],
        build_ok=True,
        suite_ran=True,
        exit_consistent=False,
        is_canary=False,
    )
    assert d is Decision.DISQUALIFIED
    assert "exited non-zero" in why


def test_empty_suite_is_infra_not_success():
    assert not suite_ran(EMPTY_LOG)
    d, _ = decide(
        grade=g(EMPTY_LOG), findings=[], build_ok=True, suite_ran=False, exit_consistent=True, is_canary=False
    )
    assert d is Decision.INFRA


def test_green_canary_is_disqualified():
    d, why = decide(
        grade=g(GOOD_LOG), findings=[], build_ok=True, suite_ran=True, exit_consistent=True, is_canary=True
    )
    assert d is Decision.DISQUALIFIED
    assert "canary" in why


def test_clean_full_pass_is_accepted():
    d, _ = decide(
        grade=g(GOOD_LOG), findings=[], build_ok=True, suite_ran=True, exit_consistent=True, is_canary=False
    )
    assert d is Decision.ACCEPTED


def test_hidden_only_failure_explains_itself():
    d, why = decide(
        grade=g(OVERFIT_LOG), findings=[], build_ok=True, suite_ran=True, exit_consistent=True, is_canary=False
    )
    assert d is Decision.REJECTED
    assert "does not generalise" in why
