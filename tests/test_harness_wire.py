"""The TrueForge wire protocol, tested from recorded frames — no server, no network.

The live path (`ratchet run` without --scripted) rides on exactly three behaviors:
SSE frames parse by `data["type"]` with no `event:` name, `turn.done` carries the
required actions that end a turn, and `HarnessBackend` assembles a completion from
the stream while keeping one session per role. Each is checked here against canned
frames shaped like the ones in RESEARCH.md, so a wire regression fails on any
machine rather than sixty seconds into a demo.
"""

from __future__ import annotations

import json

from ratchet.harness import client as tf
from ratchet.harness.backend import HarnessBackend
from ratchet.harness.client import TrueForgeClient, TurnEvent, _parse_sse, pending_actions


def _frame(seq: int, payload: dict) -> list[str]:
    return [f"id: {seq}", f"data: {json.dumps(payload)}", ""]


# ------------------------------------------------------------------ _parse_sse --


def test_parse_sse_dispatches_on_payload_type_not_event_name():
    lines = [
        *_frame(1, {"type": "turn.created", "thread_id": "main"}),
        *_frame(2, {"type": "model.message.delta", "thread_id": "main", "content": "hel"}),
        *_frame(3, {"type": "turn.done", "state": {"status": "completed"}}),
    ]
    events = list(_parse_sse(lines))
    assert [e.type for e in events] == ["turn.created", "model.message.delta", "turn.done"]
    assert [e.seq for e in events] == [1, 2, 3]
    assert events[0].thread_id == "main"


def test_parse_sse_survives_noise_bytes_and_done_sentinel():
    lines = [
        b"id: 7",
        b'data: {"type": "model.message", "content": "ok"}',
        b": keep-alive",          # comment frame from the 15s heartbeat
        b"data: [DONE]",          # OpenAI-style sentinel some proxies inject
        b"data: not json at all", # never raises, never yields
        b"id: not-a-number",      # bad id keeps the previous sequence
        b'data: {"type": "turn.done"}',
    ]
    events = list(_parse_sse(lines))
    assert [e.type for e in events] == ["model.message", "turn.done"]
    assert events[0].seq == 7
    assert events[1].seq == 7  # bad id line must not reset the counter


def test_turn_event_text_handles_string_parts_and_absence():
    assert TurnEvent(1, "model.message", None, {"content": "plain"}).text == "plain"
    parts = {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}, "junk"]}
    assert TurnEvent(2, "model.message", None, parts).text == "ab"
    assert TurnEvent(3, "model.message", None, {}).text == ""


# ------------------------------------------------------------- pending_actions --


def test_pending_actions_reads_required_actions_from_turn_done():
    ev = TurnEvent(9, tf.TURN_DONE, None, {
        "type": "turn.done",
        "state": {"status": "awaiting_approval",
                  "required_actions": [{"type": "tool_approval", "tool_call_id": "tc1"}]},
    })
    acts = pending_actions(ev)
    assert acts == [{"type": "tool_approval", "tool_call_id": "tc1"}]
    assert pending_actions(TurnEvent(10, tf.TURN_DONE, None, {"type": "turn.done"})) == []


def test_approval_items_travel_alone_and_carry_deny_reason():
    item = TrueForgeClient.approval_item("main", "tc1", allow=False, reason="not today")
    assert item["type"] == "user.tool_approval"
    assert item["approval"] == {"status": "deny", "reason": "not today"}
    allow = TrueForgeClient.approval_item("main", "tc2", allow=True)
    assert allow["approval"] == {"status": "allow"}  # no empty reason key on allow


# ---------------------------------------------------------------- HarnessBackend --


class _FakeClient:
    """Replays canned turn events; records what the backend asked for."""

    def __init__(self, events):
        self._events = events
        self.sessions_created: list[dict] = []
        self.turns: list[tuple[str, list[dict]]] = []

    def create_session(self, *, manifest=None, **_):
        self.sessions_created.append(manifest)
        return {"id": f"sess-{len(self.sessions_created)}"}

    def create_turn_stream(self, session_id, items):
        self.turns.append((session_id, items))
        yield from self._events


def _stream(*payloads):
    return [TurnEvent(i, p["type"], p.get("thread_id"), p) for i, p in enumerate(payloads, 1)]


def test_backend_assembles_text_from_deltas_and_reads_usage():
    events = _stream(
        {"type": "model.message.delta", "content": "def slug"},
        {"type": "model.message.delta", "content": "ify(): ..."},
        {"type": "model.message", "content": "", "usage": {"input_tokens": 120, "output_tokens": 30}},
        {"type": "turn.done", "state": {"status": "completed"}},
    )
    fake = _FakeClient(events)
    backend = HarnessBackend(fake, instructions="be terse")
    text, tokens, cost = backend.complete("fix slugify", model="openai/gpt-5-mini", role="generator")
    assert text == "def slugify(): ..."
    assert tokens == 150
    assert cost > 0
    assert backend.total_cost == cost
    # one session, created with the role's manifest, and the prompt as a user message
    assert len(fake.sessions_created) == 1
    (session_id, items), = fake.turns
    assert session_id == "sess-1"
    assert items == [{"type": "user.message", "content": "fix slugify"}]


def test_backend_reuses_one_session_per_role_and_model():
    fake = _FakeClient(_stream({"type": "turn.done", "state": {}}))
    backend = HarnessBackend(fake, instructions="x")
    backend.complete("a", model="m", role="reviewer")
    fake._events = _stream({"type": "turn.done", "state": {}})
    backend.complete("b", model="m", role="reviewer")
    assert len(fake.sessions_created) == 1  # same role+model = same session
    fake._events = _stream({"type": "turn.done", "state": {}})
    backend.complete("c", model="m", role="cartographer")
    assert len(fake.sessions_created) == 2  # new role = new session


def test_backend_estimates_tokens_when_usage_is_absent():
    fake = _FakeClient(_stream(
        {"type": "model.message.delta", "content": "x" * 400},
        {"type": "turn.done", "state": {}},
    ))
    backend = HarnessBackend(fake, instructions="x")
    text, tokens, _cost = backend.complete("p" * 80, model="m", role="generator")
    assert len(text) == 400
    assert tokens == (80 // 4) + (400 // 4)  # documented chars/4 fallback
