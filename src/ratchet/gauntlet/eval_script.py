"""Generates the bash that actually grades a patch.

This is the load-bearing anti-tamper primitive in the whole project, and it is
about fifteen lines of shell. Order matters:

  1. apply the agent's patch
  2. `git checkout <base> -- <protected paths>`  -> erase ANY agent edit to tests
  3. re-apply the pristine test patch             -> canonical tests, no exceptions
  4. run the suite between unambiguous markers
  5. echo the *test command's* exit code OUTSIDE the parsed region

Step 2 is why deleting a test, adding `@pytest.mark.skip`, weakening an assertion
or dropping a `conftest.py` hook buys the agent nothing: those files never make it
to the grader. Step 5 is why printing fake `PASSED` lines buys nothing either --
we cross-check the parsed log against the runner's real exit status.

Modelled on SWE-bench's `make_eval_script_list_py`; see
https://www.swebench.com/SWE-bench/reference/harness/
"""

from __future__ import annotations

import shlex

START = ">>>>> Start Test Output"
END = ">>>>> End Test Output"
EXIT = ">>>>> Test Exit Code:"
APPLIED = ">>>>> Applied Patch"
APPLY_FAILED = ">>>>> Patch Apply Failed"
RESET_FAILED = ">>>>> Reset Failed"

#: escalating apply strategies, exactly as the SWE-bench harness does it
GIT_APPLY_CMDS = [
    "git apply --verbose",
    "git apply --verbose --3way",
    "patch --batch --fuzz=5 -p1 -i",
]


def build_eval_script(
    *,
    repo_dir: str,
    base_commit: str,
    protected_paths: list[str],
    test_patch_path: str | None,
    test_cmd: str,
    directives: list[str],
    setup_cmd: str | None = None,
    timeout_s: int = 600,
) -> str:
    """Return a self-contained bash script that grades whatever is in the tree.

    Note the deliberate absence of `set -e`: we want the reset and the exit-code
    echo to run even when the suite fails, which is the common case.
    """
    protected = " ".join(shlex.quote(p) for p in protected_paths) or "."
    directive_str = " ".join(shlex.quote(d) for d in directives)
    lines = [
        "#!/bin/bash",
        "set -uxo pipefail",
        f"cd {shlex.quote(repo_dir)}",
        "git config --global --add safe.directory '*' || true",
        "",
        "# --- provenance: what did the agent actually change? -------------------",
        f"git -c core.fileMode=false diff {shlex.quote(base_commit)} > /tmp/ratchet_agent.diff || true",
        "",
        "# --- 1. reset protected paths to pristine ------------------------------",
        f"git checkout {shlex.quote(base_commit)} -- {protected} || echo '{RESET_FAILED}'",
    ]
    if test_patch_path:
        lines += [
            "",
            "# --- 2. re-apply the canonical test patch ------------------------------",
            f"git apply -v {shlex.quote(test_patch_path)} || echo '{RESET_FAILED}'",
        ]
    if setup_cmd:
        lines += ["", "# --- 3. environment setup ----------------------------------------------", setup_cmd]
    lines += [
        "",
        "# --- 4. run the suite --------------------------------------------------",
        f"echo '{START}'",
        f"timeout {int(timeout_s)} {test_cmd} {directive_str}",
        "RATCHET_TEST_EXIT_CODE=$?",
        f"echo '{END}'",
        f"echo '{EXIT}' $RATCHET_TEST_EXIT_CODE",
        "",
        "# --- 5. reset again so the next stage starts clean ---------------------",
        f"git checkout {shlex.quote(base_commit)} -- {protected} || true",
        "exit 0",
    ]
    return "\n".join(lines) + "\n"


def build_apply_script(*, repo_dir: str, patch_path: str) -> str:
    """Apply a patch with the escalating fallback chain, cleaning up between tries."""
    body = [
        "#!/bin/bash",
        "set -uxo pipefail",
        f"cd {shlex.quote(repo_dir)}",
        f"if [ ! -s {shlex.quote(patch_path)} ]; then echo '{APPLY_FAILED} (empty patch)'; exit 1; fi",
    ]
    for cmd in GIT_APPLY_CMDS:
        body += [
            f"if {cmd} {shlex.quote(patch_path)}; then echo '{APPLIED}'; exit 0; fi",
            "git checkout -- . ; git clean -fd",
        ]
    body += [
        f"if git apply --check --reverse {shlex.quote(patch_path)}; then echo '{APPLIED} (already applied)'; exit 0; fi",
        f"echo '{APPLY_FAILED}'",
        "exit 1",
    ]
    return "\n".join(body) + "\n"
