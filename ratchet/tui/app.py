"""The Ratchet console.

Three columns, and each answers a different question a stranger standing behind
you would ask:

    left     where has the search been      the tree, with scores, live and pruned
    centre   what is it doing right now     the activity stream, step by step
    right    how is this one being judged   the gauntlet, and what it is waiting on

Under them a status line says what it is costing, and above them a header says
what the run *is*. Nothing irreversible happens without the approval gate taking
the full width and stopping everything, which is the one interaction in here that
matters more than the others.

Everything renders off the JSONL bus, so the console can be started, killed and
restarted mid-run without disturbing the search, and a finished run can be
replayed into it. Build it against `make fixture` before you point it at a live
run -- there is no reason for the interface to depend on a model being up.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.timer import Timer
from textual.widgets import Button, Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option
from textual.worker import Worker

from .. import debuglog
from ..bus import Bus
from . import mascot as m

STAGES = ["build", "cheat", "f2p", "p2p", "types", "lint", "hygiene"]
STAGE_LABEL = {
    "build": "build / install",
    "cheat": "cheat check",
    "f2p": "fail-to-pass",
    "p2p": "pass-to-pass",
    "types": "type check",
    "lint": "lint",
    "hygiene": "diff hygiene",
    "apply": "patch applies",
}

BULLET = "●"
ELBOW = "⎿"
MARK = "✻"


def _short(model: str, n: int = 22) -> str:
    return (model.split("/")[-1] or model)[:n]


# --------------------------------------------------------------------------- #
# header
# --------------------------------------------------------------------------- #


class Banner(Static):
    """The mascot, the claim, and the identity of this particular run."""

    def __init__(self) -> None:
        super().__init__(id="banner")
        # Not `task`: `MessagePump.task` is the widget's own asyncio task, and a
        # widget attribute by that name explodes the first time anything reads it
        # before the widget is running.
        self.task_id = "—"
        self.provider = "—"
        self.run_id = "—"
        self.snapshots = True
        self.compact = False
        self.draw()

    def draw(self) -> None:
        sandbox = "snapshots" if self.snapshots else "worktree fallback"
        idle = self.task_id == "—"
        title = Text()
        title.append(f"{MARK} ", style=f"bold {m.ACCENT}")
        title.append("ratchet", style=f"bold {m.TEXT}")
        # Three widths of the same fact. The identity line is what tells a stranger
        # which run they are looking at, so it shortens rather than wrapping -- a
        # wrapped line here paints itself straight over the mascot.
        if idle:
            identity = Text("no run on this bus yet", style=m.DIM)
        elif self.compact:
            identity = Text.assemble((self.task_id, m.TEXT), ("  ·  ", m.DIM), (sandbox, m.MUTED))
        else:
            identity = Text.assemble(
                (self.task_id, m.TEXT),
                ("  ·  ", m.DIM),
                (f"{self.provider} · {sandbox}", m.MUTED),
                ("  ·  ", m.DIM),
                (self.run_id, m.DIM),
            )
        lines = [
            title,
            Text("the agent doesn't decide it's done. the tests do.", style=m.MUTED),
            Text(),
            identity,
        ]
        # Compact swaps in the smaller sprite rather than dropping her: the mascot
        # is the identity of the run, and a header that loses it mid-demo reads as
        # a different program.
        sprite = m.FIN_TINY if self.compact else m.FIN_SMALL
        self.update(m.beside(sprite, lines, gap=3, indent=1))


# --------------------------------------------------------------------------- #
# left: the tree
# --------------------------------------------------------------------------- #


class TreePane(Static):
    """Where the search has been. Live branch highlighted, pruned nodes muted."""

    def __init__(self) -> None:
        super().__init__(id="tree")
        self.nodes: dict[str, dict] = {}
        self.order: list[str] = []
        self.live: str | None = None
        self.draw()

    def upsert(self, node: dict, *, pruned: bool = False) -> None:
        nid = node.get("id") or "?"
        merged = {**self.nodes.get(nid, {}), **node, "pruned": pruned}
        if nid not in self.nodes:
            self.order.append(nid)
        self.nodes[nid] = merged
        if not pruned:
            self.live = nid
        self.draw()

    def draw(self) -> None:
        t = Text(no_wrap=True, overflow="ellipsis")
        if not self.nodes:
            t.append("  waiting for the root node…", style=m.DIM)
            self.update(t)
            return

        children: dict[str | None, list[str]] = {}
        for nid in self.order:
            children.setdefault(self.nodes[nid].get("parent"), []).append(nid)

        def walk(nid: str, prefix: str, last: bool, root: bool) -> None:
            n = self.nodes[nid]
            pruned, green = n.get("pruned"), n.get("green")
            glyph = "✗" if pruned else ("★" if green else BULLET)
            colour = m.RED if pruned else (m.GREEN if green else (m.ACCENT if nid == self.live else m.MUTED))
            t.append(f"{prefix}{'' if root else ('└─' if last else '├─')}", style=m.BORDER)
            t.append(f"{glyph} ", style=colour)
            t.append(f"{nid:<5}", style=f"bold {colour}" if not pruned else m.DIM)
            t.append(f" {n.get('score', 0):.2f}", style=m.DIM if pruned else m.MUTED)
            if green:
                t.append("  green", style=f"bold {m.GREEN}")
            elif nid == self.live and not pruned:
                t.append("  live", style=f"bold {m.ACCENT}")
            t.append("\n")
            # The pane is fixed-width, so the annotation is cut to fit rather than
            # wrapped -- a wrapped tree stops looking like a tree.
            room = max(12, self.size.width - len(prefix) - 5)
            if pruned:
                reason = (n.get("reason") or n.get("outcome") or "pruned")[:room]
                t.append(f"{prefix}{'   ' if last else '│  '}  {reason}\n", style=m.RED)
            elif n.get("model"):
                t.append(f"{prefix}{'   ' if last else '│  '}  {_short(n['model'], room)}\n", style=m.DIM)
            kids = children.get(nid, [])
            for i, kid in enumerate(kids):
                walk(kid, prefix + ("" if root else ("   " if last else "│  ")), i == len(kids) - 1, False)

        for r in children.get(None, []):
            walk(r, "", True, True)
        self.update(t)


class Counters(Static):
    """Ambient proof the harness is carrying the weight, in every screenshot."""

    def __init__(self) -> None:
        super().__init__(id="counters")
        self.subagents = 0
        self.live = 0
        self.total = 0
        self.approvals = 0
        self.compact = False
        self.draw()

    def draw(self) -> None:
        def row(label: str, value: str, style: str) -> Text:
            return Text.assemble((f"  {label:<11}", m.DIM), (value, style), ("\n", ""))

        t = Text(no_wrap=True, overflow="ellipsis")
        if self.compact:
            # On a short terminal these fold onto one line rather than disappearing.
            # They are the ambient proof the harness is carrying the work, and a
            # screenshot without them is a screenshot that proves nothing.
            t.append("  subagents ", style=m.DIM)
            t.append(str(self.subagents), style=m.BLUE)
            t.append(" · sandboxes ", style=m.DIM)
            t.append(f"{self.live}/{self.total}\n", style=m.BLUE)
            t.append("  approvals ", style=m.DIM)
            t.append(str(self.approvals), style=f"bold {m.AMBER}" if self.approvals else m.BLUE)
        else:
            t.append_text(row("subagents", str(self.subagents), m.BLUE))
            t.append_text(row("sandboxes", f"{self.live} live / {self.total}", m.BLUE))
            t.append_text(row("approvals", str(self.approvals),
                              f"bold {m.AMBER}" if self.approvals else m.BLUE))
        self.update(t)


# --------------------------------------------------------------------------- #
# right: the gauntlet
# --------------------------------------------------------------------------- #


class GauntletRail(Static):
    """The seven stages, in order, never scrolled off screen."""

    def __init__(self) -> None:
        super().__init__(id="gauntlet")
        self.subject = ""
        self.state: dict[str, tuple[str, str]] = {}
        self.reset()

    def reset(self, subject: str = "") -> None:
        self.subject = subject
        self.state = {s: ("pending", "") for s in STAGES}
        self.draw()

    def set(self, stage: str, passed: bool, detail: str, skipped: bool = False) -> None:
        self.state[stage] = ("skip" if skipped else ("pass" if passed else "fail"), detail)
        self.draw()

    def draw(self) -> None:
        t = Text()
        if self.subject:
            t.append(f"  {self.subject[:26]}\n\n", style=m.MUTED)
        else:
            t.append("  no candidate in flight\n\n", style=m.DIM)
        for s in STAGES:
            status, detail = self.state.get(s, ("pending", ""))
            glyph, style, name_style = {
                "pass": ("✔", m.GREEN, m.TEXT),
                "fail": ("✖", m.RED, f"bold {m.RED}"),
                "skip": ("−", m.DIM, m.DIM),
                "pending": ("○", m.BORDER, m.DIM),
            }[status]
            t.append(f"  {glyph} ", style=style)
            t.append(f"{STAGE_LABEL[s]:<16}", style=name_style)
            t.append(f"{detail.split(',')[0][:11]}\n", style=m.DIM)
        return self.update(t)


class WaitingOn(Static):
    """What is blocking, stated plainly. Empty when nothing is."""

    def __init__(self) -> None:
        super().__init__(id="waiting")
        self.reason: tuple[str, str] | None = None
        self.draw()

    def show(self, headline: str, detail: str = "") -> None:
        self.reason = (headline, detail)
        self.draw()

    def clear(self) -> None:
        self.reason = None
        self.draw()

    def draw(self) -> None:
        if not self.reason:
            self.update(Text("  nothing blocked", style=m.DIM))
            return
        headline, detail = self.reason
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(f"  ⏸ {headline}\n", style=f"bold {m.AMBER}")
        if detail:
            t.append(f"    {detail[:64]}", style=m.MUTED)
        self.update(t)


# --------------------------------------------------------------------------- #
# bottom: status and the gate
# --------------------------------------------------------------------------- #


class StatusLine(Static):
    """One line: is it moving, how long has it been moving, what has it spent."""

    def __init__(self) -> None:
        super().__init__(id="status")
        self.tick = 0
        self.started = time.time()
        self.state = "idle"  # idle | working | blocked | done
        self.note = "waiting for a run"
        self.budget: dict | None = None
        self.verb_seed = 0
        # Work time, not session age. The clock used to run from the moment the
        # console opened, so a console left open overnight claimed hours of work
        # on a one-second turn. `work_started` is None while idle; `work_total`
        # accumulates only the stretches where something was actually running.
        self.work_started: float | None = None
        self.work_total = 0.0

    def begin_work(self) -> None:
        if self.work_started is None:
            self.work_started = time.time()

    def end_work(self) -> None:
        if self.work_started is not None:
            self.work_total += time.time() - self.work_started
            self.work_started = None

    @property
    def work_seconds(self) -> float:
        """Total time spent working, including the stretch in progress."""
        live = (time.time() - self.work_started) if self.work_started else 0.0
        return self.work_total + live

    def _clock(self) -> str:
        return m.duration(self.work_seconds)

    def draw(self) -> None:
        t = Text(no_wrap=True, overflow="ellipsis")
        if self.state == "working":
            t.append(f" {m.spinner_glyph(self.tick)} ", style=f"bold {m.ACCENT}")
            t.append(f"{m.verb(self.verb_seed)}… ", style=m.TEXT)
            t.append(f"({self._clock()}", style=m.DIM)
        elif self.state == "blocked":
            t.append(" ⏸ ", style=f"bold {m.AMBER}")
            t.append("waiting on you ", style=f"bold {m.AMBER}")
            t.append(f"({self._clock()}", style=m.DIM)
        elif self.state == "done":
            ok = self.note.startswith("green")
            t.append(f" {'✔' if ok else '■'} ", style=f"bold {m.GREEN if ok else m.MUTED}")
            t.append(f"{self.note} ", style=m.GREEN if ok else m.MUTED)
            t.append(f"({self._clock()}", style=m.DIM)
        else:
            t.append(f" {m.spinner_glyph(self.tick // 3)} ", style=m.DIM)
            t.append(f"{self.note} ", style=m.DIM)
            t.append(f"({self._clock() if self.work_total else '—'}", style=m.DIM)

        b = self.budget
        if b:
            t.append(
                f" · {b.get('nodes_used', 0)}/{b.get('max_nodes', 0)} nodes"
                f" · ${b.get('usd_used', 0):.2f} of ${b.get('max_usd', 0):.2f})",
                style=m.DIM,
            )
        else:
            t.append(")", style=m.DIM)
        if self.state == "working":
            t.append("   esc to interrupt", style=m.BORDER)
        self.update(t)


class Hints(Static):
    def on_mount(self) -> None:
        t = Text()
        for key, what in (("a", "approve"), ("d", "deny"), ("r", "rewind"),
                          ("f", "follow"), ("q", "quit")):
            t.append(f"  {key}", style=m.MUTED)
            t.append(f" {what}", style=m.DIM)
        self.update(t)


class ApprovalGate(Vertical):
    """The permission prompt. Full width, and it blocks.

    This is the single most important interaction in the console: it is the
    difference between an agent that asks before the irreversible step and one
    that apologises after it. It is therefore the only thing allowed to take the
    whole width, and it does not go away until somebody decides.
    """

    OPTIONS = ("Yes, open the pull request", "No, keep searching")

    def __init__(self) -> None:
        super().__init__(id="approval")
        self.choice = 0
        self.payload: dict = {}

    def compose(self) -> ComposeResult:
        yield Static(id="approval-head")
        yield Static(id="approval-diff")
        yield Static(id="approval-choices")
        with Horizontal(id="approval-buttons"):
            yield Button("Approve", variant="success", id="approve")
            yield Button("Deny", variant="error", id="deny")

    def show(self, payload: dict) -> None:
        self.payload = payload
        self.choice = 0
        stats = payload.get("stats") or {}
        head = Text()
        head.append(f" {MARK} ", style=f"bold {m.ACCENT}")
        head.append("Ratchet wants to ", style=m.TEXT)
        head.append(payload.get("action", "do something irreversible"), style=f"bold {m.ACCENT}")
        head.append("\n\n")
        head.append(f"   {payload.get('summary', '')}\n", style=m.TEXT)
        if stats:
            head.append("   " + " · ".join(f"{k} {v}" for k, v in stats.items()), style=m.DIM)
        self.query_one("#approval-head", Static).update(head)

        diff = Text()
        for line in (payload.get("diff_preview") or "").splitlines()[:8]:
            if line.startswith("+++") or line.startswith("---"):
                diff.append(f"   {line[:96]}\n", style=m.DIM)
            elif line.startswith("+"):
                diff.append(f"   {line[:96]}\n", style=m.GREEN)
            elif line.startswith("-"):
                diff.append(f"   {line[:96]}\n", style=m.RED)
            else:
                diff.append(f"   {line[:96]}\n", style=m.DIM)
        self.query_one("#approval-diff", Static).update(diff)

        self.draw_choices()
        self.display = True

    def draw_choices(self) -> None:
        t = Text()
        t.append("   Do you want to proceed?\n", style=m.TEXT)
        for i, opt in enumerate(self.OPTIONS):
            selected = i == self.choice
            t.append("   ❯ " if selected else "     ", style=f"bold {m.ACCENT}")
            t.append(f"{i + 1}. {opt}\n", style=f"bold {m.TEXT}" if selected else m.MUTED)
        self.query_one("#approval-choices", Static).update(t)

    def move(self, delta: int) -> None:
        self.choice = (self.choice + delta) % len(self.OPTIONS)
        self.draw_choices()

    def hide(self) -> None:
        self.display = False


# --------------------------------------------------------------------------- #
# the app
# --------------------------------------------------------------------------- #


class RatchetApp(App):
    # focus starts in the chat box: the first keystroke on a coding console must
    # type, not fire a letter binding (a prompt containing "q" used to quit)
    AUTO_FOCUS = "#chat"

    CSS_PATH = "theme.tcss"
    TITLE = "ratchet"

    BINDINGS = [
        Binding("a,1", "approve", "Approve", show=False),
        Binding("d,2,escape", "deny", "Deny", show=False),
        Binding("up,k", "up", "Up", show=False),
        Binding("down,j", "down", "Down", show=False),
        Binding("enter", "confirm", "Confirm", show=False),
        Binding("f", "follow", "Follow", show=False),
        Binding("r", "rewind", "Rewind", show=False),
        Binding("q,ctrl+c", "quit", "Quit", show=False),
    ]

    def __init__(self, bus_path: Path, repo: Path) -> None:
        super().__init__()
        self.bus = Bus(bus_path)
        self.repo = Path(repo)
        self.pending_id: str | None = None
        self.follow = True
        self.seen_any = False
        self._short = False
        self._chat = None                                    # lazy ChatSession
        self._chat_worker: Worker | None = None              # the running background turn, if any
        self._palette_rows: list = []                        # rows behind the visible options
        self._awaiting_key: str | None = None                # /connect: which provider's key comes next
        self._heartbeat: Timer | None = None                 # ticks while a turn is in flight
        self._splash_showing = False                         # the idle dolphin, not the session
        self._turn_started = 0.0

    # ----------------------------------------------------------------- layout --

    def compose(self) -> ComposeResult:
        yield Banner()
        with Horizontal(id="main"):
            with Vertical(id="left"):
                with VerticalScroll(id="tree-box"):
                    yield TreePane()
                yield Counters()
            with Vertical(id="activity-box"):
                yield RichLog(id="activity", wrap=True, markup=False, highlight=False,
                              auto_scroll=True, min_width=16)
                yield RichLog(id="debug", wrap=True, markup=False, highlight=False,
                              auto_scroll=True, min_width=16)
                yield OptionList(id="palette")
                yield Input(
                    id="chat",
                    placeholder="ask for code, or / for commands — Enter runs · Esc interrupts",
                )
            with Vertical(id="right"):
                yield GauntletRail()
                yield WaitingOn()
        yield ApprovalGate()
        yield StatusLine()
        yield Hints(id="hints")

    def on_mount(self) -> None:
        self.query_one("#tree-box").border_title = "search tree"
        self.query_one("#counters").border_title = "harness"
        self.query_one("#activity-box").border_title = "activity"
        self.query_one("#gauntlet").border_title = "gauntlet"
        self.query_one("#waiting").border_title = "waiting on"
        self.query_one(ApprovalGate).display = False
        self.query_one("#palette", OptionList).display = False
        dbg = self.query_one("#debug", RichLog)
        dbg.border_title = "debug"
        dbg.display = debuglog.enabled()
        debuglog.configure(self.repo)
        debuglog.install_logging()
        debuglog.subscribe(self._on_debug_line)
        debuglog.log("info", f"console attached · repo {self.repo} · bus {self.bus.path.name}")
        self.query_one(StatusLine).draw()
        self._short = self.size.height < 40
        self._resize_banner()
        self._fit()
        self._idle_splash()
        self.call_after_refresh(self._warn_about_the_directory)
        self.call_after_refresh(self._first_run_connect)
        self.set_interval(0.2, self.drain)
        self.set_interval(0.12, self.animate_status)

    def on_resize(self, event) -> None:
        """Degrade in a chosen order rather than an accidental one.

        Narrow terminals happen at demo tables. The centre column is the one a
        stranger reads, so the side rails fold away first and the activity stream
        is the last thing standing.
        """
        w, h = event.size.width, event.size.height
        self.query_one("#right").display = w >= 104
        self.query_one("#left").display = w >= 76
        self._short = h < 40
        self._resize_banner()
        self._fit()

    def _fit(self) -> None:
        """On a short terminal the gate gives up rows so the run behind it stays
        visible. A reviewer who cannot see the tree cannot judge the diff."""
        self.query_one("#approval").styles.max_height = 12 if self._short else 17
        counters = self.query_one(Counters)
        if counters.compact != self._short:
            counters.compact = self._short
            counters.draw()

    def _resize_banner(self) -> None:
        """The header is the first thing to give way. It gives way for a narrow
        terminal, for a short one, and for a pending approval -- because when a
        human is being asked to decide, the diff matters more than the mascot."""
        banner = self.query_one(Banner)
        compact = self.size.width < 84 or getattr(self, "_short", False) or self._gate_armed
        if compact != banner.compact:
            banner.compact = compact
            banner.draw()

    def _warn_about_the_directory(self) -> None:
        """`ratchet` in $HOME cannot commit and will feed the model your whole home
        directory. Say so before the first prompt, not after it fails."""
        from ..gitstate import is_repo

        log = self.query_one("#activity", RichLog)
        if self.repo == Path.home():
            self._step(log, "heads up", "this is your home directory", m.AMBER)
            self._note(log, "make a project folder first — `mkdir mysite && cd mysite && git init && ratchet`", m.AMBER)
        elif not is_repo(self.repo):
            self._step(log, "heads up", "not a git repository", m.AMBER)
            self._note(log, "run `git init` here so each turn becomes a commit you can /undo", m.AMBER)

    def _first_run_connect(self) -> None:
        """Nothing connected means the first thing on screen is the connect picker:
        a coding console whose first prompt would hit a demo scaffolder is a trap."""
        from ..providers import connected_providers

        live = connected_providers()
        if any(ok for name, ok in live.items() if name != "demo"):
            return
        log = self.query_one("#activity", RichLog)
        self._step(log, "connect", "no model connected yet", m.AMBER)
        self._note(log, "pick a provider below and paste its API key — once, it persists.")
        self._note(log, "or start TrueForge (`npx @truefoundry/trueforge@latest`) and pick trueforge.")
        box = self.query_one("#chat", Input)
        box.value = "/connect "
        box.focus()
        box.cursor_position = len(box.value)

    def _clear_splash(self, log: RichLog) -> None:
        """The dolphin and the quick start go once real work starts, and only then."""
        if self._splash_showing:
            log.clear()
            self._splash_showing = False

    def _idle_splash(self) -> None:
        """Until the first event lands, the console is a dolphin and a promise."""
        log = self.query_one("#activity", RichLog)
        self._splash_showing = True
        log.write(Text())
        log.write(m.render(m.FIN, indent=6, dim=0.55))
        log.write(Text("   nothing on the bus yet. quick start:\n", style=m.MUTED))
        log.write(Text("   ratchet demo --dir demo-repo      seed a playground repo", style=m.DIM))
        log.write(Text("   ratchet run --repo demo-repo      search until the verifier says green", style=m.DIM))
        log.write(Text("   ratchet redteam --repo demo-repo  score the verifier itself", style=m.DIM))
        log.write(Text("\n   then run `ratchet` here again to watch it live.", style=m.MUTED))

    # -------------------------------------------------------------- animation --

    def animate_status(self) -> None:
        s = self.query_one(StatusLine)
        s.tick += 1
        if s.tick % 40 == 0:
            s.verb_seed += 1
        s.draw()

    # -------------------------------------------------------------------- bus --

    def drain(self) -> None:
        # A timer callback that raises takes the whole app down with it. The panes
        # are gone during teardown and can be absent mid-relayout, and neither is
        # worth killing a session over -- the next tick will find them.
        try:
            tree = self.query_one(TreePane)
            rail = self.query_one(GauntletRail)
            counters = self.query_one(Counters)
            waiting = self.query_one(WaitingOn)
            status = self.query_one(StatusLine)
        except NoMatches:
            return
        log = self.query_one("#activity", RichLog)

        for ev in self.bus.tail():
            p, k = ev.payload, ev.kind
            if k == "chat.step":
                # the live pulse of an agentic session: the activity pane already
                # has it from the worker, but the waiting-on panel only learns what
                # the session is doing from here
                waiting.show("claude code", str(p.get("text", ""))[:70])
                continue
            if k.startswith("chat."):
                # the turn's own start/end records: for the dashboard and replay.
                # They must not count as "the run started", which clears the log.
                if k == "chat.done":
                    waiting.clear()
                continue
            if not self.seen_any:
                self.seen_any = True
                # Clear the idle splash, not the session. A chat turn writes its own
                # narration first and its bus events arrive second, so clearing the
                # whole log here erased the very lines the user was reading.
                self._clear_splash(log)
                # from now, not from the event's timestamp: attaching to an
                # existing bus file made the clock read hours (found live)
                status.started = time.time()
            if k != "run.done" and status.state != "blocked":
                status.state = "working"
                status.begin_work()
            if k == "run.done":
                status.end_work()

            if k == "run.started":
                b = self.query_one(Banner)
                b.task_id = str(p.get("task", "—"))
                b.provider = str(p.get("provider", "—"))
                b.run_id = str(p.get("run_id", "—"))
                b.snapshots = bool(p.get("snapshots", True))
                b.draw()
                status.budget = p.get("budget")
                self._step(log, "Run", p.get("run_id", ""), m.ACCENT)
                self._note(log, f"task {p.get('task')} · provider {p.get('provider')}")

            elif k == "repo.mapped":
                counters.subagents += 1
                counters.draw()
                self._step(log, "Cartographer", "repo map", m.BLUE)
                self._note(log, f"{p.get('lines')} lines of repo map into the context")

            elif k == "sandbox.created":
                counters.live += 1
                counters.total += 1
                counters.draw()

            elif k == "expand":
                self._step(log, "Expand", str(p.get("node", "")), m.ACCENT)
                self._note(log, f"fanout {p.get('fanout')} · depth {p.get('depth')} · "
                                f"{p.get('dead_ends', 0)} dead end(s) fed back to siblings")

            elif k == "verify.started":
                rail.reset(str(p.get("intent", ""))[:40])
                counters.subagents += 1
                counters.draw()
                self._step(log, "Verify", str(p.get("label", "")), m.ACCENT)
                self._note(log, f"{_short(str(p.get('model', '')))}  {p.get('intent', '')}", style=m.MUTED)

            elif k == "stage.result":
                stage = str(p.get("stage", ""))
                passed, skipped = bool(p.get("passed")), bool(p.get("skipped"))
                if stage in rail.state:
                    rail.set(stage, passed, str(p.get("detail", "")), skipped)
                mark = "skip" if skipped else ("PASS" if passed else "FAIL")
                colour = m.DIM if skipped else (m.GREEN if passed else m.RED)
                self._note(log, f"{mark:4}  {STAGE_LABEL.get(stage, stage):<16}{p.get('detail', '')}",
                           style=colour)

            elif k == "subagent":
                # Declared in `bus.py` as part of the renderer contract. Nothing in
                # the loop emits it today; a role the harness spawns on its own --
                # a reviewer, a second cartographer -- would arrive here.
                counters.subagents += 1
                counters.draw()
                self._step(log, "Subagent", str(p.get("role") or p.get("label", "")), m.BLUE)
                if p.get("task"):
                    self._note(log, str(p["task"])[:90], style=m.MUTED)

            elif k == "candidate.empty":
                self._note(log, f"{_short(str(p.get('model', '')))} returned no patch", style=m.DIM)

            elif k == "node.added":
                tree.upsert(p)
                counters.live = max(0, counters.live - 1)
                counters.draw()
                green = bool(p.get("green"))
                self._note(log, f"kept {p.get('id')} at {p.get('score', 0):.2f}"
                                + ("   ★ every gate green" if green else ""),
                           style=f"bold {m.GREEN}" if green else m.GREEN)

            elif k == "node.pruned":
                tree.upsert(p, pruned=True)
                counters.live = max(0, counters.live - 1)
                counters.draw()
                findings = ", ".join(p.get("findings") or [])
                self._step(log, "Prune", str(p.get("id", "")), m.RED)
                self._note(log, f"{p.get('reason') or p.get('outcome')}"
                                + (f"  [{findings}]" if findings else ""), style=m.RED)

            elif k == "stall":
                self._step(log, "Stall", str(p.get("node", "")), m.AMBER)
                self._note(log, f"no improvement for 3 expansions — forking {p.get('fanout')} ways "
                                f"from depth {p.get('depth')}", style=m.AMBER)

            elif k == "docs.fetch":
                self._step(log, "BrightData", f"{p.get('library')} {p.get('version')}", m.VIOLET)

            elif k == "docs.heal":
                self._step(log, "ScraperRepair", str(p.get("library", "")), m.VIOLET)
                self._note(log, f"{p.get('old_section')!r} → {p.get('new_section')!r}", style=m.VIOLET)

            elif k == "approval.required":
                self.pending_id = p.get("id")
                counters.approvals += 1
                counters.draw()
                status.state = "blocked"
                waiting.show(f"approval {p.get('id')}", str(p.get("summary", "")))
                self.query_one(ApprovalGate).show(p)
                self._resize_banner()
                self.bell()

            elif k == "approval.resolved":
                counters.approvals = max(0, counters.approvals - 1)
                counters.draw()
                status.state = "working"
                waiting.clear()
                self.query_one(ApprovalGate).hide()
                self._resize_banner()
                allowed = bool(p.get("approved"))
                self._note(log, f"approval {'granted' if allowed else 'denied'}",
                           style=m.GREEN if allowed else m.RED)

            elif k == "rewind":
                self._step(log, "Rewind", str(p.get("node", "")), m.AMBER)

            elif k == "run.done":
                status.budget = p.get("budget")
                status.state = "done"
                green = bool(p.get("green"))
                status.note = "green" if green else str(p.get("reason", "stopped"))
                waiting.clear()
                self._step(log, "Done", str(p.get("winner", "")), m.GREEN if green else m.MUTED)
                self._note(log, f"{p.get('reason')} · winner {p.get('winner')} "
                                f"at {p.get('score', 0):.2f} · {p.get('nodes')} nodes",
                           style=f"bold {m.GREEN}" if green else m.MUTED)

            if p.get("budget"):
                status.budget = p["budget"]
        status.draw()

    # ---- Claude-Code-shaped output: a bullet for the step, an elbow per result --

    def _step(self, log: RichLog, verb: str, arg: str, colour: str) -> None:
        t = Text()
        t.append("\n")
        t.append(f"{BULLET} ", style=f"bold {colour}")
        t.append(verb, style=f"bold {m.TEXT}")
        if arg:
            t.append(f"({arg})", style=m.MUTED)
        log.write(t)

    def _note(self, log: RichLog, body: str, style: str = "") -> None:
        t = Text()
        t.append(f"  {ELBOW}  ", style=m.BORDER)
        t.append(body, style=style or m.MUTED)
        log.write(t)

    # ------------------------------------------------------------------ chat --
    # The input box turns the console into the front door of a coding session: a
    # prompt runs on a worker thread, the activity pane shows the ultra-summary
    # (one line per step, never a raw diff), Esc interrupts mid-turn, and every
    # completed turn is one git commit -- revertible, like everything else here.

    def _restart(self) -> None:
        """Put the console back to a known-good state without leaving it.

        Cancels an in-flight turn, drops the chat session (so the provider is
        re-read from the environment and any wedged http client is discarded),
        rewinds the bus reader and clears the panes. The escape hatch for exactly
        the state where something is stuck and you cannot tell what."""
        log = self.query_one("#activity", RichLog)
        debuglog.log("warn", "restart requested")
        if self._chat_worker is not None:
            try:
                if self._chat is not None:
                    self._chat.cancel.set()
                self._chat_worker.cancel()
            except Exception as e:
                debuglog.exception("cancelling the worker", e)
        self.workers.cancel_group(self, "default")
        self._chat_worker = None
        self._chat = None
        self._awaiting_key = None
        if self._heartbeat is not None:
            self._heartbeat.stop()
            self._heartbeat = None
        box = self.query_one("#chat", Input)
        box.password = False
        box.value = ""
        box.placeholder = "ask for code, or / for commands — Enter runs · Esc interrupts"
        self._close_palette()
        # deliberately NOT a fresh reader: rewinding to byte zero replays the whole
        # session into the freshly cleared pane and buries the restart notice under
        # the history the restart was meant to get away from
        self.seen_any = True
        status = self.query_one(StatusLine)
        status.end_work()
        status.state = "idle"
        status.note = "restarted"
        status.draw()
        log.clear()
        self._idle_splash()
        session = self._chat_session()
        self._note(log, f"restarted · chat on {session.backend.provider}/{session.backend.model}", m.GREEN)
        box.focus()
        self.call_after_refresh(self._first_run_connect)

    def _tick_turn(self) -> None:
        """One status line while a turn runs, so a slow model reads as slow."""
        status = self.query_one(StatusLine)
        running = self._chat_worker is not None and self._chat_worker.is_running
        if not running:
            if self._heartbeat is not None:
                self._heartbeat.stop()
                self._heartbeat = None
            status.end_work()
            status.state = "done" if status.state == "working" else status.state
            status.note = f"idle · {m.duration(status.work_seconds)} of work this session"
            status.draw()
            return
        secs = int(time.time() - self._turn_started)
        status.state = "working"
        status.note = f"generating… {secs}s · Esc to interrupt"
        status.draw()
        if secs and secs % 15 == 0:
            debuglog.log("info", f"still waiting on the model · {secs}s")

    def _on_debug_line(self, ts: float, level: str, text: str) -> None:
        """Called from any thread -- the debug channel is written by workers."""
        try:
            log = self.query_one("#debug", RichLog)
        except Exception:
            return
        colour = {"ERROR": m.RED, "TRACE": m.RED, "WARN": m.AMBER}.get(level, m.DIM)
        t = Text()
        t.append(time.strftime("%H:%M:%S ", time.localtime(ts)), style=m.BORDER)
        t.append(f"{level:<5} ", style=colour)
        t.append(text, style=m.MUTED if level == "INFO" else colour)
        self.call_from_thread(log.write, t) if self._off_thread() else log.write(t)

    def _off_thread(self) -> bool:
        import threading

        return threading.current_thread() is not threading.main_thread()

    def _chat_session(self):
        if self._chat is None:
            from ..chat import ChatSession

            self._chat = ChatSession(self.repo, bus=self.bus)
        return self._chat

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        log = self.query_one("#activity", RichLog)

        # /connect key-prompt mode: this submit IS the pasted key
        if self._awaiting_key:
            provider, self._awaiting_key = self._awaiting_key, None
            event.input.password = False
            event.input.placeholder = "ask for code, or / for commands — Enter runs · Esc interrupts"
            event.input.value = ""
            if text:
                self._connect_with_key(provider, text)
            else:
                self._note(log, "connect cancelled", m.AMBER)
            return

        # palette open with a highlighted row: Enter applies the row, not the text
        palette = self.query_one("#palette", OptionList)
        if palette.display and palette.highlighted is not None and self._palette_rows:
            event.input.value = ""
            self._apply_palette_row(self._palette_rows[palette.highlighted])
            return

        event.input.value = ""
        if not text:
            return
        if text.startswith("/"):
            self._dispatch_command(text)
            return
        self._start_turn(text)

    # ---------------------------------------------------------------- palette --

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._awaiting_key:
            return
        from .palette import rows_for

        rows = rows_for(event.value)
        palette = self.query_one("#palette", OptionList)
        self._palette_rows = rows
        palette.clear_options()
        if rows:
            palette.add_options([Option(f"{r.label:<38} {r.meta}", id=str(i)) for i, r in enumerate(rows)])
            palette.display = True
            palette.highlighted = 0
        else:
            palette.display = False

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "palette" and event.option.id is not None:
            self._apply_palette_row(self._palette_rows[int(event.option.id)])
            self.query_one("#chat", Input).value = ""

    def _close_palette(self) -> None:
        self.query_one("#palette", OptionList).display = False
        self._palette_rows = []

    def _apply_palette_row(self, row) -> None:
        box = self.query_one("#chat", Input)
        if row.kind == "model":
            self._close_palette()
            self._dispatch_command(f"/model {row.value}")
        elif row.kind == "provider":
            self._close_palette()
            self._dispatch_command(f"/connect {row.value}")
        elif row.value in ("/model", "/connect"):
            # these want an argument: complete the text and reopen with the options
            box.value = row.value + " "
            box.focus()
            box.cursor_position = len(box.value)
        else:
            self._close_palette()
            self._dispatch_command(row.value)

    # --------------------------------------------------------------- commands --

    def _dispatch_command(self, text: str) -> None:
        from ..providers import PROVIDERS, connected_providers
        from .palette import COMMANDS, help_lines

        log = self.query_one("#activity", RichLog)
        self._close_palette()
        cmd, _, arg = text.partition(" ")
        arg = arg.strip()
        session = self._chat_session()

        if cmd == "/help":
            self._step(log, "help", "", m.ACCENT)
            for line in help_lines():
                self._note(log, line)
        elif cmd == "/model":
            if not arg:
                box = self.query_one("#chat", Input)
                box.value = "/model "
                box.focus()
                box.cursor_position = len(box.value)
                return
            try:
                self._note(log, f"chat model -> {session.backend.switch(arg)}", m.ACCENT)
            except Exception as e:
                self._note(log, str(e), m.RED)
        elif cmd == "/connect":
            if not arg:
                box = self.query_one("#chat", Input)
                box.value = "/connect "
                box.focus()
                box.cursor_position = len(box.value)
                return
            provider, _, inline_key = arg.partition(" ")
            if provider not in PROVIDERS or provider == "demo":
                self._note(log, f"unknown provider {provider!r} — /connect then pick from the list", m.RED)
                return
            if inline_key:
                self._connect_with_key(provider, inline_key.strip())
                return
            self._awaiting_key = provider
            box = self.query_one("#chat", Input)
            box.password = True
            box.placeholder = f"paste your {provider} API key — Enter saves · empty Enter cancels"
            box.focus()
        elif cmd == "/providers":
            self._step(log, "providers", "", m.ACCENT)
            live = connected_providers()
            for name, (_b, _key_env, default) in PROVIDERS.items():
                mark = "connected" if live.get(name) else f"not connected — /connect {name}"
                self._note(log, f"{name:<10} {default:<28} {mark}",
                           m.GREEN if live.get(name) else m.MUTED)
        elif cmd == "/undo":
            self._undo_last_turn()
        elif cmd == "/last":
            turns = session.turns
            if not turns:
                self._note(log, "no turns yet", m.MUTED)
            else:
                t = turns[-1]
                state = "ok" if t.ok else (t.error or "cancelled")
                self._note(log, f"{t.intent or t.prompt[:60]} · {len(t.files)} file(s) · "
                                f"commit {t.commit or '—'} · {state}")
        elif cmd == "/export":
            from .. import report

            status = self.query_one(StatusLine)
            path = report.write(self.repo, turns=session.turns, bus_path=self.bus.path,
                                work_seconds=status.work_seconds)
            self._step(log, "export", path.name, m.ACCENT)
            ok = [t for t in session.turns if t.ok]
            files = {f for t in session.turns for f in t.files}
            self._note(log, f"{len(session.turns)} turn(s), {len(ok)} landed · {len(files)} file(s) · "
                            f"{m.duration(status.work_seconds)} of work", m.GREEN)
            self._note(log, str(path))
        elif cmd == "/restart":
            self._restart()
        elif cmd == "/debug":
            dbg = self.query_one("#debug", RichLog)
            dbg.display = not dbg.display
            if dbg.display:
                dbg.clear()
                for ts, level, line in debuglog.lines()[-60:]:
                    self._on_debug_line(ts, level, line)
                self._note(log, f"debug panel on · also tailing {self.repo}/.ratchet/debug.log", m.ACCENT)
            else:
                self._note(log, "debug panel off", m.MUTED)
        elif cmd == "/clear":
            log.clear()
            self._idle_splash()
        elif cmd == "/quit":
            self.exit()
        else:
            from ..providers import redact as _redact

            self._note(log, f"unknown command {_redact(cmd)!r} — /help lists everything, "
                            f"or just type what you want built", m.AMBER)
            _ = COMMANDS  # imported for parity with the palette

    def _connect_with_key(self, provider: str, key: str) -> None:
        self._run_connect(provider, key)

    @work(thread=True, exit_on_error=False)
    def _run_connect(self, provider: str, key: str) -> None:
        from ..providers import PROVIDERS, ChatProviderError, save_key, validate_key

        log = self.query_one("#activity", RichLog)
        self.call_from_thread(self._note, log, f"checking {provider} key…")
        try:
            verdict = validate_key(provider, key)
        except ChatProviderError as e:
            self.call_from_thread(self._note, log, f"{provider}: {e}", m.RED)
            return
        path = save_key(provider, key)
        session = self._chat_session()
        session.backend.provider = provider
        session.backend.model = PROVIDERS[provider][2]
        self.call_from_thread(
            self._note, log,
            f"{provider} {verdict} · saved to {path} · chat now on "
            f"{session.backend.provider}/{session.backend.model}", m.GREEN,
        )

    def _undo_last_turn(self) -> None:
        import subprocess

        log = self.query_one("#activity", RichLog)
        subject = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=self.repo,
                                 capture_output=True, text=True).stdout.strip()
        if not subject.startswith("[ratchet chat]"):
            self._note(log, f"last commit is not a chat turn ({subject[:50]!r}); refusing to touch it", m.AMBER)
            return
        r = subprocess.run(
            ["git", "-c", "user.name=ratchet-chat", "-c", "user.email=chat@ratchet.local",
             "revert", "--no-edit", "HEAD"],
            cwd=self.repo, capture_output=True, text=True,
        )
        if r.returncode == 0:
            self._note(log, f"reverted: {subject[15:66]}", m.GREEN)
        else:
            self._note(log, f"revert failed: {(r.stderr or r.stdout)[-120:]}", m.RED)

    def _start_turn(self, prompt: str) -> None:
        log = self.query_one("#activity", RichLog)
        session = self._chat_session()
        if self._chat_worker is not None and self._chat_worker.is_running:
            self._note(log, "a turn is already running — Esc to interrupt it first", m.AMBER)
            return
        self._turn_started = time.time()
        self._clear_splash(log)
        self.query_one(StatusLine).begin_work()
        if self._heartbeat is None:
            # "is it dead or just slow?" is the question that made this whole
            # session unusable; answer it once a second, on screen
            self._heartbeat = self.set_interval(1.0, self._tick_turn)
        self._step(log, "chat", f"{session.backend.provider}/{session.backend.model}", m.ACCENT)
        if session.backend.provider == "demo":
            self._note(log, "demo provider: this scaffolds a stub, it does not think — "
                            "/connect for real codegen", m.AMBER)
        from ..providers import redact as _redact

        self._note(log, _redact(prompt[:120]))
        self._chat_worker = self._run_chat_turn(prompt)

    @work(thread=True, exit_on_error=False)
    def _run_chat_turn(self, prompt: str) -> None:
        log = self.query_one("#activity", RichLog)
        session = self._chat_session()

        def emit(kind: str, text: str) -> None:
            colour = {"step": "", "note": m.AMBER, "error": m.RED, "done": m.GREEN}.get(kind, "")
            self.call_from_thread(self._note, log, text, colour)

        turn = session.run_turn(prompt, emit)
        if turn.ok:
            where = f" · commit {turn.commit}" if turn.commit else f" · {turn.commit_note or 'no commit'}"
            self.call_from_thread(
                self._note, log,
                f"done in {turn.seconds}s · {len(turn.files)} file(s){where}", m.GREEN,
            )

    # ---------------------------------------------------------------- actions --

    def _decide(self, allow: bool) -> None:
        """The decision travels as a file, so it still works if the console dies."""
        if not self.pending_id:
            return
        d = self.repo / ".ratchet" / "approvals"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{self.pending_id}.json").write_text(
            json.dumps({"allow": allow, "reason": "" if allow else "denied at the console"})
        )
        self.pending_id = None
        self.query_one(ApprovalGate).hide()
        self.query_one(WaitingOn).clear()
        self.query_one(StatusLine).state = "working"
        self._resize_banner()

    @property
    def _gate_armed(self) -> bool:
        return bool(self.pending_id) and self.query_one(ApprovalGate).display

    def action_approve(self) -> None:
        self._decide(True)

    def action_deny(self) -> None:
        # Esc: close the palette first, then interrupt a running turn, then --
        # with neither in play -- its original job of denying an approval.
        palette = self.query_one("#palette", OptionList)
        if palette.display:
            self._close_palette()
            return
        if self._chat_worker is not None and self._chat_worker.is_running:
            self._chat_session().cancel.set()
            self._chat_worker.cancel()
            self._note(self.query_one("#activity", RichLog), "interrupt requested", m.AMBER)
            return
        if self._gate_armed:
            self._decide(False)

    def action_up(self) -> None:
        palette = self.query_one("#palette", OptionList)
        if palette.display:
            palette.action_cursor_up()
            return
        if self._gate_armed:
            self.query_one(ApprovalGate).move(-1)

    def action_down(self) -> None:
        palette = self.query_one("#palette", OptionList)
        if palette.display:
            palette.action_cursor_down()
            return
        if self._gate_armed:
            self.query_one(ApprovalGate).move(1)

    def action_confirm(self) -> None:
        if self._gate_armed:
            self._decide(self.query_one(ApprovalGate).choice == 0)

    def action_follow(self) -> None:
        self.follow = not self.follow
        self.query_one("#activity", RichLog).auto_scroll = self.follow

    def action_rewind(self) -> None:
        self._note(self.query_one("#activity", RichLog),
                   "rewind is a repo operation: `ratchet rewind <node>` in another pane",
                   style=m.AMBER)

    @on(Button.Pressed, "#approve")
    def _approve_btn(self) -> None:
        self._decide(True)

    @on(Button.Pressed, "#deny")
    def _deny_btn(self) -> None:
        self._decide(False)


def run(bus_path: str, repo: str = ".") -> None:  # pragma: no cover
    RatchetApp(Path(bus_path), Path(repo)).run()
