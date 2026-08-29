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

{sources}{history}User request:
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
    commit_note: str = ""
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

        # The gauntlet's static stage, before a byte lands: the same cheat detector
        # the run loop uses inspects what the model wants to write. sys.exit(0) in
        # generated source, report-hook tampering, writes into graded paths -- the
        # chat door is not a way around the verifier.
        from .verifier import cheat as cheat_mod

        synth = self._as_diff(files, diff.group(1) if diff else "")
        findings = cheat_mod.inspect(synth, protected_paths=list(cheat_mod.DEFAULT_PROTECTED))
        crit = [f for f in findings if f.severity.value == "critical"]
        if crit:
            turn.error = f"gauntlet blocked the turn: {crit[0].one_line()}"
            emit("error", turn.error)
            return self._finish(turn, t0)
        emit("step", "gauntlet cheat check: clean" if not findings
             else f"gauntlet cheat check: {len(findings)} warning(s), none blocking")

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
            if applied:
                from .verifier.cheat import parse_unified_diff

                turn.files.extend(f.path for f in parse_unified_diff(diff.group(1)) if f.path)
                emit("step", "applied diff")
            else:
                emit("note", "diff did not apply; skipped")

        return self._commit_and_finish(turn, t0)

    # -------------------------------------------------------------- plumbing --

    def _render(self, prompt: str) -> str:
        history = ""
        if self.turns:
            lines = [f"- {t.intent or t.prompt[:60]} -> {'ok' if t.ok else t.error or 'cancelled'}"
                     for t in self.turns[-3:]]
            history = "Recent turns:\n" + "\n".join(lines) + "\n\n"
        return _PROMPT.format(
            listing=tree_listing(self.repo, [])[:4000],
            sources=self._sources_block(),
            history=history,
            prompt=prompt,
        )

    def _sources_block(self, *, max_files: int = 8, max_chars: int = 10_000) -> str:
        """The current contents of the repo's small text files, so an edit request
        edits what is actually there instead of hallucinating it. Capped hard --
        context is the scarcest resource, and a lockfile is not worth shipping."""
        keep = (".html", ".css", ".js", ".ts", ".py", ".md", ".json", ".yaml", ".yml", ".toml")
        parts: list[str] = []
        used = 0
        for path in sorted(self.repo.rglob("*")):
            if len(parts) >= max_files or used >= max_chars:
                break
            if not path.is_file() or path.suffix not in keep:
                continue
            rel = path.relative_to(self.repo)
            if any(seg in (".git", ".ratchet", "node_modules", ".venv") for seg in rel.parts):
                continue
            text = path.read_text(errors="replace")
            if len(text) > 4000:
                continue  # big files are named in the listing; the model can ask
            parts.append(f"--- {rel} ---\n{text}")
            used += len(text)
        if not parts:
            return ""
        return "Current file contents (edit these, do not reinvent them):\n" + "\n".join(parts) + "\n\n"

    def _as_diff(self, files: list[tuple[str, str]], diff_text: str) -> str:
        """What the model wants to write, as one unified diff the cheat detector
        can inspect -- new files synthesised, an explicit diff passed through."""
        import difflib

        chunks: list[str] = []
        for raw_path, content in files:
            rel = raw_path.strip()
            existing = ""
            target = self.repo / rel
            if target.is_file():
                existing = target.read_text(errors="replace")
            body = "".join(difflib.unified_diff(
                existing.splitlines(keepends=True), content.splitlines(keepends=True),
                fromfile=f"a/{rel}" if existing else "/dev/null", tofile=f"b/{rel}",
            ))
            chunks.append(f"diff --git a/{rel} b/{rel}\n" + ("" if existing else "new file mode 100644\n") + body)
        if diff_text:
            chunks.append(diff_text)
        return "\n".join(chunks)

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
        if turn.files:
            turn.commit = self._commit(turn, turn.intent or "chat turn")
        self.turns.append(turn)
        return self._finish(turn, t0)

    def _commit(self, turn: Turn, intent: str) -> str:
        if subprocess.run(["git", "rev-parse", "--git-dir"], cwd=self.repo,
                          capture_output=True, timeout=20).returncode != 0:
            turn.commit_note = "not a git repo — files written, but nothing to revert to"
            return ""
        # only what THIS turn touched is staged: `git add -A` swept a user's own
        # in-flight edits into a chat commit (found by audit). Never the machinery.
        paths = [f for f in dict.fromkeys(turn.files) if not f.startswith(".ratchet")]
        if not paths:
            turn.commit_note = "nothing to commit"
            return ""
        subprocess.run(["git", "add", "--", *paths], cwd=self.repo, capture_output=True, timeout=30)
        r = subprocess.run(
            ["git", "-c", "user.name=ratchet-chat", "-c", "user.email=chat@ratchet.local",
             "commit", "-q", "-m", f"[ratchet chat] {intent}"],
            cwd=self.repo, capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            turn.commit_note = f"commit failed: {(r.stderr or r.stdout)[-80:].strip()}"
            return ""
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=self.repo,
                              capture_output=True, text=True, timeout=20).stdout.strip()

    def _finish(self, turn: Turn, t0: float) -> Turn:
        turn.seconds = round(time.time() - t0, 2)
        self._bus("chat.done", intent=turn.intent, files=turn.files, commit=turn.commit,
                  error=turn.error, cancelled=turn.cancelled, seconds=turn.seconds)
        return turn

    def _bus(self, kind: str, **payload) -> None:
        if self.bus is not None:
            self.bus.emit(kind, **payload)
