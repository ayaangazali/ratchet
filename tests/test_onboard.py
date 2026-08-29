"""`ratchet go` -- the observations that become a task.

The point of `onboard` is that nothing about the task is asked for: the framework
comes from the files on disk, and the fail-to-pass / pass-to-pass split comes from
running the suite once. That makes almost all of it a pure function of a directory
listing or a log string, and therefore cheap to pin down here.

The ANSI test is not hypothetical. `FORCE_COLOR` in an inherited environment made
`pytest -rA` emit escape sequences, `parse_pytest` splits its lines on whitespace,
and a six-failure suite came back as "no tests ran" -- which reads exactly like a
green repository. That is the worst shape a bug can take in this codebase.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ratchet.config import load_task
from ratchet.models import TestStatus
from ratchet.onboard import _ANSI, detect, normalise_url, repo_name, split_held_out, write_task
from ratchet.verifier import parsers

# --------------------------------------------------------------------------- #
# urls
# --------------------------------------------------------------------------- #


def test_normalise_url_passes_a_full_url_through() -> None:
    assert normalise_url("https://github.com/psf/requests") == "https://github.com/psf/requests"


def test_normalise_url_expands_owner_slash_repo() -> None:
    assert normalise_url("psf/requests") == "https://github.com/psf/requests"


def test_normalise_url_makes_a_local_path_absolute(tmp_path: Path, monkeypatch) -> None:
    """The clone runs with its cwd set to the destination's parent, so a relative
    path would resolve against the wrong directory and fail confusingly."""
    (tmp_path / "myrepo").mkdir()
    monkeypatch.chdir(tmp_path)
    assert normalise_url("myrepo") == str((tmp_path / "myrepo").resolve())


def test_repo_name_strips_the_git_suffix() -> None:
    assert repo_name("https://github.com/ayaangazali/ratchet.git") == "ratchet"


# --------------------------------------------------------------------------- #
# the held-out split
# --------------------------------------------------------------------------- #


def test_split_held_out_interleaves() -> None:
    """Interleaved, not sliced: adjacent test names in a file usually cover the same
    behaviour with different inputs, which is what a held-out slice should be."""
    visible, hidden = split_held_out(["t::d", "t::a", "t::c", "t::b"])
    assert visible == ["t::a", "t::c"]
    assert hidden == ["t::b", "t::d"]


def test_split_held_out_keeps_a_lone_failure_visible() -> None:
    """One failing test cannot be held out: an empty visible list would leave the
    agent with no statement of the job at all."""
    assert split_held_out(["t::only"]) == (["t::only"], [])
    assert split_held_out([]) == ([], [])


def test_split_held_out_never_puts_a_name_in_both_halves() -> None:
    visible, hidden = split_held_out([f"t::t{i}" for i in range(9)])
    assert not set(visible) & set(hidden)
    assert len(visible) + len(hidden) == 9


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #


def _repo(root: Path, files: dict[str, str], dirs: tuple[str, ...] = ()) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for d in dirs:
        (root / d).mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


@pytest.mark.parametrize(
    "files,dirs,framework",
    [
        ({"go.mod": "module x\n"}, (), "gotest"),
        ({"Cargo.toml": "[package]\n"}, (), "cargo"),
        ({"package.json": '{"devDependencies": {"vitest": "1"}}'}, (), "vitest"),
        ({"package.json": '{"devDependencies": {"jest": "29"}}'}, (), "jest"),
        ({"pyproject.toml": "[project]\n"}, ("tests",), "pytest"),
    ],
)
def test_detect_reads_the_framework_off_the_manifest(
    tmp_path: Path, files: dict[str, str], dirs: tuple[str, ...], framework: str
) -> None:
    assert detect(_repo(tmp_path / "r", files, dirs)).framework == framework


def test_detect_protects_the_test_directory_and_the_config(tmp_path: Path) -> None:
    """Protected paths are reverted before grading, so anything the agent could edit
    to change what 'pass' means has to end up in this list."""
    det = detect(_repo(tmp_path / "r", {"pyproject.toml": "", "conftest.py": ""}, ("tests", "src")))
    assert "tests/" in det.protected_paths
    assert "conftest.py" in det.protected_paths
    assert "pyproject.toml" in det.protected_paths
    assert det.source_paths == ["src/"]


def test_detect_falls_back_to_top_level_packages_without_a_src_dir(tmp_path: Path) -> None:
    root = _repo(tmp_path / "r", {"mypkg/__init__.py": "", "pyproject.toml": ""}, ("tests",))
    assert detect(root).source_paths == ["mypkg/"]


# --------------------------------------------------------------------------- #
# the colour bug
# --------------------------------------------------------------------------- #

_COLOURED = (
    "collected 2 items\n"
    "\x1b[32mPASSED\x1b[0m tests/test_a.py::\x1b[1mtest_ok\x1b[0m\n"
    "\x1b[31mFAILED\x1b[0m tests/test_a.py::\x1b[1mtest_bad\x1b[0m - AssertionError\n"
    "\x1b[31m===== \x1b[1m1 failed\x1b[0m, \x1b[32m1 passed\x1b[0m in 0.03s\n"
)


def test_coloured_output_is_unparseable_before_stripping() -> None:
    """The bug, stated as a test: escape sequences make a real suite look empty."""
    assert parsers.parse(_COLOURED, "pytest") == {}


def test_stripping_ansi_recovers_the_statuses() -> None:
    assert parsers.parse(_ANSI.sub("", _COLOURED), "pytest") == {
        "tests/test_a.py::test_ok": TestStatus.PASSED,
        "tests/test_a.py::test_bad": TestStatus.FAILED,
    }


# --------------------------------------------------------------------------- #
# the task file
# --------------------------------------------------------------------------- #


def test_write_task_produces_a_file_the_loader_accepts(tmp_path: Path) -> None:
    """The round trip is the point. A generated task that `load_task` rejects is a
    one-shot that fails on step five, in front of whoever you are demoing to."""
    repo = _repo(tmp_path / "repo", {"pyproject.toml": ""}, ("tests", "src"))
    statuses = {
        "tests/t.py::a": TestStatus.FAILED,
        "tests/t.py::b": TestStatus.FAILED,
        "tests/t.py::c": TestStatus.PASSED,
        "tests/t.py::d": TestStatus.ERROR,
    }
    path, visible, hidden, passing = write_task(
        repo=repo, det=detect(repo), statuses=statuses, out=tmp_path / "tasks" / "gen.yaml"
    )
    assert set(visible) | set(hidden) == {"tests/t.py::a", "tests/t.py::b", "tests/t.py::d"}
    assert passing == ["tests/t.py::c"]

    task = load_task(path)
    assert task.repo_path == str(repo)
    assert task.p2p == ["tests/t.py::c"]
    assert sorted(task.f2p_all) == ["tests/t.py::a", "tests/t.py::b", "tests/t.py::d"]
    assert "tests/" in task.protected_paths


def test_write_task_keeps_held_out_names_out_of_the_statement(tmp_path: Path) -> None:
    """Invariant 5: a held-out name in anything the agent reads destroys the signal,
    and the statement is the first thing the agent reads."""
    repo = _repo(tmp_path / "repo", {"pyproject.toml": ""}, ("tests",))
    statuses = {f"tests/t.py::t{i}": TestStatus.FAILED for i in range(6)}
    path, _visible, hidden, _passing = write_task(
        repo=repo, det=detect(repo), statuses=statuses, out=tmp_path / "t.yaml"
    )
    statement = load_task(path).statement
    assert hidden, "this test is meaningless if nothing was held out"
    for name in hidden:
        assert name.split("::")[-1] not in statement
