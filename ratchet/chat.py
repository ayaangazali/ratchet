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

from . import debuglog
from .context import tree_listing, walk_files
from .providers import ChatBackend, ChatProviderError, looks_like_secret, redact

FILE_FENCE = re.compile(r"```file:([^\n`]+)\n(.*?)```", re.S)
DIFF_FENCE = re.compile(r"```(?:diff|patch)\n(.*?)```", re.S)
INTENT_LINE = re.compile(r"^\s*intent\s*:\s*(.+)$", re.I | re.M)

_PROMPT = """You are the coding hand behind ratchet's console. You write code straight into the
user's working directory; every turn becomes one git commit the user can revert.

Working directory listing:
{listing}

{sources}{history}{review}User request:
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
    def __init__(self, repo: Path, backend: ChatBackend | None = None, bus=None, qodo=None) -> None:
        self.repo = Path(repo).resolve()
        self.backend = backend or ChatBackend.from_env()
        self.bus = bus
        self.qodo = qodo  # advisory Qodo review context, or None
        self.cancel = threading.Event()
        self.turns: list[Turn] = []
        self._label = "turn-1"          # the current turn's node id, for the console
        self._focus = ""  # the live prompt, so _sources_block can prefer named files

    # ----------------------------------------------------------------- turn --

    def run_turn(self, prompt: str, emit: Callable[[str, str], None]) -> Turn:
        """Never raises. Every failure path ends as a Turn carrying `error`."""
        try:
            return self._run_turn(prompt, emit)
        except BaseException as e:  # the last net; a worker swallows what escapes
            debuglog.exception("turn crashed", e)
            t = Turn(prompt=prompt, error=f"{type(e).__name__}: {e}")
            emit("error", t.error)
            return self._finish(t, time.time())

    def _run_turn(self, prompt: str, emit: Callable[[str, str], None]) -> Turn:
        """One chat turn. `emit(kind, text)` receives the ultra-summary lines the
        activity pane shows; kinds: step, note, done, error."""
        t0 = time.time()
        turn = Turn(prompt=prompt)
        self.cancel.clear()
        # A mistyped /connect makes the "command" a prompt -- and a prompt is sent
        # to a third-party model and appended to the bus file. Anything key-shaped
        # stops here, unsent and unlogged, so a typo never forces a key rotation.
        if looks_like_secret(prompt):
            turn.error = "that looks like an API key — refusing to send it to a model or write it to the bus. Use /connect."
            emit("error", turn.error)
            return self._finish(turn, t0)
        self._bus("chat.turn", prompt=redact(prompt[:200]), provider=self.backend.provider, model=self.backend.model)

        # The console's panes -- tree, gauntlet rail, counters, waiting-on -- are fed
        # by run events. A chat turn used to emit only chat.*, which the renderer
        # skips, so four panes sat dead through every session and the tool looked
        # broken while it worked. A turn IS a node: it has an intent, it gets
        # graded, it produces files, it lands as a commit. Say so in that language.
        self._label = f"turn-{len(self.turns) + 1}"
        self._bus("expand", node=self._label, fanout=1, depth=len(self.turns), dead_ends=0)
        self._bus("verify.started", label=self._label, parent="chat",
                  intent=redact(prompt[:80]), model=f"{self.backend.provider}/{self.backend.model}")
        for stage in ("build", "f2p", "p2p", "types", "lint", "hygiene"):
            # honest: a chat turn runs the static gate and nothing else, so the
            # rest of the rail is reported skipped rather than left blank
            self._bus("stage.result", label=self._label, stage=stage, passed=True,
                      detail="not run for a chat turn", skipped=True)

        if getattr(self.backend, "agentic", False):
            return self._run_agentic(prompt, emit, turn, t0)

        emit("step", f"asking {self.backend.provider}/{self.backend.model}")
        rendered = self._render(prompt)
        debuglog.log("info", f"turn start · {self.backend.provider}/{self.backend.model} · "
                             f"prompt {len(rendered)} chars")
        t_req = time.time()
        try:
            reply = self.backend.complete(rendered)
        except ChatProviderError as e:
            turn.error = str(e)
            debuglog.log("error", f"provider refused: {e}")
            emit("error", turn.error)
            return self._finish(turn, t0)
        except BaseException as e:
            # A timeout, a dropped connection, a malformed payload -- anything not
            # already a ChatProviderError used to escape into the worker, which
            # swallows it, leaving the pane frozen on "asking..." forever. Never
            # again: every failure becomes a visible error line.
            turn.error = f"{type(e).__name__}: {e}"
            debuglog.exception("provider call failed", e)
            emit("error", turn.error)
            return self._finish(turn, t0)
        debuglog.log("info", f"reply in {time.time() - t_req:.1f}s · {len(reply)} chars")
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
            self._bus("stage.result", label=self._label, stage="cheat", passed=False,
                      detail=f"{crit[0].rule} — blocked")
            turn.error = f"gauntlet blocked the turn: {crit[0].one_line()}"
            emit("error", turn.error)
            return self._finish(turn, t0)
        self._bus("stage.result", label=self._label, stage="cheat", passed=True,
                  detail=f"{len(findings)} finding(s), 0 critical")
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
            from .verifier.cheat import parse_unified_diff

            targets = [f.path for f in parse_unified_diff(diff.group(1)) if f.path]
            applied = self._apply(diff.group(1))
            if applied:
                turn.files.extend(dict.fromkeys(targets))
                emit("step", "applied diff")
            else:
                # a diff against files that are not there (the model guessed at a
                # repo it could not see) used to end as a silent success: no files,
                # no commit, outcome "ok". That is the worst possible report.
                named = ", ".join(dict.fromkeys(targets)) or "the named files"
                turn.error = f"the model's diff did not apply to {named} — nothing was written"
                emit("error", turn.error)
                return self._finish(turn, t0)

        if not turn.files and not turn.cancelled:
            turn.error = "the model replied but produced no file the working directory could take"
            emit("error", turn.error)
            return self._finish(turn, t0)

        return self._commit_and_finish(turn, t0)

    def _run_agentic(self, prompt: str, emit, turn: Turn, t0: float) -> Turn:
        """A turn where the provider edits the tree itself.

        Claude Code does the work and narrates every step through `emit`; ratchet's
        contribution is unchanged -- the resulting diff goes through the same cheat
        gate as any other patch, and lands as one commit that /undo can revert. The
        gate matters more here, not less: nothing parsed the output, so the working
        tree is the only record of what happened.
        """
        import subprocess

        from .verifier import cheat as cheat_mod

        emit("step", f"claude code session · {self.backend.model}")
        self._bus("sandbox.created", label=self._label, provider="claude-code")
        started_at = time.time()

        def relay(kind: str, text: str) -> None:
            """One step of the session: to the activity pane, to the waiting-on
            panel, and onto the bus so the browser and a replay see it too."""
            emit(kind, text)
            self._bus("chat.step", label=self._label, text=text)

        
        before = self._tracked_state()
        try:
            self.backend.run_agentic(prompt, self.repo, relay)
        except ChatProviderError as e:
            turn.error = str(e)
            debuglog.log("error", f"agentic session failed: {e}")
            emit("error", turn.error)
            return self._finish(turn, t0)
        except BaseException as e:
            turn.error = f"{type(e).__name__}: {e}"
            debuglog.exception("agentic session crashed", e)
            emit("error", turn.error)
            return self._finish(turn, t0)

        # created and edited, not one or the other: a session that added a file and
        # changed another used to commit only the new one, leaving the edit dirty for
        # the next turn to sweep up under its own name.
        turn.files = sorted(set(self._tracked_state()) - set(before) | set(self._dirty_paths(started_at)))
        if not turn.files:
            turn.error = "the session finished without changing any file"
            emit("error", turn.error)
            return self._finish(turn, t0)

        diff = subprocess.run(["git", "diff", "HEAD", "--", *turn.files], cwd=self.repo,
                              capture_output=True, text=True, timeout=60).stdout
        if not diff:
            # no repo to diff against: synthesise one from what is on disk so the
            # cheat gate still sees the same text it would have seen in a repo
            diff = self._as_diff(
                [(f, (self.repo / f).read_text(errors="replace"))
                 for f in turn.files if (self.repo / f).is_file()],
                "", from_disk=False,
            )
        findings = cheat_mod.inspect(diff or "", protected_paths=list(cheat_mod.DEFAULT_PROTECTED))
        crit = [f for f in findings if f.severity.value == "critical"]
        if crit:
            # the session already wrote; reverting is the only honest response
            subprocess.run(["git", "checkout", "--", *turn.files], cwd=self.repo,
                           capture_output=True, timeout=60)
            self._bus("stage.result", label=self._label, stage="cheat", passed=False,
                      detail=f"{crit[0].rule} — blocked, session reverted")
            turn.error = f"gauntlet blocked the session and reverted it: {crit[0].one_line()}"
            emit("error", turn.error)
            return self._finish(turn, t0)
        self._bus("stage.result", label=self._label, stage="cheat", passed=True,
                  detail=f"{len(findings)} finding(s), 0 critical")
        emit("step", "gauntlet cheat check: clean" if not findings
             else f"gauntlet cheat check: {len(findings)} warning(s), none blocking")

        turn.intent = turn.intent or f"claude code: {prompt[:80]}"
        return self._commit_and_finish(turn, t0)

    def _tracked_state(self) -> list[str]:
        """Every file in the working directory -- the before/after of a session.

        Git first, because it already knows what to ignore. Without a repo it walks
        the tree instead: a session that built a real site in a plain directory was
        reported as "changed nothing", because change detection assumed a repo the
        user had not made. The work is what matters; the commit is a bonus.
        """
        import subprocess

        try:
            r = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                               cwd=self.repo, capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                return [p for p in r.stdout.splitlines() if p and not p.startswith(".ratchet")]
        except (OSError, subprocess.SubprocessError):
            pass
        return [p for p in walk_files(self.repo, limit=5000) if not p.startswith(".ratchet")]

    def _dirty_paths(self, since: float = 0.0) -> list[str]:
        """Modified-in-place files, for a session that edited rather than created.

        `since` is load-bearing in both branches, not just the fallback: a file the
        user had already edited before the turn began is dirty too, and taking every
        dirty path put somebody else's in-flight work inside this turn's commit --
        and, in `qodo-fix`, pushed it under a Qodo finding's name. Only a file the
        session itself wrote has an mtime past the start.

        Falls back to modification time outside a repo, for the same reason
        `_tracked_state` does.
        """
        import subprocess

        try:
            r = subprocess.run(["git", "diff", "--name-only", "HEAD"], cwd=self.repo,
                               capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                return [p for p in r.stdout.splitlines()
                        if p and not p.startswith(".ratchet") and self._touched_since(p, since)]
        except (OSError, subprocess.SubprocessError):
            pass
        if not since:
            return []
        return [
            rel for rel in walk_files(self.repo, limit=5000)
            if not rel.startswith(".ratchet") and self._touched_since(rel, since)
        ]

    def _touched_since(self, rel: str, since: float) -> bool:
        p = self.repo / rel
        try:
            return not since or p.stat().st_mtime >= since
        except OSError:
            return True  # deleted by the session; still this turn's change

    # -------------------------------------------------------------- plumbing --

    def _render(self, prompt: str) -> str:
        history = ""
        if self.turns:
            lines = [f"- {t.intent or t.prompt[:60]} -> {'ok' if t.ok else t.error or 'cancelled'}"
                     for t in self.turns[-3:]]
            history = "Recent turns:\n" + "\n".join(lines) + "\n\n"
        self._focus = prompt
        review = ""
        if self.qodo is not None:
            text = self.qodo.findings_for_prompt(cap=1500)
            if text:
                review = ("Latest Qodo review of this repo's open PR "
                          "(advisory -- may lag local changes):\n" + text + "\n\n")
        return _PROMPT.format(
            listing=tree_listing(self.repo, [])[:4000],
            sources=self._sources_block(),
            history=history,
            review=review,
            prompt=prompt,
        )

    def _sources_block(self, *, max_files: int = 8, max_chars: int = 10_000) -> str:
        """The current contents of the repo's small text files, so an edit request
        edits what is actually there instead of hallucinating it. Capped hard --
        context is the scarcest resource, and a lockfile is not worth shipping."""
        keep = (".html", ".css", ".js", ".ts", ".py", ".md", ".json", ".yaml", ".yml", ".toml")
        candidates = [r for r in walk_files(self.repo, limit=2000) if Path(r).suffix in keep]
        # files the request names come first -- alphabetical order shipped
        # AGENTS.md when you asked about index.html
        named = [r for r in candidates if Path(r).name.lower() in (self._focus or "").lower()]
        rest = sorted(set(candidates) - set(named),
                      key=lambda r: -(self.repo / r).stat().st_mtime)  # then most recent
        parts: list[str] = []
        used = 0
        for rel in [*named, *rest]:
            if len(parts) >= max_files or used >= max_chars:
                break
            text = (self.repo / rel).read_text(errors="replace")
            if len(text) > 4000:
                continue  # big files are named in the listing; the model can ask
            parts.append(f"--- {rel} ---\n{text}")
            used += len(text)
        if not parts:
            return ""
        return "Current file contents (edit these, do not reinvent them):\n" + "\n".join(parts) + "\n\n"

    def _as_diff(self, files: list[tuple[str, str]], diff_text: str, *, from_disk: bool = True) -> str:
        """What the model wants to write, as one unified diff the cheat detector
        can inspect -- new files synthesised, an explicit diff passed through."""
        import difflib

        chunks: list[str] = []
        for raw_path, content in files:
            rel = raw_path.strip()
            existing = ""
            target = self.repo / rel
            # `from_disk=False` when the content is ALREADY on disk (an agentic
            # session wrote it): reading the file back would diff it against itself
            # and hand the cheat gate an empty patch to approve.
            if from_disk and target.is_file():
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
        # Every turn is recorded, not just the ones that committed. A failed turn
        # vanishing from the session made /export read "0 turns" for a session that
        # had plainly just run one -- the report said nothing happened while the
        # user watched it happen.
        if turn not in self.turns:
            self.turns.append(turn)
        node: dict[str, object] = {
            "id": getattr(self, "_label", "turn"),
            "parent": None,
            "score": 1.0 if turn.ok else 0.0,
            "green": turn.ok,
            "outcome": "green" if turn.ok else ("cancelled" if turn.cancelled else "broken"),
            # redacted: the intent falls back to the prompt, and a refused
            # key-shaped prompt would otherwise be written to the bus by the very
            # event that draws it on screen
            "intent": redact(turn.intent or turn.prompt[:60]),
            "model": f"{self.backend.provider}/{self.backend.model}",
            "depth": max(0, len(self.turns) - 1),
            "findings": [],
            "reason": redact(turn.error or f"{len(turn.files)} file(s)"),
        }
        self._bus("node.added" if turn.ok else "node.pruned", **node)
        self._bus("chat.done", intent=turn.intent, files=turn.files, commit=turn.commit,
                  error=turn.error, cancelled=turn.cancelled, seconds=turn.seconds)
        return turn

    def _bus(self, kind: str, **payload) -> None:
        if self.bus is not None:
            self.bus.emit(kind, **payload)
