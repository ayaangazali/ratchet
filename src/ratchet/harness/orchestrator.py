"""Drives a Ratchet run on top of TrueForge and mirrors everything onto the bus.

The division of labour is the point of the project, so it is worth stating plainly:

  TrueForge owns   the agent loop, model calls, context and compaction, MCP tool
                   dispatch, the sandbox, sub-agent threads, session persistence,
                   and the approval interrupt
  Ratchet owns     what counts as progress

This file is the seam. It creates the session, feeds the task in, pumps SSE events
onto the local bus so the TUI can draw them, and turns `turn.done` +
`required_actions` into a human decision and back into a resumed turn.

It does not decide whether the agent's work was any good. Nothing in this file
reads a test result.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..bus import AGENT_TEXT, AGENT_TOOL, APPROVAL_REQUIRED, APPROVAL_RESOLVED, PHASE, RUN_DONE, Bus
from ..config import Settings
from . import client as tf
from .client import TrueForgeClient, TurnEvent

APPROVAL_POLL_S = 0.4
APPROVAL_TIMEOUT_S = 900


class Orchestrator:
    def __init__(self, settings: Settings, bus: Bus, run_id: str) -> None:
        self.s = settings
        self.bus = bus
        self.run_id = run_id
        self.client = TrueForgeClient(settings.trueforge_base_url)
        self.session_id: str | None = None
        self.turn_id: str | None = None
        self.last_seq = 0
        self.approvals_dir = Path(settings.repo_path) / ".ratchet" / "approvals"
        self.approvals_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- session --

    def manifest(self) -> dict[str, Any]:
        spec = json.loads(Path(self.s.agent_spec_path).read_text())
        spec.setdefault("model", {})["name"] = self.s.model
        for srv in spec.get("mcp_servers", []):
            if srv.get("name") == "ratchet":
                srv["url"] = f"http://{self.s.mcp_host}:{self.s.mcp_port}/mcp"
        return spec

    def start(self, first_message: str) -> None:
        session = self.client.create_session(manifest=self.manifest())
        self.session_id = session["id"]
        self.bus.emit(PHASE, phase="working", session_id=self.session_id)
        self._pump(self.client.create_turn_stream(self.session_id, [tf.TrueForgeClient.user_message(first_message)]))

    def resume(self) -> None:
        """Reattach after a disconnect. Hydrate durably first, then go live."""
        if not (self.session_id and self.turn_id):
            return
        for ev in self.client.list_turn_events(self.session_id, self.turn_id):
            self._mirror(ev)
            self.last_seq = max(self.last_seq, ev.seq)
        try:
            self._pump(self.client.subscribe(self.session_id, self.turn_id, after=self.last_seq))
        except tf.TrueForgeError as e:
            self.bus.emit(PHASE, phase="idle", note=str(e))

    # ---------------------------------------------------------------- pump --

    def _pump(self, events: Iterable[TurnEvent]) -> None:
        for ev in events:
            self.last_seq = max(self.last_seq, ev.seq)
            self._mirror(ev)
            if ev.type == tf.TURN_CREATED:
                self.turn_id = ev.raw.get("turn_id") or ev.raw.get("id")
            elif ev.type == tf.TURN_DONE:
                actions = tf.pending_actions(ev)
                if actions:
                    self._handle_required_actions(actions)
                else:
                    self.bus.emit(RUN_DONE, status=(ev.raw.get("state") or {}).get("status"))
                return

    def _mirror(self, ev: TurnEvent) -> None:
        """Everything the harness emits becomes a bus event the TUI can draw."""
        thread = ev.thread_id or tf.MAIN_THREAD
        if ev.type in (tf.MODEL_MESSAGE, tf.MODEL_MESSAGE_DELTA):
            text = ev.text
            if text:
                self.bus.emit(AGENT_TEXT, thread=thread, text=text, delta=ev.type.endswith("delta"))
        elif ev.type == tf.TOOL_RESPONSE:
            self.bus.emit(
                AGENT_TOOL,
                thread=thread,
                tool=ev.raw.get("tool_name") or ev.raw.get("name"),
                ok=not ev.raw.get("is_error"),
                preview=str(ev.raw.get("content"))[:600],
            )
        elif ev.type == tf.THREAD_CREATED:
            self.bus.emit("thread.created", thread=thread, title=ev.raw.get("title"), parent=ev.raw.get("parent_thread_id"))
        elif ev.type == tf.THREAD_DONE:
            self.bus.emit("thread.done", thread=thread, state=(ev.raw.get("state") or {}).get("status"))
        elif ev.type == tf.SANDBOX_CREATED:
            self.bus.emit("sandbox.created", provider=ev.raw.get("provider"))
        elif ev.type == tf.MCP_AUTH_REQUIRED:
            self.bus.emit("mcp.auth_required", servers=ev.raw.get("mcp_servers"))
        elif ev.type == tf.AGENT_CONTEXT_OVERWRITE:
            self.bus.emit("context.compacted", reason=ev.raw.get("reason"))

    # ----------------------------------------------------------- approvals --

    def _handle_required_actions(self, actions: list[dict]) -> None:
        items: list[dict] = []
        for action in actions:
            if action.get("type") != tf.TOOL_APPROVAL_REQUIRED:
                continue
            thread_id = action.get("thread_id") or tf.MAIN_THREAD
            for call in action.get("tool_calls", []):
                call_id = call.get("id")
                self.bus.emit(
                    APPROVAL_REQUIRED,
                    tool_call_id=call_id,
                    thread=thread_id,
                    tool=call.get("name") or call.get("tool_name"),
                    arguments=call.get("arguments"),
                )
                decision = self._await_decision(call_id)
                self.bus.emit(APPROVAL_RESOLVED, tool_call_id=call_id, approved=decision["allow"], reason=decision.get("reason", ""))
                items.append(
                    tf.TrueForgeClient.approval_item(
                        thread_id, call_id, allow=decision["allow"], reason=decision.get("reason", "")
                    )
                )
        if items and self.session_id:
            self._pump(self.client.resume_with_approvals(self.session_id, items))

    def _await_decision(self, call_id: str) -> dict:
        """Block until the TUI (or a human with a text editor) drops a decision file.

        A file rather than a socket: if the TUI crashes mid-demo you can still
        approve the push with `echo '{"allow": true}' > .ratchet/approvals/<id>.json`,
        and that has saved more than one demo.
        """
        path = self.approvals_dir / f"{call_id}.json"
        deadline = time.time() + APPROVAL_TIMEOUT_S
        while time.time() < deadline:
            if path.exists():
                try:
                    return json.loads(path.read_text())
                except json.JSONDecodeError:
                    pass
            time.sleep(APPROVAL_POLL_S)
        return {"allow": False, "reason": "no human responded within the approval window"}

    @staticmethod
    def decide(repo: Path, call_id: str, allow: bool, reason: str = "") -> None:
        d = repo / ".ratchet" / "approvals"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{call_id}.json").write_text(json.dumps({"allow": allow, "reason": reason}))
