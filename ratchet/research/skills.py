"""A skill: one technique, lifted from a paper, in a form a prompt can carry.

A skill file is markdown with YAML front matter, living in `skills/` in the
repository. That placement is the point, and it is the same argument `scrapers.yaml`
makes: a technique that only exists inside a model's context window cannot be
reviewed, diffed, blamed or reverted. One that sits in git can.

    ---
    name: negative-sibling-injection
    kind: skill                     # skill | system_prompt
    source: arXiv:2410.20285
    title: SWE-Search -- Monte Carlo Tree Search for software agents
    applies_to: [pytest]            # frameworks, empty means any
    triggers: [stall]               # stall | regression | cheat | always
    status: adopted                 # proposed | adopted | rejected
    trial:
      baseline: 0.58
      treatment: 0.83
      trials: 6
      verdict: adopted
    ---
    When two attempts from the same state have both been pruned, ...

`status` is the load-bearing field. A freshly distilled skill is `proposed` and is
**not** injected into any prompt. It becomes `adopted` only by winning a measured
trial, and `rejected` skills stay in the repository on purpose -- a technique that
sounded good and did not work is a more useful record than silence, and it stops the
next person re-reading the same paper and proposing the same thing.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

PROPOSED, ADOPTED, REJECTED = "proposed", "adopted", "rejected"
TRIGGERS = ("always", "stall", "regression", "cheat", "start")

#: Prompts are budgeted. Two skills is a technique; six is a manifesto nobody reads,
#: and every character spent here is a character not spent on the failure output.
MAX_SKILLS_IN_PROMPT = 3
MAX_SKILL_CHARS = 900

_FRONT = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)


@dataclass
class Trial:
    baseline: float = 0.0
    treatment: float = 0.0
    trials: int = 0
    verdict: str = ""

    @property
    def delta(self) -> float:
        return self.treatment - self.baseline

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Skill:
    name: str
    body: str
    kind: str = "skill"  # skill | system_prompt
    source: str = ""  # "arXiv:2510.20270"
    title: str = ""
    url: str = ""
    applies_to: list[str] = field(default_factory=list)
    triggers: list[str] = field(default_factory=lambda: ["always"])
    keywords: list[str] = field(default_factory=list)
    status: str = PROPOSED
    trial: Trial = field(default_factory=Trial)

    # ------------------------------------------------------------------ views --

    @property
    def adopted(self) -> bool:
        return self.status == ADOPTED

    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-") or "skill"

    def render(self) -> str:
        """What actually reaches the prompt. The citation rides along so a reader of
        the transcript can go and check whether the paper says what we claim."""
        head = f"## {self.name}"
        if self.source:
            head += f"   ({self.source})"
        return f"{head}\n{self.body.strip()[:MAX_SKILL_CHARS]}"

    def one_line(self) -> str:
        mark = {ADOPTED: "✓", REJECTED: "✗", PROPOSED: "·"}.get(self.status, "·")
        d = f"{self.trial.delta:+.2f}" if self.trial.trials else "  —  "
        return f" {mark} {self.name:<34} {self.kind:<14} {d}  {self.source}"

    # ------------------------------------------------------------ persistence --

    def to_markdown(self) -> str:
        meta: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "source": self.source,
            "title": self.title,
            "url": self.url,
            "applies_to": self.applies_to,
            "triggers": self.triggers,
            "keywords": self.keywords,
            "status": self.status,
        }
        if self.trial.trials:
            meta["trial"] = self.trial.to_dict()
        if yaml is None:  # pragma: no cover - pyyaml is a hard dependency
            raise RuntimeError("pip install pyyaml")
        front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
        return f"---\n{front}\n---\n{self.body.strip()}\n"

    @classmethod
    def from_markdown(cls, text: str, *, name_hint: str = "") -> Skill:
        m = _FRONT.match(text)
        if not m:
            return cls(name=name_hint or "unnamed", body=text.strip())
        if yaml is None:  # pragma: no cover
            raise RuntimeError("pip install pyyaml")
        meta = yaml.safe_load(m.group(1)) or {}
        trial = Trial(**{k: v for k, v in (meta.get("trial") or {}).items() if k in Trial.__dataclass_fields__})
        known = {f for f in cls.__dataclass_fields__} - {"body", "trial"}
        return cls(
            body=m.group(2).strip(),
            trial=trial,
            **{k: v for k, v in meta.items() if k in known and v is not None},
        )


class SkillLibrary:
    """Every skill in `skills/`, and the rules for which ones reach a prompt."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.skills: list[Skill] = []

    @classmethod
    def load(cls, root: Path) -> SkillLibrary:
        lib = cls(root)
        if not lib.root.is_dir():
            return lib
        for path in sorted(lib.root.glob("*.md")):
            try:
                lib.skills.append(Skill.from_markdown(path.read_text(), name_hint=path.stem))
            except Exception:
                continue  # a malformed skill file must not take a run down with it
        return lib

    def save(self, skill: Skill) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{skill.slug()}.md"
        path.write_text(skill.to_markdown())
        self.skills = [s for s in self.skills if s.slug() != skill.slug()] + [skill]
        return path

    def get(self, name: str) -> Skill | None:
        want = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        return next((s for s in self.skills if s.slug() == want), None)

    def __len__(self) -> int:
        return len(self.skills)

    def __iter__(self):
        return iter(self.skills)

    # -------------------------------------------------------------- selection --

    def select(
        self,
        *,
        framework: str = "",
        trigger: str = "always",
        task_text: str = "",
        kind: str = "skill",
        limit: int = MAX_SKILLS_IN_PROMPT,
        include_proposed: bool = False,
    ) -> list[Skill]:
        """The skills that belong in *this* prompt, best first.

        Only adopted skills are injected unless a caller explicitly asks otherwise,
        and the one caller that does is the trial harness -- which is the only place
        an unproven skill should ever reach a model.
        """
        terms = {w for w in re.findall(r"[a-z]{4,}", task_text.lower())}
        pairs: list[tuple[tuple[float, int, str], Skill]] = []
        for s in self.skills:
            if s.kind != kind:
                continue
            if not (s.adopted or (include_proposed and s.status != REJECTED)):
                continue
            if s.applies_to and framework and framework not in s.applies_to:
                continue
            if trigger not in s.triggers and "always" not in s.triggers:
                continue
            hits = sum(1 for k in s.keywords if k.lower() in terms) if s.keywords else 0
            # measured improvement first, then relevance to this task, then the name
            # as a stable tiebreak -- the same run twice must send the same prompt.
            pairs.append(((-s.trial.delta, -hits, s.name), s))
        pairs.sort(key=lambda pair: pair[0])
        return [skill for _key, skill in pairs[:limit]]

    def system_prompt(self, *, framework: str = "", task_text: str = "") -> str:
        """The adopted `system_prompt` skills, concatenated. Injected once per run."""
        chosen = self.select(framework=framework, task_text=task_text, kind="system_prompt", limit=4)
        return "\n\n".join(s.render() for s in chosen)
