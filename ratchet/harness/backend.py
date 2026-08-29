"""Model calls, routed through TrueForge.

Every role in `subagents.py` -- cartographer, generator, reviewer -- reaches its
model through here, and here reaches it through the harness. There is no provider
SDK anywhere in this repository, on purpose: multi-provider routing, retries,
credentials and session persistence are the harness's job, and re-implementing them
would be exactly the "thin wrapper" failure the whole design is trying to avoid.

One session per role keeps context sensible: the cartographer's session holds the
repo map, each generator branch holds its own line of reasoning, and the reviewer
starts clean every time so a previous verdict cannot colour the next one.

Two rules here were learned the hard way and are worth stating, because breaking
either one turns a configuration bug into a phantom agent bug:

**A failed call raises.** This module used to return `""` when a turn produced no
text -- including when the turn failed outright because the model name did not
exist. An empty completion is indistinguishable from a model that chose to say
nothing, so the search loop counted it as a legitimate step, charged a node, and
spent the entire budget exploring nothing. The run printed progress and finished
clean, which is precisely what "it just does a fake demo" looks like from a
terminal. Silence is now an exception.

**The routing is reconciled against the catalog before it is used.** Asking for a
model the harness does not carry fails with a 422 at session creation, so we ask the
harness what it has and route onto that (see `catalog.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import client as tf
from .client import TrueForgeClient, TrueForgeError

#: Rough public list prices per million tokens, input/output. Only used to keep the
#: budget bar honest on screen; the hard cap is enforced on the total either way.
#: A model absent from this table falls back to `DEFAULT_PRICE`, and `estimate_cost`
#: reports whether the number it returned was a real lookup or that fallback -- an
#: invented price presented as a measurement is how a budget silently stops meaning
#: anything.
COST_PER_MTOK = {
    "anthropic/claude-sonnet-4-6": (3.0, 15.0),
    "anthropic/claude-opus-4-6": (15.0, 75.0),
    "openai/gpt-5.2": (2.5, 10.0),
    "openai/gpt-5-mini": (0.3, 1.2),
    "openai/gpt-5-4-mini": (0.3, 1.2),
    "openai/gpt-5-5": (2.5, 10.0),
    "openai/gpt-5-6-luna": (2.5, 10.0),
    "openai/gpt-5-6-sol": (2.5, 10.0),
    "openai/gpt-5-6-terra": (2.5, 10.0),
    "google-gemini/gemini-3-pro": (1.5, 7.0),
}

DEFAULT_PRICE = (1.0, 3.0)


class ModelCallFailed(RuntimeError):
    """A turn did not produce usable text. Never swallowed, never returned as ''."""


def estimate_cost(model: str, in_tokens: int, out_tokens: int) -> float:
    cin, cout = COST_PER_MTOK.get(model, DEFAULT_PRICE)
    return (in_tokens * cin + out_tokens * cout) / 1_000_000


def price_is_known(model: str) -> bool:
    return model in COST_PER_MTOK


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
    calls: int = 0
    total_tokens: int = 0
    #: filled on first use, so the catalog is fetched once per process
    _available: list[str] | None = None

    # ------------------------------------------------------------- catalog --

    def available_models(self) -> list[str]:
        if self._available is None:
            try:
                raw = self.client.models() or []
            except Exception as e:  # a harness that cannot list models cannot run one
                raise ModelCallFailed(
                    f"cannot list models on {self.client.base}: {e}\n"
                    "  is TrueForge running?  npx @truefoundry/trueforge@latest"
                ) from e
            self._available = [m.get("name") or m.get("model_id") or "" for m in raw if isinstance(m, dict)]
            self._available = [m for m in self._available if m]
        return self._available

    def _resolve_model(self, model: str) -> str:
        """Map a requested name onto one this harness actually serves."""
        from .catalog import NoModelsAvailable, resolve_one

        available = self.available_models()
        if model in available:
            return model
        if not available:
            raise NoModelsAvailable(
                f"the harness at {self.client.base} exposes no models -- configure a provider:\n"
                "    python scripts/setup_harness.py openai --key sk-..."
            )
        from .catalog import is_small

        return resolve_one(model, available, prefer_small=is_small(model)).resolved

    # ------------------------------------------------------------ sessions --

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
            try:
                self.sessions[key] = self.client.create_session(manifest=manifest)["id"]
            except TrueForgeError as e:
                raise ModelCallFailed(
                    f"could not open a session for {model!r} (role {role}): {e}\n"
                    f"  models this harness serves: {', '.join(self.available_models()) or 'none'}"
                ) from e
        return self.sessions[key]

    # --------------------------------------------------------------- turns --

    def complete(self, prompt: str, *, model: str, role: str, max_tokens: int = 4096) -> tuple[str, int, float]:
        model = self._resolve_model(model)
        session_id = self._session_for(role, model)

        deltas: list[str] = []
        final_text: str | None = None
        in_tok = out_tok = 0
        status = ""
        error_detail = ""

        try:
            stream = self.client.create_turn_stream(session_id, [TrueForgeClient.user_message(prompt)])
            for ev in stream:
                # The full message arrives as `model.message` with empty content at the
                # start of the stream and the text follows as deltas, so appending both
                # would be wrong in one direction and dropping both wrong in the other.
                # Deltas are the stream; `turn.done` carries the authoritative final text.
                if ev.type == tf.MODEL_MESSAGE_DELTA:
                    deltas.append(ev.text)
                elif ev.type == tf.MODEL_MESSAGE and ev.text:
                    final_text = ev.text
                elif ev.type == tf.TURN_DONE:
                    state = ev.raw.get("state") or {}
                    status = str(state.get("status") or "")
                    output = state.get("output") or {}
                    if isinstance(output, dict):
                        content = output.get("content")
                        if isinstance(content, str) and content:
                            final_text = content
                        usage = output.get("usage") or {}
                        in_tok = int(usage.get("input_tokens") or 0)
                        out_tok = int(usage.get("output_tokens") or 0)
                    err = state.get("error") or ev.raw.get("error")
                    if err:
                        error_detail = str(err)[:400]
                    break
                elif "error" in ev.type or "failed" in ev.type:
                    error_detail = str(ev.raw)[:400]
        except TrueForgeError as e:
            raise ModelCallFailed(f"turn failed for {model!r} (role {role}): {e}") from e

        text = final_text if final_text is not None else "".join(deltas)

        if error_detail:
            raise ModelCallFailed(f"{model!r} (role {role}) returned an error: {error_detail}")
        if status and status not in ("done", "completed", "succeeded"):
            raise ModelCallFailed(f"{model!r} (role {role}) ended in state {status!r}: {error_detail or 'no detail'}")
        if not text.strip():
            raise ModelCallFailed(
                f"{model!r} (role {role}) produced no text (turn status {status or 'unknown'}). "
                "This is a harness or provider failure, not the agent choosing to stay silent."
            )

        # Real usage when the harness reports it, a character estimate only as a
        # last resort -- and never a number invented for a call that did not happen.
        if not in_tok:
            in_tok = len(prompt) // 4
        if not out_tok:
            out_tok = len(text) // 4

        cost = estimate_cost(model, in_tok, out_tok)
        self.total_cost += cost
        self.calls += 1
        self.total_tokens += in_tok + out_tok
        return text, in_tok + out_tok, cost
