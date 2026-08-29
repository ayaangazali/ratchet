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

from .parsers import END, EXIT, START


def build_test_command(
    *,
    repo_dir: str,
    base_commit: str,
    protected_paths: list[str],
    test_cmd: str,
    timeout_s: int = 600,
    setup_cmd: str | None = None,
) -> str:
    protected = " ".join(shlex.quote(p) for p in protected_paths) or "."
    lines = [
        f"cd {shlex.quote(repo_dir)}",
        "git config --global --add safe.directory '*' >/dev/null 2>&1 || true",
        # 1. pristine tests, every time, no flag to skip it
        f"git checkout {shlex.quote(base_commit)} -- {protected} || echo '>>>>> ratchet reset failed'",
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
        f"git checkout {shlex.quote(base_commit)} -- {protected} >/dev/null 2>&1 || true",
        "exit 0",
    ]
    return "\n".join(lines)


def build_stage_command(*, repo_dir: str, cmd: str, timeout_s: int = 300) -> str:
    """A plain stage (build, types, lint): exit code is the whole result."""
    return f"cd {shlex.quote(repo_dir)}\ntimeout {int(timeout_s)} {cmd}"
