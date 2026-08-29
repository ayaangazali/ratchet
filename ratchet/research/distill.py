"""Turn a paper into a skill, or admit that it does not contain one.

Most papers do not contain a technique you can hand to a coding agent. They contain
a benchmark, a measurement, an architecture, or a result about training. Pretending
otherwise is how you end up with a `skills/` directory full of restated abstracts
that cost tokens in every prompt and change nothing -- so the distiller is given an
explicit way to say no, and is told that saying no is the common case.

What we want is narrow: an *instruction* that changes what the agent does at a
specific moment, phrased so that following it is checkable. "Consider the problem
carefully" is not a skill. "When two attempts from the same state have both been
pruned, change the file you are editing rather than the edit you are making" is.

The output is a `proposed` skill. It reaches no prompt until `trial.py` measures it.
"""

from __future__ import annotations

import re

from .skills import PROPOSED, Skill
from .sources import Paper

NO_TECHNIQUE = "NO-TECHNIQUE"

_FIELD = re.compile(r"^([A-Z][A-Z_]+):\s*(.*)$")

PROMPT = """\
You are reading a research paper to decide whether it contains a technique that
would change how an autonomous coding agent behaves, and if so, to write that
technique down as an instruction the agent can follow.

The agent you are writing for works like this: it is given a repository and a
failing test suite. It proposes a patch, a verifier runs the tests and scores it,
and the agent cannot declare itself finished -- the tests decide. It searches over
repository states in a tree, forks in parallel when it stalls, and prunes branches
that regress.

{paper}

FIRST, decide honestly whether this paper contains an actionable technique for that
agent. Most papers do not. A benchmark, a dataset, a measurement, a model
architecture, or a training method is NOT an actionable technique -- there is
nothing the agent could do differently tomorrow because of it. If that is the case,
reply with exactly:

{no_technique}

and one sentence saying what the paper is about instead. Do not stretch. A wrong
skill is worse than no skill: it costs context in every prompt and moves the agent
in a direction nobody measured.

OTHERWISE, reply in exactly this format, with no preamble and no markdown fences:

NAME: a short kebab-case-free human name, at most six words
KIND: skill
TRIGGERS: a comma-separated subset of always, start, stall, regression, cheat
APPLIES_TO: comma-separated test frameworks this is specific to, or "any"
KEYWORDS: 3-6 comma-separated words describing when this is relevant
BODY:
The instruction itself. Address the agent directly in the second person. Under 120
words. Say what to do and at what moment, not why the paper is interesting. If the
paper gives a concrete threshold, a count, or an ordering, keep the number -- a
number is the difference between an instruction and a sentiment.

Use KIND: system_prompt instead of KIND: skill only if the technique should apply to
every single call rather than at a particular moment.
"""


def _parse(text: str, paper: Paper) -> Skill | None:
    if NO_TECHNIQUE in text[:400].upper():
        return None
    fields: dict[str, str] = {}
    body_lines: list[str] = []
    in_body = False
    for line in text.splitlines():
        if in_body:
            body_lines.append(line)
            continue
        m = _FIELD.match(line.strip())
        if not m:
            continue
        key, value = m.group(1).upper(), m.group(2).strip()
        if key == "BODY":
            in_body = True
            if value:
                body_lines.append(value)
            continue
        fields[key] = value

    body = "\n".join(body_lines).strip()
    name = fields.get("NAME", "").strip().strip(".")
    if not body or not name:
        return None

    def listy(key: str) -> list[str]:
        raw = fields.get(key, "")
        items = [x.strip().lower() for x in re.split(r"[,;]", raw) if x.strip()]
        return [x for x in items if x and x != "any"]

    triggers = [t for t in listy("TRIGGERS")] or ["always"]
    kind = "system_prompt" if fields.get("KIND", "").lower().startswith("system") else "skill"
    return Skill(
        name=name[:60],
        body=body,
        kind=kind,
        source=paper.citation(),
        title=paper.title,
        url=paper.url,
        applies_to=listy("APPLIES_TO"),
        triggers=triggers,
        keywords=listy("KEYWORDS")[:6],
        status=PROPOSED,
    )


class Distiller:
    """One model call per paper, through the harness like everything else."""

    def __init__(self, backend, model: str = "openai/gpt-5-mini") -> None:
        self.backend = backend
        self.model = model
        self.skipped: list[tuple[Paper, str]] = []

    def distill(self, paper: Paper) -> Skill | None:
        prompt = PROMPT.format(paper=paper.brief(), no_technique=NO_TECHNIQUE)
        text, _tokens, _cost = self.backend.complete(
            prompt, model=self.model, role="researcher", max_tokens=900
        )
        skill = _parse(text or "", paper)
        if skill is None:
            reason = (text or "").strip().replace(NO_TECHNIQUE, "").strip().splitlines()
            self.skipped.append((paper, (reason[0] if reason else "no technique found")[:140]))
        return skill

    def distill_all(self, papers: list[Paper]) -> list[Skill]:
        out = []
        for p in papers:
            skill = self.distill(p)
            if skill is not None:
                out.append(skill)
        return out
