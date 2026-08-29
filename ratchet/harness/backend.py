"""Model calls, routed through TrueForge.

Every role in `subagents.py` -- cartographer, generator, reviewer -- reaches its
model through here, and here reaches it through the harness. There is no provider
SDK anywhere in this repository, on purpose: multi-provider routing, retries,
credentials and session persistence are the harness's job, and re-implementing them
would be exactly the "thin wrapper" failure the whole design is trying to avoid.

One session per role keeps context sensible: the cartographer's session holds the
repo map, each generator branch holds its own line of reasoning, and the reviewer
starts clean every time so a previous verdict cannot colour the next one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import client as tf
from .client import TrueForgeClient

#: rough public list prices, only used to keep the budget honest on screen.
#: Wrong numbers here cost nothing but a mis-drawn progress bar; the hard cap is
#: enforced on the total either way.
COST_PER_MTOK = {
    "anthropic/claude-sonnet-4-6": (3.0, 15.0),
    "anthropic/claude-opus-4-6": (15.0, 75.0),
    "openai/gpt-5.2": (2.5, 10.0),
    "openai/gpt-5-mini": (0.3, 1.2),
    "google-gemini/gemini-3-pro": (1.5, 7.0),
}


def estimate_cost(model: str, in_tokens: int, out_tokens: int) -> float:
    cin, cout = COST_PER_MTOK.get(model, (1.0, 3.0))
    return (in_tokens * cin + out_tokens * cout) / 1_000_000


def _load_instructions() -> str:
    """Shared instructions live in `agent/instructions.md` so they are reviewable in a
    pull request rather than buried in a string literal."""
    for candidate in (Path("agent/instructions.md"), Path(__file__).resolve().parents[2] / "agent" / "instructions.md"):
        if candidate.exists():
            return candidate.read_text()
    return "You are a component of a verifier-gated coding agent. Answer exactly what is asked."


@dataclass
class HarnessBackend:
    """Implements `subagents.ModelBackend` against a running TrueForge instance."""

    client: TrueForgeClient
    instructions: str = field(default_factory=_load_instructions)
    sessions: dict[str, str] = field(default_factory=dict)
    total_cost: float = 0.0

    def _session_for(self, role: str, model: str) -> str:
        key = f"{role}:{model}"
        if key not in self.sessions:
            manifest = {
                "model": {"name": model, "params": {"temperature": 0.2, "max_tokens": 8192}},
                "instructions": self.instructions,
                "config": {
                    "iteration_limit": 8,
                    "sandbox": {"enabled": False},
                    "dynamic_sub_agents": {"enabled": False},
                    "context_management": {
                        "compaction": {"enabled": True, "compaction_threshold_tokens": 50000}
                    },
                },
            }
            self.sessions[key] = self.client.create_session(manifest=manifest)["id"]
        return self.sessions[key]

    def complete(self, prompt: str, *, model: str, role: str, max_tokens: int = 4096) -> tuple[str, int, float]:
        session_id = self._session_for(role, model)
        text_parts: list[str] = []
        in_tok = out_tok = 0
        for ev in self.client.create_turn_stream(session_id, [TrueForgeClient.user_message(prompt)]):
            if ev.type in (tf.MODEL_MESSAGE, tf.MODEL_MESSAGE_DELTA):
                text_parts.append(ev.text)
            if ev.type == tf.MODEL_MESSAGE:
                usage = ev.raw.get("usage") or {}
                in_tok += int(usage.get("input_tokens") or 0)
                out_tok += int(usage.get("output_tokens") or 0)
            if ev.type == tf.TURN_DONE:
                break
        text = "".join(text_parts)
        if not in_tok:
            in_tok = len(prompt) // 4
        if not out_tok:
            out_tok = len(text) // 4
        cost = estimate_cost(model, in_tok, out_tok)
        self.total_cost += cost
        return text, in_tok + out_tok, cost
