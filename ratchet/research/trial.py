"""Does this skill actually help? Measure it, or refuse to say.

A distilled skill is a claim: "an agent told this will do better than one not told
this." That is exactly the shape of claim this project refuses to take on trust
anywhere else, and there is no reason for prompts to be the exception. So a skill is
adopted by a paired A/B run of the real search -- same task, same repository, same
budget, same seed order, the only difference being whether the skill is in the
prompt.

**The refusal matters as much as the measurement.** `ScriptedBackend` replays canned
responses; it never reads the prompt it is handed. Running an A/B against it produces
two identical numbers and a verdict that means nothing, which is worse than no
verdict because it looks like evidence. So the trial detects that case and declines,
and `status` stays `proposed`.

You cannot A/B a prompt against a generator that does not read prompts. Research mode
says so instead of printing a number.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..models import TaskSpec
from .skills import ADOPTED, PROPOSED, REJECTED, Skill, SkillLibrary, Trial


@dataclass
class Arm:
    """One complete search run, in one condition."""

    green: bool
    nodes: int
    usd: float
    seconds: float
    winner_score: float = 0.0


@dataclass
class TrialOutcome:
    skill: str
    n: int = 0
    baseline: list[Arm] = field(default_factory=list)
    treatment: list[Arm] = field(default_factory=list)
    verdict: str = PROPOSED
    note: str = ""

    @staticmethod
    def _rate(arms: list[Arm]) -> float:
        return statistics.fmean([1.0 if a.green else 0.0 for a in arms]) if arms else 0.0

    @staticmethod
    def _mean_nodes(arms: list[Arm]) -> float:
        solved = [a.nodes for a in arms if a.green]
        return statistics.fmean(solved) if solved else 0.0

    @property
    def baseline_rate(self) -> float:
        return self._rate(self.baseline)

    @property
    def treatment_rate(self) -> float:
        return self._rate(self.treatment)

    @property
    def delta(self) -> float:
        return self.treatment_rate - self.baseline_rate

    def to_trial(self) -> Trial:
        return Trial(
            baseline=round(self.baseline_rate, 3),
            treatment=round(self.treatment_rate, 3),
            trials=self.n,
            verdict=self.verdict,
        )

    def report(self) -> str:
        if not self.baseline and not self.treatment:
            return f"\n  {self.skill}: not measured — {self.note}\n"
        bn, tn = self._mean_nodes(self.baseline), self._mean_nodes(self.treatment)
        cost = sum(a.usd for a in self.baseline + self.treatment)
        lines = [
            "",
            f"skill trial — {self.skill}",
            f"{self.n} paired runs; the only difference is whether the skill is in the prompt.",
            "",
            f"{'arm':<12}{'solved':>10}{'nodes to green':>18}{'cost':>10}",
            "-" * 50,
            f"{'baseline':<12}{self.baseline_rate * 100:>9.0f}%{bn:>18.1f}"
            f"{sum(a.usd for a in self.baseline):>10.2f}",
            f"{'with skill':<12}{self.treatment_rate * 100:>9.0f}%{tn:>18.1f}"
            f"{sum(a.usd for a in self.treatment):>10.2f}",
            "",
            f"delta {self.delta:+.2f} solved · verdict {self.verdict.upper()} · ${cost:.2f} spent",
        ]
        if self.note:
            lines.append(f"  {self.note}")
        return "\n".join(lines)


def can_measure(backend) -> tuple[bool, str]:
    """Is this backend capable of being A/B'd at all?

    A backend that ignores its prompt cannot show a prompt's effect. Checked by
    behaviour rather than by class name where possible, but the scripted backend is
    named too, because that is the one people actually reach for offline.
    """
    if type(backend).__name__ == "ScriptedBackend":
        return False, (
            "the scripted backend replays canned responses and never reads the prompt, "
            "so both arms would be identical by construction. Point --task at a real "
            "run with TrueForge up to measure this."
        )
    if not hasattr(backend, "complete"):
        return False, "backend has no complete()"
    return True, ""


def _one_run(
    *,
    task: TaskSpec,
    repo: Path,
    subagents,
    provider_factory,
    run_id: str,
    library: SkillLibrary | None,
    max_nodes: int,
) -> Arm:
    """A single full search, in one condition. Deliberately reuses `SearchRun`: a
    trial that measured a reimplementation of the loop would measure the wrong
    thing."""
    from ..bus import Bus
    from ..loop import SearchRun
    from ..scheduler import Budget, Scheduler

    t0 = time.time()
    run = SearchRun(
        task=task,
        repo=repo,
        provider=provider_factory(run_id),
        subagents=subagents,
        run_id=run_id,
        scheduler=Scheduler(Budget(max_nodes=max_nodes)),
        bus=Bus(repo / ".ratchet" / f"{run_id}.bus.jsonl"),
        skills=library,
    )
    result = run.run()
    return Arm(
        green=result.green,
        nodes=len(result.tree),
        usd=run.scheduler.budget.usd_used,
        seconds=time.time() - t0,
        winner_score=result.winner.score,
    )


def run_trial(
    skill: Skill,
    *,
    task: TaskSpec,
    repo: Path,
    subagents,
    provider_factory,
    n: int = 3,
    max_nodes: int = 12,
    echo=print,
) -> TrialOutcome:
    """Paired A/B. Returns an outcome whose verdict may legitimately be `proposed`.

    Adoption is deliberately conservative. A skill has to *win*, not merely tie:
    every skill costs context in every prompt it appears in, so break-even is a
    reason to leave it out.
    """
    out = TrialOutcome(skill=skill.name)
    ok, why = can_measure(subagents.backend)
    if not ok:
        out.note = why
        return out

    with_skill = SkillLibrary(repo / "skills")
    forced = Skill(**{**skill.__dict__, "status": ADOPTED})
    with_skill.skills = [forced]

    for i in range(n):
        echo(f"    run {i + 1}/{n}  baseline…")
        out.baseline.append(_one_run(
            task=task, repo=repo, subagents=subagents, provider_factory=provider_factory,
            run_id=f"trial-{skill.slug()}-b{i}", library=None, max_nodes=max_nodes,
        ))
        echo(f"    run {i + 1}/{n}  with skill…")
        out.treatment.append(_one_run(
            task=task, repo=repo, subagents=subagents, provider_factory=provider_factory,
            run_id=f"trial-{skill.slug()}-t{i}", library=with_skill, max_nodes=max_nodes,
        ))
    out.n = n

    if out.treatment_rate > out.baseline_rate:
        out.verdict = ADOPTED
        out.note = "solved more often with the skill in the prompt"
    elif out.treatment_rate == out.baseline_rate and out.baseline_rate > 0:
        bn = TrialOutcome._mean_nodes(out.baseline)
        tn = TrialOutcome._mean_nodes(out.treatment)
        if tn and bn and tn < bn:
            out.verdict = ADOPTED
            out.note = f"same solve rate, {bn - tn:.1f} fewer nodes to green"
        else:
            out.verdict = REJECTED
            out.note = "no measurable improvement; every skill costs context, so break-even is a reject"
    else:
        out.verdict = REJECTED
        out.note = "solved less often with the skill in the prompt"
    return out
