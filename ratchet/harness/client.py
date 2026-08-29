"""A small Python client for the TrueForge HTTP + SSE API.

TrueForge ships a TypeScript SDK. Ratchet's orchestrator, pawl and TUI are Python,
so this module speaks the wire protocol directly. It is deliberately thin: the
harness runs the loop, this just drives and observes it.

Wire notes that cost time if you learn them the hard way:

* The SSE frames carry no `event:` name. Every frame is `id: <sequence>` plus a
  JSON `data:` payload -- you dispatch on `data["type"]`, not on the event name.
* `id` is a monotonic per-turn sequence number. Persist the last one you saw and
  reconnect with `?after_sequence_number=N` to resume exactly where you dropped.
* A completed turn's live stream is garbage-collected a few minutes after it ends;
  reconnecting after that returns 412. The durable path is `listTurnEvents`, so
  always hydrate from the event list first and only then attach to the stream.
* Approvals are not a callback. The turn *ends* with `state.required_actions`, and
  you resume by starting a new turn whose input items are approval decisions.
  Approval items and user messages may not be mixed in one turn.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx

# ---- event type constants (dispatch on payload["type"]) --------------------
TURN_CREATED = "turn.created"
TURN_DONE = "turn.done"
MODEL_MESSAGE = "model.message"
MODEL_MESSAGE_DELTA = "model.message.delta"
TOOL_RESPONSE = "tool.response"
TOOL_APPROVAL_REQUIRED = "tool.approval_required"
TOOL_RESPONSE_REQUIRED = "tool.response_required"
THREAD_CREATED = "thread.created"
THREAD_DONE = "thread.done"
SANDBOX_CREATED = "sandbox.created"
MCP_AUTH_REQUIRED = "mcp.auth_required"
MCP_INITIALIZE = "mcp.initialize"
AGENT_CONTEXT_OVERWRITE = "agent.context.overwrite"

MAIN_THREAD = "main"


@dataclass
class TurnEvent:
    seq: int
    type: str
    thread_id: str | None
    raw: dict[str, Any]

    @property
    def text(self) -> str:
        c = self.raw.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "".join(p.get("text", "") for p in c if isinstance(p, dict))
        return ""


class TrueForgeError(RuntimeError):
    pass


class TrueForgeClient:
    def __init__(self, base_url: str = "http://localhost:8790", timeout: float = 600.0) -> None:
        self.base = base_url.rstrip("/")
        self._http = httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0))

    # ------------------------------------------------------------- plumbing --

    def _url(self, path: str) -> str:
        return f"{self.base}/api/v1{path}"

    def _json(self, method: str, path: str, **kw) -> Any:
        r = self._http.request(method, self._url(path), **kw)
        if r.status_code >= 400:
            raise TrueForgeError(f"{method} {path} -> {r.status_code}: {r.text[:400]}")
        return r.json() if r.content else None

    # --------------------------------------------------------------- setup --

    def capabilities(self) -> dict:
        return self._json("GET", "/capabilities")

    def models(self) -> list[dict]:
        data = self._json("GET", "/models")
        return data.get("data", data) if isinstance(data, dict) else data

    def create_agent(self, name: str, manifest: dict) -> dict:
        return self._json("POST", "/agents", json={"name": name, "manifest": manifest})

    def create_session(self, *, agent_name: str | None = None, manifest: dict | None = None) -> dict:
        body: dict[str, Any] = {}
        if manifest is not None:
            body["agent"] = {"spec": manifest}
        elif agent_name:
            body["agent"] = {"name": agent_name}
        data = self._json("POST", "/sessions", json=body)
        return data.get("data", data)

    # ---------------------------------------------------------------- turns --

    def create_turn_stream(self, session_id: str, input_items: list[dict]) -> Iterator[TurnEvent]:
        """POST a turn and yield events as they arrive."""
        with self._http.stream(
            "POST",
            self._url(f"/sessions/{session_id}/turns"),
            json={"input": input_items, "stream": True},
            headers={"accept": "text/event-stream"},
        ) as r:
            if r.status_code >= 400:
                raise TrueForgeError(f"turn failed {r.status_code}: {r.read()[:400]!r}")
            yield from _parse_sse(r.iter_lines())

    def subscribe(self, session_id: str, turn_id: str, after: int = 0) -> Iterator[TurnEvent]:
        """Reattach to a live turn after a disconnect. 412 means the stream is gone."""
        with self._http.stream(
            "GET",
            self._url(f"/sessions/{session_id}/turns/{turn_id}/subscribe"),
            params={"after_sequence_number": after},
            headers={"accept": "text/event-stream"},
        ) as r:
            if r.status_code == 412:
                raise TrueForgeError("412: live stream expired; hydrate from list_turn_events instead")
            if r.status_code >= 400:
                raise TrueForgeError(f"subscribe failed {r.status_code}")
            yield from _parse_sse(r.iter_lines())

    def list_turn_events(self, session_id: str, turn_id: str) -> list[TurnEvent]:
        """The durable replay path. Always available, no TTL."""
        data = self._json("GET", f"/sessions/{session_id}/turns/{turn_id}/events")
        items = data.get("data", data) if isinstance(data, dict) else data
        return [TurnEvent(i.get("sequence_number", n), i.get("type", ""), i.get("thread_id"), i) for n, i in enumerate(items or [])]

    def list_session_events(self, session_id: str) -> list[dict]:
        data = self._json("GET", f"/sessions/{session_id}/events")
        return data.get("data", data) if isinstance(data, dict) else data

    def cancel(self, session_id: str) -> None:
        self._json("POST", f"/sessions/{session_id}/cancel")

    # ------------------------------------------------------------ approvals --

    @staticmethod
    def approval_item(thread_id: str, tool_call_id: str, *, allow: bool, reason: str = "") -> dict:
        item: dict[str, Any] = {
            "type": "user.tool_approval",
            "thread_id": thread_id,
            "tool_call_id": tool_call_id,
            "approval": {"status": "allow" if allow else "deny"},
        }
        if not allow and reason:
            item["approval"]["reason"] = reason
        return item

    @staticmethod
    def user_message(text: str) -> dict:
        return {"type": "user.message", "content": text}

    def resume_with_approvals(self, session_id: str, items: list[dict]) -> Iterator[TurnEvent]:
        """Approval items must travel alone -- never mixed with a user message."""
        return self.create_turn_stream(session_id, items)


def _parse_sse(lines) -> Iterator[TurnEvent]:
    seq = 0
    for raw in lines:
        line = raw.decode() if isinstance(raw, bytes) else raw
        if not line:
            continue
        if line.startswith("id:"):
            try:
                seq = int(line[3:].strip())
            except ValueError:
                pass
            continue
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        yield TurnEvent(seq, obj.get("type", ""), obj.get("thread_id"), obj)


def pending_actions(turn_done_event: TurnEvent) -> list[dict]:
    """Pull `required_actions` out of a `turn.done` payload."""
    state = turn_done_event.raw.get("state") or {}
    return state.get("required_actions") or []
