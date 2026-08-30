"""Reconcile the model routing we ask for with the models the harness actually has.

This module exists because of the single most expensive failure this project had:
the shipped defaults named models (`openai/gpt-5-mini`, `anthropic/claude-sonnet-4-6`,
`google-gemini/gemini-3-pro`) that no harness instance is guaranteed to expose. A
clone with no `.env` therefore died on the very first model call with

    POST /sessions -> 422: Unknown model "..." — provider not configured

and every fallback downstream made that look like the agent had simply decided to do
nothing. Naming a model you hope exists is a configuration bug pretending to be an
agent bug.

So: ask the harness what it has, and route roles onto that. Exact names win when they
are real. When they are not, roles are filled from the live catalog by tier -- a small
model for the cartographer and reviewer, the distinct strong ones for the fan-out --
which is the behaviour that makes `ratchet run` work on a machine it has never seen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..subagents import Roles

#: Substrings that mark a model as the cheap tier. Ordered by how strong a signal
#: each one is, and matched against the model name only -- providers rename models
#: constantly, but they keep calling the small one "mini" or "flash".
_SMALL_HINTS = ("mini", "haiku", "flash", "small", "lite", "nano", "turbo")

_SPLIT = re.compile(r"[^a-z0-9]+")


def _tokens(name: str) -> set[str]:
    """`openai/gpt-5-4-mini` -> {gpt, 5, 4, mini}. Provider is dropped: the same model
    moves between providers and the version digits are what actually identify it."""
    tail = name.split("/", 1)[-1].lower()
    return {t for t in _SPLIT.split(tail) if t}


def _provider(name: str) -> str:
    return name.split("/", 1)[0].lower() if "/" in name else ""


def is_small(name: str) -> bool:
    return any(h in name.split("/", 1)[-1].lower() for h in _SMALL_HINTS)


def similarity(requested: str, candidate: str) -> float:
    """0..1. Jaccard over name tokens, with a bonus for the same provider.

    Deliberately crude. This decides which real model stands in for a name that does
    not exist, and a crude score that is easy to reason about beats a clever one that
    silently routes the fan-out onto three aliases of the same model.
    """
    a, b = _tokens(requested), _tokens(candidate)
    if not a or not b:
        return 0.0
    jaccard = len(a & b) / len(a | b)
    same_provider = 0.15 if _provider(requested) and _provider(requested) == _provider(candidate) else 0.0
    return min(1.0, jaccard + same_provider)


@dataclass
class Resolution:
    """What we asked for, what we got, and whether a human should be told."""

    requested: str
    resolved: str
    exact: bool
    reason: str = ""

    @property
    def substituted(self) -> bool:
        return not self.exact

    def one_line(self) -> str:
        if self.exact:
            return f"{self.resolved}"
        return f"{self.resolved}  (requested {self.requested}; {self.reason})"


class NoModelsAvailable(RuntimeError):
    """The harness has no models at all -- a provider was never configured."""


def pick_small(available: list[str]) -> str:
    """The cheap-tier model, for the cartographer and the reviewer."""
    smalls = [m for m in available if is_small(m)]
    return (smalls or available)[0]


def pick_strong(available: list[str], n: int) -> list[str]:
    """`n` distinct models for the fan-out, strongest tier first.

    Distinctness is the whole point of the fan-out -- three samples from one model are
    three phrasings of one idea. When the catalog genuinely cannot supply `n` distinct
    models we repeat, but the caller is told so it can say as much on screen rather
    than claiming a diversity it does not have.
    """
    strong = [m for m in available if not is_small(m)] or list(available)
    if not strong:
        return []
    out = list(strong[:n])
    while len(out) < n:  # catalog too small: rotate rather than silently shrink
        out.append(strong[len(out) % len(strong)])
    return out


def resolve_one(requested: str, available: list[str], *, prefer_small: bool, threshold: float = 0.34) -> Resolution:
    """Map one requested model name onto something the harness really has."""
    if not available:
        raise NoModelsAvailable("the harness exposes no models")
    if requested in available:
        return Resolution(requested, requested, exact=True)

    scored = sorted(((similarity(requested, m), m) for m in available), reverse=True)
    best_score, best = scored[0]
    if best_score >= threshold:
        return Resolution(requested, best, exact=False, reason=f"closest match, {best_score:.2f}")

    fallback = pick_small(available) if prefer_small else pick_strong(available, 1)[0]
    tier = "cheap tier" if prefer_small else "strong tier"
    return Resolution(requested, fallback, exact=False, reason=f"no match, fell back to the {tier}")


def resolve_roles(roles: Roles, available: list[str]) -> tuple[Roles, list[Resolution]]:
    """Return a copy of `roles` routed onto real models, plus what changed.

    `roles` is a `subagents.Roles`. It is not mutated: the caller keeps the requested
    routing for the receipt, and runs with the resolved one.
    """
    from ..subagents import Roles

    if not available:
        raise NoModelsAvailable(
            "the harness exposes no models -- configure a provider first:\n"
            "    python scripts/setup_harness.py openai --key sk-..."
        )

    notes: list[Resolution] = []

    def one(name: str, *, small: bool) -> str:
        r = resolve_one(name, available, prefer_small=small)
        notes.append(r)
        return r.resolved

    cartographer = one(roles.cartographer, small=True)
    reviewer = one(roles.reviewer, small=True)
    researcher = one(getattr(roles, "researcher", roles.reviewer), small=True)

    # The generators are resolved as a set, not one at a time: resolving them
    # independently is how three requested models collapse onto one real one and the
    # fan-out quietly becomes best-of-1.
    wanted = list(roles.generators) or [pick_strong(available, 1)[0]]
    generators: list[str] = []
    for name in wanted:
        r = resolve_one(name, available, prefer_small=False)
        notes.append(r)
        generators.append(r.resolved)
    if len(set(generators)) < len(generators):
        spread = pick_strong(available, len(wanted))
        for i, m in enumerate(spread):
            generators[i] = m

    return (
        Roles(cartographer=cartographer, reviewer=reviewer, researcher=researcher, generators=generators),
        notes,
    )


def describe(resolutions: list[Resolution]) -> list[str]:
    """Lines for the console. Only substitutions are worth printing -- an exact match
    is the boring case and printing it trains people to ignore the block."""
    return [f"  {r.requested} -> {r.resolved}  ({r.reason})" for r in resolutions if r.substituted]
