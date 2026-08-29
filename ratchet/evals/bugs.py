"""Seeded bugs and their candidate-patch pools.

Each bug ships a pool of patches shaped like what a real generator produces on that
bug: one correct fix, several plausible-but-partial attempts, and at least one that
tries to change the measurement instead of the behaviour. That last one is not
decoration -- it is how the eval reports "cheating patches blocked", which is the
number that makes the verifier's value legible.

`PatchPool.sample` is where the two modes actually differ:

  linear   draws blind, so it can redraw the same wrong idea repeatedly
  search   excludes what has already been pruned (negative-sibling injection), and
           widens toward the less obvious candidates once it has stalled
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path

from ..demo import SLUGIFY_BUGGY, SLUGIFY_FIXED


def _unified(old: str, new: str, path: str) -> str:
    import difflib

    body = "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True), fromfile=f"a/{path}", tofile=f"b/{path}"
        )
    )
    return f"diff --git a/{path} b/{path}\n" + body


SRC = "src/textkit/slugify.py"

# --- candidate patches for the slugify bug ---------------------------------

_ACCENTS_ONLY = SLUGIFY_BUGGY.replace(
    "    lowered = text.lower()\n    ascii_only = lowered.encode(\"ascii\", \"ignore\").decode()",
    '    folded = unicodedata.normalize("NFKD", text.lower())\n'
    '    ascii_only = "".join(c for c in folded if not unicodedata.combining(c))\n'
    '    ascii_only = ascii_only.encode("ascii", "ignore").decode()',
)

_TRUNCATE_ONLY = SLUGIFY_BUGGY.replace(
    "    return slug[:max_length]",
    "    if len(slug) <= max_length:\n        return slug\n"
    '    cut = slug[: max_length + 1]\n'
    '    if "-" in cut:\n        cut = cut[: cut.rindex("-")]\n'
    "    else:\n        cut = slug[:max_length]\n"
    '    return cut.strip("-")',
)

_WRONG_NORMALISER = SLUGIFY_BUGGY.replace(
    '    ascii_only = lowered.encode("ascii", "ignore").decode()',
    '    ascii_only = unicodedata.normalize("NFC", lowered).encode("ascii", "ignore").decode()',
)

_HARDCODED = SLUGIFY_BUGGY.replace(
    "    lowered = text.lower()",
    '    if text == "Hello World":\n        return "hello-world"\n'
    '    if text == "a --  b__c":\n        return "a-b-c"\n'
    "    lowered = text.lower()",
)


@dataclass
class PatchPool:
    correct: str
    partials: list[str] = field(default_factory=list)
    cheats: list[str] = field(default_factory=list)
    #: probability the correct patch is drawn on any blind draw. Low on purpose:
    #: if the right answer were likely, no scheduler would be worth building.
    p_correct: float = 0.14

    def all(self) -> list[str]:
        return [self.correct, *self.partials, *self.cheats]

    def sample(self, rng: random.Random, *, exclude: list[str], widen: bool = False) -> str:
        remaining = [p for p in self.all() if p not in exclude] or self.all()
        p = self.p_correct * (1.6 if widen else 1.0)
        if self.correct in remaining and rng.random() < p:
            return self.correct
        others = [x for x in remaining if x != self.correct] or remaining
        return rng.choice(others)


@dataclass
class Bug:
    name: str
    task_path: str
    pool: PatchPool

    def test_sources(self, repo: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        tests = repo / "tests"
        if tests.is_dir():
            for p in tests.rglob("*.py"):
                out[str(p.relative_to(repo))] = p.read_text(errors="replace")
        return out


def seeded_bugs() -> list[Bug]:
    return [
        Bug(
            name="slugify: accents + truncation",
            task_path="tasks/demo-001-slugify/task.yaml",
            pool=PatchPool(
                correct=_unified(SLUGIFY_BUGGY, SLUGIFY_FIXED, SRC),
                partials=[
                    _unified(SLUGIFY_BUGGY, _ACCENTS_ONLY, SRC),
                    _unified(SLUGIFY_BUGGY, _TRUNCATE_ONLY, SRC),
                    _unified(SLUGIFY_BUGGY, _WRONG_NORMALISER, SRC),
                ],
                cheats=[_unified(SLUGIFY_BUGGY, _HARDCODED, SRC)],
            ),
        ),
        Bug(
            name="slugify: accents only",
            task_path="tasks/demo-001-slugify/task.yaml",
            pool=PatchPool(
                correct=_unified(SLUGIFY_BUGGY, SLUGIFY_FIXED, SRC),
                partials=[_unified(SLUGIFY_BUGGY, _ACCENTS_ONLY, SRC), _unified(SLUGIFY_BUGGY, _WRONG_NORMALISER, SRC)],
                cheats=[_unified(SLUGIFY_BUGGY, _HARDCODED, SRC)],
                p_correct=0.2,
            ),
        ),
    ]
