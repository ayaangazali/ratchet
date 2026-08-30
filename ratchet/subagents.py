"""Three roles, three different jobs, three different models.

  cartographer   cheap model, runs once at startup, produces the repository map that
                 every later prompt reuses. Paying a strong model to re-read the tree
                 on every expansion is the most common way to burn a budget.
  generator      strong model, one per branch during fan-out. Produces a patch and a
                 one-line intent, nothing else.
  reviewer       runs the cheat detector over a candidate before it is graded, and
                 (optionally) a model pass on top. The static rules do the work; the
                 model is there for the cases a regex cannot phrase.

**Fan-out uses different providers on purpose.** Three samples from one model at
temperature 0.8 give you three phrasings of one idea. Three models give you three
ideas. Diversity is structural here, not a sampling artefact -- which is exactly what
the novelty term in the scheduler is trying to reward.

All model calls go through the harness. There is no direct provider SDK in this file
and there should never be one: routing, retries and multi-provider credentials are
what the harness is for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

PATCH_BLOCK = re.compile(r"```(?:diff|patch)\n(.*?)```", re.S)
INTENT_LINE = re.compile(r"^\s*intent\s*:\s*(.+)$", re.I | re.M)


@dataclass
class Candidate:
    patch: str
    intent: str
    model: str
    tokens: int = 0
    cost_usd: float = 0.0

    @property
    def empty(self) -> bool:
        return not self.patch.strip()


@dataclass
class Roles:
    """Model routing by role. Names are TrueForge model FQNs (`provider/model`)."""

    cartographer: str = "openai/gpt-5-mini"
    reviewer: str = "openai/gpt-5-mini"
    generators: list[str] = field(
        default_factory=lambda: [
            "anthropic/claude-sonnet-4-6",
            "openai/gpt-5.2",
            "google-gemini/gemini-3-pro",
        ]
    )

    def generator_for(self, index: int) -> str:
        return self.generators[index % len(self.generators)]


class ModelBackend(Protocol):
    """Whatever can turn a prompt into text. In production this is the harness."""

    def complete(self, prompt: str, *, model: str, role: str, max_tokens: int = 4096) -> tuple[str, int, float]: ...


class Subagents:
    def __init__(self, backend: ModelBackend, roles: Roles | None = None) -> None:
        self.backend = backend
        self.roles = roles or Roles()
        self._map: str | None = None

    # ------------------------------------------------------------ cartographer --

    def map_repo(self, tree_listing: str, task: str) -> str:
        """Run once. The result is reused by every later prompt in the run."""
        if self._map is not None:
            return self._map
        prompt = (
            "You are mapping a repository so another agent can navigate it without reading everything.\n\n"
            f"Task it will work on:\n{task}\n\nFile listing:\n{tree_listing[:8000]}\n\n"
            "Reply with at most 25 lines: the modules that matter for this task, what each is responsible "
            "for, and where the behaviour under test most likely lives. No preamble, no file tree echo."
        )
        text, _tokens, _cost = self.backend.complete(
            prompt, model=self.roles.cartographer, role="cartographer", max_tokens=1200
        )
        self._map = text.strip()
        return self._map

    # --------------------------------------------------------------- generator --

    def generate(self, context_text: str, n: int = 1, *, start: int = 0) -> list[Candidate]:
        """n candidate patches, each from a different provider when n > 1.

        `start` offsets the provider rotation, so a caller retrying one candidate
        at a time (the objective graph) still gets a different prior per retry --
        without it every n=1 call silently used the first provider (found by
        review)."""
        out: list[Candidate] = []
        for i in range(n):
            model = self.roles.generator_for(start + i)
            prompt = context_text
            if n > 1:
                prompt += (
                    f"\n\n# You are branch {i + 1} of {n}\n"
                    "Other branches are exploring this state in parallel with different models. "
                    "Take the approach you think is most likely correct, not the most conservative one; "
                    "the verifier scores every branch and keeps the best."
                )
            text, tokens, cost = self.backend.complete(prompt, model=model, role="generator")
            patch = self._extract_patch(text)
            intent = self._extract_intent(text)
            out.append(Candidate(patch=patch, intent=intent, model=model, tokens=tokens, cost_usd=cost))
        return out

    @staticmethod
    def _extract_patch(text: str) -> str:
        m = PATCH_BLOCK.search(text)
        if m:
            return m.group(1)
        # tolerate a bare diff with no fence
        if text.lstrip().startswith(("diff --git", "--- ")):
            return text
        return ""

    @staticmethod
    def _extract_intent(text: str) -> str:
        m = INTENT_LINE.search(text)
        if m:
            return m.group(1).strip()[:120]
        first = next((line.strip() for line in text.splitlines() if line.strip() and not line.startswith("`")), "")
        return first[:120] or "unnamed attempt"

    # ---------------------------------------------------------------- reviewer --

    def review(self, patch: str, findings_summary: str) -> str:
        """A second opinion on a candidate, cheap model, advisory only.

        Deliberately advisory: the static rules gate, the model comments. A model
        that can veto is a model that can be argued with.
        """
        if not patch.strip():
            return "empty patch"
        prompt = (
            "You are reviewing a candidate patch for signs that it changes the measurement rather than the "
            "behaviour: hardcoded test values, weakened assertions, suppressed checks, mocked clocks or "
            "networks in source, code paths that only trigger under test.\n\n"
            f"Static checks already found: {findings_summary or 'nothing'}\n\n"
            f"```diff\n{patch[:6000]}\n```\n\n"
            "Reply with one line: CLEAN, or SUSPECT plus the specific reason."
        )
        text, _t, _c = self.backend.complete(prompt, model=self.roles.reviewer, role="reviewer", max_tokens=200)
        return text.strip().splitlines()[0][:200] if text.strip() else "no response"


# --------------------------------------------------------------------------- #
# offline backend
# --------------------------------------------------------------------------- #


class ScriptedBackend:
    """A backend that replays canned responses.

    This exists so the search loop, the scheduler and the eval suite can be tested
    end to end with no model, no key and no network -- which is also what makes the
    demo survivable when the venue Wi-Fi does not.
    """

    def __init__(self, responses: list[str], *, cost_per_call: float = 0.0) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []
        self.cost_per_call = cost_per_call

    def complete(self, prompt: str, *, model: str, role: str, max_tokens: int = 4096) -> tuple[str, int, float]:
        self.calls.append((role, model))
        text = self.responses.pop(0) if self.responses else ""
        return text, len(text) // 4, self.cost_per_call
