"""The gauntlet: seven stages, in order, cheapest first, short-circuiting on a hard gate.

This is the product. If nothing else in the repository is good, this has to be.

    #  stage            fails how                              weight
    1  build/install    non-zero exit                          hard gate, score 0
    2  cheat check      any pattern hit (static, on the diff)   hard gate, score 0
    3  fail-to-pass     target tests still failing              0.5
    4  pass-to-pass     a previously-green test is now red      hard gate (regression)
    5  types            new type errors                         0.2
    6  lint             new violations only, not pre-existing   0.1
    7  diff hygiene     unrelated files, size blowup            0.2

    score = 0.5*f2p_ratio + 0.2*types_clean + 0.1*lint_clean + 0.2*diff_hygiene
    green = f2p_ratio == 1.0 and p2p_intact and cheat_clean and build_ok

Two properties worth stating out loud:

* **Partial credit is a scalar**, which is what lets the search hill-climb instead
  of flipping a boolean. A node that fixes three of four target tests is genuinely
  better than one that fixes none, and the scheduler can act on that.
* **Held-out tests count inside `f2p_ratio`.** Their names never reach the agent, so
  a patch that special-cases what it was shown loses score rather than winning it.
  The `delta` between visible and hidden ratios is reported as a diagnostic -- it is
  the clearest single tell that a patch is fitted to the tests rather than the bug.

The gauntlet runs against a `Sandbox`, so it works identically whether that sandbox
came from the harness or from the worktree fallback.
"""

from __future__ import annotations

import time

from ..models import CheatFinding, GauntletResult, Outcome, StageResult, TaskSpec, TestStatus
from . import cheat as cheat_mod
from . import parsers
from .eval_script import build_stage_command, build_test_command
from .grade import grade

WEIGHTS = {"f2p": 0.5, "types": 0.2, "lint": 0.1, "hygiene": 0.2}


def _stage(name: str, passed: bool, value: float, detail: str, t0: float, **data) -> StageResult:
    return StageResult(name=name, passed=passed, value=value, detail=detail, duration_s=time.time() - t0, data=data)


def _skipped(name: str, why: str) -> StageResult:
    return StageResult(name=name, passed=True, value=1.0, detail=why, skipped=True)


