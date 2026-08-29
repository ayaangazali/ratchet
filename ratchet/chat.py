"""The chat coder behind the console's input box.

A turn is: prompt -> model -> files and/or a diff -> written into the working
directory -> one git commit. The activity pane shows the ultra-summary (one line
per step), never the raw diff -- and every turn being a commit means every turn is
revertible, which is the only reason letting a model write into your directory is
sane. This is the chat-shaped door into the same idea as the run loop: the model
proposes, something checkable lands, nothing is un-undoable.

Interruption is a threading.Event checked between steps: Esc in the console sets it
and the turn stops at the next boundary, reporting what it had already done.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .context import tree_listing
from .providers import ChatBackend, ChatProviderError

FILE_FENCE = re.compile(r"```file:([^\n`]+)\n(.*?)```", re.S)
DIFF_FENCE = re.compile(r"```(?:diff|patch)\n(.*?)```", re.S)
INTENT_LINE = re.compile(r"^\s*intent\s*:\s*(.+)$", re.I | re.M)

_PROMPT = """You are the coding hand behind ratchet's console. You write code straight into the
user's working directory; every turn becomes one git commit the user can revert.

Working directory listing:
{listing}

{history}User request:
{prompt}

Reply with ONE line starting `intent:` describing the change, then the change itself:
- new or rewritten files as ```file:relative/path fenced blocks (full file contents)
- surgical edits to existing files as a single ```diff fenced unified diff
No prose outside the fences. Never touch .git, .ratchet, or paths outside the
working directory."""


@dataclass
class Turn:
    prompt: str
    intent: str = ""
    files: list[str] = field(default_factory=list)
    commit: str = ""
    error: str = ""
    cancelled: bool = False
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.error and not self.cancelled


class ChatSession:
    def __init__(self, repo: Path, backend: ChatBackend | None = None, bus=None) -> None:
        self.repo = Path(repo).resolve()
        self.backend = backend or ChatBackend.from_env()
        self.bus = bus
        self.cancel = threading.Event()
        self.turns: list[Turn] = []

    # ----------------------------------------------------------------- turn --

    def run_turn(self, prompt: str, emit: Callable[[str, str], None]) -> Turn:
        """One chat turn. `emit(kind, text)` receives the ultra-summary lines the
        activity pane shows; kinds: step, note, done, error."""
        t0 = time.time()
        turn = Turn(prompt=prompt)
        self.cancel.clear()
        self._bus("chat.turn", prompt=prompt[:200], provider=self.backend.provider, model=self.backend.model)

        emit("step", f"asking {self.backend.provider}/{self.backend.model}")
        try:
            reply = self.backend.complete(self._render(prompt))
        except ChatProviderError as e:
            turn.error = str(e)
            emit("error", turn.error)
            return self._finish(turn, t0)
        if self.cancel.is_set():
            turn.cancelled = True
            emit("note", "interrupted before anything was written")
            return self._finish(turn, t0)

        m_intent = INTENT_LINE.search(reply)
        turn.intent = m_intent.group(1).strip()[:120] if m_intent else "unnamed change"
        emit("step", turn.intent)

        files = FILE_FENCE.findall(reply)
        diff = DIFF_FENCE.search(reply)
        if not files and not diff:
            turn.error = "the model returned no file or diff blocks; nothing written"
            emit("error", turn.error)
            return self._finish(turn, t0)

        for raw_path, content in files:
            if self.cancel.is_set():
                turn.cancelled = True
                emit("note", f"interrupted after {len(turn.files)} file(s)")
                return self._commit_and_finish(turn, t0)
            rel = raw_path.strip()
            target = (self.repo / rel).resolve()
            # the model writes only inside the working tree, never into the
            # machinery or the git object store
            if not target.is_relative_to(self.repo) or any(
                part in (".git", ".ratchet") for part in target.relative_to(self.repo).parts
            ):
                emit("note", f"refused path {rel!r}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            turn.files.append(rel)
            emit("step", f"wrote {rel} ({len(content.splitlines())} lines)")

        if diff and not self.cancel.is_set():
            applied = self._apply(diff.group(1))
            emit("step" if applied else "note", "applied diff" if applied else "diff did not apply; skipped")

        return self._commit_and_finish(turn, t0)

    # -------------------------------------------------------------- plumbing --

    def _render(self, prompt: str) -> str:
        history = ""
        if self.turns:
            lines = [f"- {t.intent or t.prompt[:60]} -> {'ok' if t.ok else t.error or 'cancelled'}"
                     for t in self.turns[-3:]]
            history = "Recent turns:\n" + "\n".join(lines) + "\n\n"
        return _PROMPT.format(listing=tree_listing(self.repo, [])[:4000], history=history, prompt=prompt)

    def _apply(self, diff: str) -> bool:
        import tempfile

        fd_path = Path(tempfile.mkstemp(suffix=".diff", prefix="ratchet-chat-")[1])
        fd_path.write_text(diff)
        try:
            for argv in (["git", "apply"], ["git", "apply", "--3way"]):
                if subprocess.run([*argv, str(fd_path)], cwd=self.repo, capture_output=True).returncode == 0:
                    return True
            return False
        finally:
            fd_path.unlink(missing_ok=True)

    def _commit_and_finish(self, turn: Turn, t0: float) -> Turn:
        if turn.files or not turn.error:
            sha = self._commit(turn.intent or "chat turn")
            if sha:
                turn.commit = sha
        self.turns.append(turn)
        return self._finish(turn, t0)

    def _commit(self, intent: str) -> str:
        if subprocess.run(["git", "rev-parse", "--git-dir"], cwd=self.repo, capture_output=True).returncode != 0:
            return ""  # not a repo: files written, nothing to revert to -- said in the summary
        subprocess.run(["git", "add", "-A"], cwd=self.repo, capture_output=True)
        r = subprocess.run(
            ["git", "-c", "user.name=ratchet-chat", "-c", "user.email=chat@ratchet.local",
             "commit", "-q", "-m", f"[ratchet chat] {intent}"],
            cwd=self.repo, capture_output=True,
        )
        if r.returncode != 0:
            return ""
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=self.repo,
                              capture_output=True, text=True).stdout.strip()

    def _finish(self, turn: Turn, t0: float) -> Turn:
        turn.seconds = round(time.time() - t0, 2)
        self._bus("chat.done", intent=turn.intent, files=turn.files, commit=turn.commit,
                  error=turn.error, cancelled=turn.cancelled, seconds=turn.seconds)
        return turn

    def _bus(self, kind: str, **payload) -> None:
        if self.bus is not None:
            self.bus.emit(kind, **payload)
