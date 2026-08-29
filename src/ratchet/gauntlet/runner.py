"""The pawl: run every gate against a candidate tree and return a Verdict.

Execution model
---------------
The graded tree is a git worktree on the host. The agent never has a shell in it.
For each attempt we:

    1. write the proposed diff to a file
    2. apply it inside a throwaway container that mounts the worktree
    3. run the eval script (which reverts protected paths first)
    4. parse -> grade -> patchlint -> score -> decide
    5. commit on green, `git reset --hard <last green>` on red

Container flags are not decoration. `--network=none` during grading is what stops
"curl the answer", exfiltration, and pip-installing a shim mid-run. `--pids-limit`
stops a fork bomb from taking the demo machine with it.

`Backend.LOCAL` exists because a hackathon venue's Wi-Fi and Docker Desktop are
both hostile, and a demo that cannot fall back is a demo that does not happen. It
is explicitly less safe and says so in the UI.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..models import Decision, GateResult, TaskSpec, Verdict
from . import parse
from .eval_script import build_apply_script, build_eval_script
from .grade import grade as grade_fn
from .patchlint import lint, parse_unified_diff
from .score import compute_score, decide


class Backend(str, Enum):
    DOCKER = "docker"
    LOCAL = "local"


DOCKER_FLAGS = [
    "--rm",
    "--network=none",
    "--memory=2g",
    "--memory-swap=2g",
    "--cpus=2",
    "--pids-limit=512",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
    "--ulimit",
    "nofile=4096:4096",
]


@dataclass
class ExecResult:
    code: int
    out: str

    @property
    def ok(self) -> bool:
        return self.code == 0


class Pawl:
    def __init__(
        self,
        *,
        backend: Backend = Backend.DOCKER,
        image: str = "ratchet-task:latest",
        log_dir: Path | None = None,
    ) -> None:
        self.backend = backend
        self.image = image
        self.log_dir = log_dir or Path(".ratchet/logs")
        self.log_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- exec --

    def _run(
        self,
        script: str,
        worktree: Path,
        timeout_s: int,
        *,
        network: bool = False,
        extra_mounts: list[tuple[str, str]] | None = None,
    ) -> ExecResult:
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
            fh.write(script)
            script_path = fh.name
        os.chmod(script_path, 0o755)
        try:
            if self.backend is Backend.DOCKER:
                flags = [f for f in DOCKER_FLAGS if not (network and f == "--network=none")]
                mounts: list[str] = []
                for host, dest in extra_mounts or []:
                    mounts += ["-v", f"{host}:{dest}:ro"]
                cmd = [
                    "docker",
                    "run",
                    *flags,
                    *mounts,
                    "-v",
                    f"{worktree.resolve()}:/work",
                    "-v",
                    f"{script_path}:/ratchet.sh:ro",
                    "-w",
                    "/work",
                    self.image,
                    "/bin/bash",
                    "/ratchet.sh",
                ]
            else:
                cmd = ["/bin/bash", script_path]
            proc = subprocess.run(
                cmd,
                cwd=str(worktree) if self.backend is Backend.LOCAL else None,
                capture_output=True,
                text=True,
                timeout=timeout_s + 30,
            )
            return ExecResult(proc.returncode, (proc.stdout or "") + (proc.stderr or ""))
        except subprocess.TimeoutExpired:
            return ExecResult(124, ">>>>> Tests Timed Out")
        finally:
            os.unlink(script_path)

    # --------------------------------------------------------------- gates --

    def run_gauntlet(
        self,
        *,
        task: TaskSpec,
        worktree: Path,
        base_commit: str,
        diff: str,
        branch: str,
        attempt_id: str | None = None,
        test_sources: dict[str, str] | None = None,
        type_cmd: str | None = "python -m mypy --ignore-missing-imports .",
        lint_cmd: str | None = "python -m ruff check .",
    ) -> Verdict:
        t0 = time.time()
        attempt_id = attempt_id or uuid.uuid4().hex[:8]
        gates: list[GateResult] = []
        repo_dir = "/work" if self.backend is Backend.DOCKER else str(worktree)

        # --- gate 0: integrity, before we run a single line of their code ----
        # This gate is static analysis of the diff text only. Nothing from the
        # patch has executed at this point, and if it fails, nothing ever will.
        g0 = time.time()
        findings = lint(diff, protected_paths=task.protected_paths, test_sources=test_sources)
        crit = [f for f in findings if f.severity.value == "critical"]
        gates.append(
            GateResult(
                "cheat",
                not crit,
                f"{len(findings)} finding(s), {len(crit)} critical",
                time.time() - g0,
                {"findings": [f.to_dict() for f in findings]},
            )
        )
        if crit:
            v = self._verdict(
                attempt_id, task, branch, Decision.DISQUALIFIED, 0.0, gates, findings, None, "", t0, diff
            )
            v.gates.append(
                GateResult("decision", False, f"integrity violation: {crit[0].rule} at {crit[0].path}:{crit[0].line}")
            )
            return v

        # --- gate 1: does the patch even apply -------------------------------
        # The patch file is written OUTSIDE the graded tree on purpose: the apply
        # fallback chain runs `git clean -fd` between attempts, which would delete
        # the patch out from under itself if it lived in the worktree.
        g1 = time.time()
        patch_fd, patch_host = tempfile.mkstemp(suffix=".diff", prefix="ratchet-")
        with os.fdopen(patch_fd, "w") as fh:
            fh.write(diff)
        patch_in_container = "/tmp/ratchet_patch.diff" if self.backend is Backend.DOCKER else patch_host
        apply_res = self._run(
            build_apply_script(repo_dir=repo_dir, patch_path=patch_in_container),
            worktree,
            120,
            extra_mounts=[(patch_host, "/tmp/ratchet_patch.diff")],
        )
        gates.append(GateResult("apply", apply_res.ok, "patch applied" if apply_res.ok else "patch did not apply", time.time() - g1))
        try:
            os.unlink(patch_host)
        except OSError:
            pass
        if not apply_res.ok:
            return self._verdict(
                attempt_id, task, branch, Decision.REJECTED, 0.0, gates, findings, None, parse.tail(apply_res.out), t0, diff
            )

        # --- gate 2: build / import ------------------------------------------
        g2 = time.time()
        build_cmd = task.setup_cmd or "python -c 'import compileall,sys; sys.exit(0 if compileall.compile_dir(\".\", quiet=2) else 1)'"
        build_res = self._run(f"#!/bin/bash\nset -uxo pipefail\ncd {repo_dir}\n{build_cmd}\n", worktree, 180)
        gates.append(GateResult("build", build_res.ok, "ok" if build_res.ok else "build/import failed", time.time() - g2))

        # --- gate 3: the suite -----------------------------------------------
        g3 = time.time()
        script = build_eval_script(
            repo_dir=repo_dir,
            base_commit=base_commit,
            protected_paths=task.protected_paths,
            test_patch_path=None,
            test_cmd=task.test_cmd,
            directives=[],
            timeout_s=task.timeout_s,
        )
        test_res = self._run(script, worktree, task.timeout_s)
        log = test_res.out
        (self.log_dir / f"{attempt_id}.log").write_text(log)

        status_map = parse.parse_pytest_log(log)
        ran = parse.suite_ran(log)
        consistent = parse.exit_code_consistent(log, status_map)
        g = grade_fn(status_map, f2p_visible=task.f2p_visible, f2p_hidden=task.f2p_hidden, p2p=task.p2p)

        gates.append(
            GateResult(
                "f2p",
                g.f2p_visible_rate >= 1.0,
                f"{len(g.f2p_visible['success'])}/{len(task.f2p_visible)} visible",
                time.time() - g3,
                {"failures": g.f2p_visible["failure"]},
            )
        )
        gates.append(
            GateResult(
                "hidden",
                g.f2p_hidden_rate >= 1.0,
                f"{len(g.f2p_hidden['success'])}/{len(task.f2p_hidden)} held-out"
                + ("  <-- fix does not generalise" if g.delta > 0 else ""),
                0.0,
                {"failures": g.f2p_hidden["failure"]},
            )
        )
        gates.append(
            GateResult(
                "p2p",
                g.p2p_rate >= 1.0,
                f"{len(g.p2p['success'])}/{len(task.p2p)} kept green",
                0.0,
                {"failures": g.p2p["failure"]},
            )
        )

        # --- gates 4/5: types and lint ---------------------------------------
        types_ok = lint_ok = True
        if type_cmd:
            r = self._run(f"#!/bin/bash\ncd {repo_dir}\n{type_cmd}\n", worktree, 180)
            types_ok = r.ok
            gates.append(GateResult("types", types_ok, "clean" if types_ok else parse.tail(r.out, 3)))
        if lint_cmd:
            r = self._run(f"#!/bin/bash\ncd {repo_dir}\n{lint_cmd}\n", worktree, 120)
            lint_ok = r.ok
            gates.append(GateResult("lint", lint_ok, "clean" if lint_ok else parse.tail(r.out, 3)))

        # --- decide -----------------------------------------------------------
        files_touched = len({f.path for f in parse_unified_diff(diff)})
        decision, why = decide(
            grade=g,
            findings=findings,
            build_ok=build_res.ok,
            suite_ran=ran,
            exit_consistent=consistent,
            is_canary=task.is_canary,
        )
        sb = compute_score(
            grade=g,
            findings=findings,
            build_ok=build_res.ok,
            types_ok=types_ok,
            lint_ok=lint_ok,
            files_touched=files_touched,
        )
        # a disqualified attempt has no score worth reporting -- showing 0.99 next to
        # DISQUALIFIED reads as a bug even when it is arithmetically true
        total = 0.0 if decision is Decision.DISQUALIFIED else sb.total
        v = self._verdict(attempt_id, task, branch, decision, total, gates, findings, g, parse.tail(log), t0, diff)
        v.stdout_tail = parse.tail(log)
        v.gates.append(GateResult("decision", decision is Decision.ACCEPTED, why))
        return v

    # ------------------------------------------------------------- helpers --

    def _verdict(self, attempt_id, task, branch, decision, score, gates, findings, g, tail_txt, t0, diff) -> Verdict:
        fds = parse_unified_diff(diff)
        return Verdict(
            attempt_id=attempt_id,
            task_id=task.task_id,
            branch=branch,
            decision=decision,
            score=score,
            gates=gates,
            findings=findings,
            f2p_visible_rate=g.f2p_visible_rate if g else 0.0,
            f2p_hidden_rate=g.f2p_hidden_rate if g else 0.0,
            p2p_rate=g.p2p_rate if g else 0.0,
            delta=g.delta if g else 0.0,
            resolution=g.resolution if g else Verdict.__dataclass_fields__["resolution"].default,
            files_touched=len(fds),
            diff_lines=sum(len(f.added) for f in fds),
            stdout_tail=tail_txt,
            duration_s=time.time() - t0,
        )


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False