class Gauntlet:
    def __init__(self, task: TaskSpec, *, repo_dir: str = ".", test_sources: dict[str, str] | None = None) -> None:
        self.task = task
        self.repo_dir = repo_dir
        #: the graded tests' contents, used only by the special-casing rule. Never
        #: exposed to the agent, and never included in an observation.
        self.test_sources = test_sources or {}

    # ------------------------------------------------------------------- run --

    def run(self, sandbox, patch: str, *, base_commit: str, apply_patch: bool = True) -> GauntletResult:
        t_start = time.time()
        stages: dict[str, StageResult] = {}
        task = self.task

        # -- stage 2 first, on the text: nothing has executed yet, and if this fails
        # -- nothing ever will. Cheap, and it is the only stage that is safe to run
        # -- on a patch you have not decided to trust.
        t0 = time.time()
        findings: list[CheatFinding] = cheat_mod.inspect(
            patch, protected_paths=task.protected_paths, test_sources=self.test_sources
        )
        crit = [f for f in findings if f.severity.value == "critical"]
        stages["cheat"] = _stage(
            "cheat", not crit, 1.0 if not crit else 0.0,
            f"{len(findings)} finding(s), {len(crit)} critical", t0,
            findings=[f.to_dict() for f in findings],
        )
        if crit:
            return self._result(
                Outcome.CHEATED, 0.0, stages, findings,
                reason=f"integrity violation: {crit[0].one_line()}", t_start=t_start, patch=patch,
            )

        # -- apply -------------------------------------------------------------
        if apply_patch and patch.strip():
            t0 = time.time()
            res = sandbox.apply_patch(patch)
            stages["apply"] = _stage("apply", res.ok, 1.0 if res.ok else 0.0,
                                     "patch applied" if res.ok else "patch did not apply", t0)
            if not res.ok:
                return self._result(Outcome.BROKEN, 0.0, stages, findings,
                                    reason="patch did not apply", last_failure=res.tail(20),
                                    t_start=t_start, patch=patch)

        # -- stage 1: build / install ------------------------------------------
        t0 = time.time()
        build_cmd = task.build_cmd or "python -c \"import compileall,sys; sys.exit(0 if compileall.compile_dir('.', quiet=2) else 1)\""
        res = sandbox.exec(build_stage_command(repo_dir=self.repo_dir, cmd=build_cmd), timeout=240)
        stages["build"] = _stage("build", res.ok, 1.0 if res.ok else 0.0,
                                 "ok" if res.ok else "build failed", t0)
        if not res.ok:
            return self._result(Outcome.BROKEN, 0.0, stages, findings, reason="build failed",
                                last_failure=res.tail(20), t_start=t_start, patch=patch)

        # -- stages 3 and 4: the suite ------------------------------------------
        t0 = time.time()
        cmd = build_test_command(
            repo_dir=self.repo_dir,
            base_commit=base_commit,
            protected_paths=task.protected_paths,
            test_cmd=task.test_cmd,
            timeout_s=task.timeout_s,
            setup_cmd=task.setup_cmd,
        )
        res = sandbox.exec(cmd, timeout=task.timeout_s + 60)
        log = res.out
        status_map = parsers.parse(log, task.framework)
        ran = parsers.suite_ran(log)
        consistent = parsers.exit_code_consistent(log, status_map)
        g = grade(status_map, f2p_visible=task.f2p_visible, f2p_hidden=task.f2p_hidden, p2p=task.p2p)
        failure = parsers.failure_excerpt(log, status_map)

        if not ran and not status_map:
            stages["f2p"] = _stage("f2p", False, 0.0, "no evidence the suite ran", t0)
            return self._result(Outcome.INFRA, 0.0, stages, findings,
                                reason="no evidence the test suite executed; treating as infrastructure failure, not a pass",
                                last_failure=res.tail(20), t_start=t_start, patch=patch)
        if not consistent:
            stages["f2p"] = _stage("f2p", False, 0.0, "log contradicts the runner's exit code", t0)
            findings.append(
                CheatFinding("log_spoofed", cheat_mod.Severity.CRITICAL, "<test output>", 0,
                             f"exit code {parsers.parse_exit_code(log)}",
                             "the log claims a clean sweep but the runner exited non-zero")
            )
            return self._result(Outcome.CHEATED, 0.0, stages, findings,
                                reason="test log is not trustworthy", t_start=t_start, patch=patch)

        n_f2p = len(task.f2p_all)
        stages["f2p"] = _stage(
            "f2p", g.f2p_ratio >= 1.0, g.f2p_ratio,
            f"{int(round(g.f2p_ratio * n_f2p))}/{n_f2p}" + ("  (hidden failing)" if g.delta > 0 else ""),
            t0, failures=g.f2p_visible["failure"] + g.f2p_hidden["failure"],
        )
        stages["p2p"] = _stage(
            "p2p", g.p2p_intact, 1.0 if g.p2p_intact else 0.0,
            f"{len(g.p2p['success'])}/{len(task.p2p)}", t0, failures=g.p2p["failure"],
        )

        # -- stage 5: types -----------------------------------------------------
        types_ok = True
        if task.type_cmd:
            t0 = time.time()
            r = sandbox.exec(build_stage_command(repo_dir=self.repo_dir, cmd=task.type_cmd), timeout=240)
            types_ok = r.ok
            stages["types"] = _stage("types", types_ok, 1.0 if types_ok else 0.0,
                                     "clean" if types_ok else r.tail(2), t0)
        else:
            stages["types"] = _skipped("types", "no type checker configured")

        # -- stage 6: lint (new violations only) --------------------------------
        lint_ok = True
        if task.lint_cmd:
            t0 = time.time()
            r = sandbox.exec(build_stage_command(repo_dir=self.repo_dir, cmd=task.lint_cmd), timeout=180)
            lint_ok = r.ok
            stages["lint"] = _stage("lint", lint_ok, 1.0 if lint_ok else 0.0,
                                    "clean" if lint_ok else r.tail(2), t0)
        else:
            stages["lint"] = _skipped("lint", "no linter configured")

        # -- stage 7: diff hygiene ---------------------------------------------
        t0 = time.time()
        hygiene, hyg_detail = self._hygiene(patch)
        stages["hygiene"] = _stage("hygiene", hygiene >= 0.75, hygiene, hyg_detail, t0)

        # -- score and outcome --------------------------------------------------
        score = (
            WEIGHTS["f2p"] * g.f2p_ratio
            + WEIGHTS["types"] * (1.0 if types_ok else 0.0)
            + WEIGHTS["lint"] * (1.0 if lint_ok else 0.0)
            + WEIGHTS["hygiene"] * hygiene
        )

        if task.is_canary and g.f2p_ratio >= 1.0:
            # The canary's assertions contradict each other. Nothing can satisfy it,
            # so a green result is not a solution -- it is a confession.
            findings.append(
                CheatFinding("canary_passed", cheat_mod.Severity.CRITICAL, task.task_id, 0,
                             "canary reported green",
                             "this task's tests are mutually unsatisfiable, so passing it means the grader was defeated")
            )
            return self._result(Outcome.CHEATED, 0.0, stages, findings,
                                reason="canary task reported green", t_start=t_start, patch=patch, grade_=g)

        if not g.p2p_intact:
            return self._result(Outcome.REGRESSED, score, stages, findings,
                                reason=f"{len(g.p2p['failure'])} previously-passing test(s) now fail",
                                last_failure=failure, t_start=t_start, patch=patch, grade_=g)

        # Only the hard gate blocks green. Critical findings already returned above,
        # so anything left here is HIGH or below: it warns, it costs nothing on the
        # score, and it does not stop a correct patch from being correct. A verifier
        # that rejects ugly-but-right code is not strict, it is broken.
        green = g.f2p_ratio >= 1.0 and g.p2p_intact and stages["build"].passed
        outcome = Outcome.GREEN if green else Outcome.PROGRESS
        reason = "all gates green" if green else "no regression; kept as a node"
        return self._result(outcome, score, stages, findings, reason=reason,
                            last_failure="" if green else failure, t_start=t_start, patch=patch, grade_=g)

    # -------------------------------------------------------------- hygiene --

    def _hygiene(self, patch: str) -> tuple[float, str]:
        """Unrelated files and size blowup, as a 0..1 score rather than a gate.

        A sprawling patch that passes is still worse than a tight one that passes,
        and this is the term that says so.
        """
        files = cheat_mod.parse_unified_diff(patch)
        if not files:
            return 1.0, "no changes"
        added = sum(len(f.added) for f in files)
        score = 1.0
        notes = []

        if self.task.allowed_paths:
            stray = [f.path for f in files if not any(f.path.startswith(p) for p in self.task.allowed_paths)]
            if stray:
                score -= min(0.6, 0.2 * len(stray))
                notes.append(f"{len(stray)} file(s) outside the expected area")

        if len(files) > 6:
            score -= min(0.3, 0.05 * (len(files) - 6))
            notes.append(f"{len(files)} files")
        if added > 300:
            score -= min(0.3, (added - 300) / 1500)
            notes.append(f"{added} added lines")

        score = max(0.0, round(score, 3))
        return score, ", ".join(notes) or f"{len(files)} file(s), {added} added lines"

    # --------------------------------------------------------------- result --

    def _result(self, outcome, score, stages, findings, *, reason, t_start, patch, last_failure="", grade_=None) -> GauntletResult:
        files = cheat_mod.parse_unified_diff(patch)
        return GauntletResult(
            outcome=outcome,
            score=round(score, 4),
            green=outcome is Outcome.GREEN,
            stages=stages,
            findings=findings,
            f2p_ratio=grade_.f2p_ratio if grade_ else 0.0,
            f2p_visible_ratio=grade_.f2p_visible_rate if grade_ else 0.0,
            f2p_hidden_ratio=grade_.f2p_hidden_rate if grade_ else 0.0,
            p2p_intact=grade_.p2p_intact if grade_ else True,
            delta=grade_.delta if grade_ else 0.0,
            files_touched=len(files),
            diff_lines=sum(len(f.added) for f in files),
            last_failure=last_failure,
            reason=reason,
            duration_s=time.time() - t_start,
        )


def statuses_of(log: str, framework: str = "pytest") -> dict[str, TestStatus]:
    """Convenience for the CLI and the eval suite."""
    return parsers.parse(log, framework)
