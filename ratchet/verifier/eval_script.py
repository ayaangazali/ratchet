"""The shell that actually grades a patch.

This is the load-bearing anti-tamper primitive in the whole project and it is about
fifteen lines. Order matters:

  1. revert every protected path to its pristine state  -> erases ANY agent edit to
     a graded test, whether it deleted it, skipped it or weakened an assertion
  2. run the suite between unambiguous markers
  3. echo the *test command's* exit code OUTSIDE the markers

Step 1 is why editing tests buys nothing: those files never reach the grader.
Step 3 is why printing fake `PASSED` lines buys nothing either -- the exit code is
outside the region a patch can write into, so a spoofed log contradicts itself.

There is deliberately no `set -e`: the reset and the exit-code echo must run even
when the suite fails, which is the common case.

Modelled on the SWE-bench harness. The command is a string because it is handed to
a sandbox the harness owns, not to a container we manage.
"""

from __future__ import annotations

import shlex

from .parsers import END, EXIT, RESET_FAILED, START


def _reset_lines(base_commit: str, protected_paths: list[str], *, quiet: bool = False) -> list[str]:
    """The revert, one protected path at a time.

    One combined `git checkout <base> -- a b c` looks equivalent and is not: git
    refuses the *entire* checkout when any one pathspec matches nothing in the base
    commit (a task protecting `conftest.py` in a repo that has none), and the agent's
    edits to every other protected path survive to the grader. So each path gets its
    own checkout, guarded by `ls-tree` so a legitimately absent path is skipped
    rather than reported as a failure.

    `git clean` is the other half of "pristine": checkout restores tracked content
    but leaves behind any file the patch *created* under a protected path (a new
    tests/conftest.py, say). `-x` includes ignored files -- without it, a created
    file matching an ignore pattern survived the reset (found by review). Scoped
    to the protected paths, so nothing outside them is touched.
    """
    base = shlex.quote(base_commit)
    tail = " >/dev/null 2>&1 || true" if quiet else f" || echo '{RESET_FAILED}'"
    lines: list[str] = []
    for p in protected_paths:
        q = shlex.quote(p)
        lines.append(f'if [ -n "$(git ls-tree --name-only {base} -- {q})" ]; then git checkout {base} -- {q}{tail}; fi')
        lines.append(f"git clean -fdxq -- {q}{tail}")
    return lines


def build_test_command(
    *,
    repo_dir: str,
    base_commit: str,
    protected_paths: list[str],
    test_cmd: str,
    timeout_s: int = 600,
    setup_cmd: str | None = None,
) -> str:
    lines = [
        f"cd {shlex.quote(repo_dir)}",
        "git config --global --add safe.directory '*' >/dev/null 2>&1 || true",
        # 1. pristine tests, every time, no flag to skip it
        *_reset_lines(base_commit, protected_paths),
    ]
    if setup_cmd:
        lines.append(setup_cmd)
    lines += [
        f"echo '{START}'",
        f"timeout {int(timeout_s)} {test_cmd}",
        "RATCHET_EXIT=$?",
        f"echo '{END}'",
        # 3. outside the markers on purpose
        f"echo '{EXIT}' $RATCHET_EXIT",
        # leave the tree clean for the next stage
        *_reset_lines(base_commit, protected_paths, quiet=True),
        "exit 0",
    ]
    return "\n".join(lines)


def build_stage_command(*, repo_dir: str, cmd: str, timeout_s: int = 300) -> str:
    """A plain stage (build, types, lint): exit code is the whole result."""
    return f"cd {shlex.quote(repo_dir)}\ntimeout {int(timeout_s)} {cmd}"
