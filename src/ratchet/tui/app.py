"""The Ratchet console.

Three questions, answerable at a glance, from across a room:

    what is it doing        the stream on the left, with sub-agent threads inline
    what is it waiting on   the gate rail and the approval bar
    what did it do          the ratchet spine: green teeth are commits that stuck,
                            red stubs are attempts that were rolled back

The approval bar is the only widget that ever takes the whole width. That is
deliberate: an irreversible action should interrupt the room, not sit politely in
a corner waiting to be noticed.

Everything is driven off the JSONL bus, so the console can be started, killed and
restarted mid-run without disturbing the agent.
"""

from __future__ import annotations

import json
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.widgets import Button, Footer, Header, Label, RichLog, Static

from ..bus import Bus

GATE_ORDER = ["cheat", "apply", "build", "f2p", "hidden", "p2p", "types", "lint", "decision"]

GATE_LABEL = {
    "cheat": "integrity",
    "apply": "patch applies",
    "build": "build",
    "f2p": "fail-to-pass",
    "hidden": "held-out",
    "p2p": "pass-to-pass",
    "types": "types",
    "lint": "lint",
    "decision": "verdict",
}


class Spine(Static):
    """The ratchet itself: a vertical run of teeth, newest at the top."""

    def __init__(self) -> None:
        super().__init__(id="spine")
        self.rows: list[tuple[str, str, str]] = []  # (glyph, label, style)

    def add_green(self, sha: str, subject: str) -> None:
        self.rows.insert(0, ("=", f"{sha[:7]}  {subject[:34]}", "bold green"))
        self.render_rows()

    def add_red(self, attempt: str, why: str) -> None:
        self.rows.insert(0, ("x", f"{attempt}  {why[:34]}", "red"))
        self.render_rows()

    def add_note(self, text: str, style: str = "yellow") -> None:
        self.rows.insert(0, ("-", text[:42], style))
        self.render_rows()

    def render_rows(self) -> None:
        t = Text()
        for glyph, label, style in self.rows[:40]:
            bar = "|" if glyph == "=" else " "
            t.append(f" {bar} ", style="dim")
            t.append(f"{glyph} ", style=style)
            t.append(label + "\n", style=style if glyph != "=" else "")
        self.update(t)


class GateRail(Static):
    """One line per gate. The whole gauntlet, always visible, never scrolled away."""

    def __init__(self) -> None:
        super().__init__(id="gates")
        self.state: dict[str, tuple[str, str]] = {}
        self.reset()

    def reset(self) -> None:
        self.state = {g: ("pending", "") for g in GATE_ORDER}
        self.draw()

    def set(self, gate: str, passed: bool, detail: str) -> None:
        self.state[gate] = ("pass" if passed else "fail", detail)
        self.draw()

    def draw(self) -> None:
        t = Text()
        for g in GATE_ORDER:
            status, detail = self.state.get(g, ("pending", ""))
            mark, style = {
                "pass": ("PASS", "bold green"),
                "fail": ("FAIL", "bold red"),
                "pending": ("....", "dim"),
            }[status]
            t.append(f" {mark} ", style=style)
            t.append(f"{GATE_LABEL[g]:<14}", style="bold" if status != "pending" else "dim")
            t.append(f"{detail[:44]}\n", style="dim")
        self.update(t)


class Scoreboard(Static):
    def __init__(self) -> None:
        super().__init__(id="scoreboard")
        self.update(Text("no candidates yet", style="dim"))

    def show(self, rows: list[dict]) -> None:
        t = Text()
        t.append(f"{'cand':<9}{'score':>7}{'hidden':>8}{'vis':>6}{'p2p':>6}{'gap':>6}  flags\n", style="bold")
        for i, r in enumerate(rows):
            style = "bold green" if i == 0 else ""
            if r.get("findings"):
                style = "bold red"
            t.append(
                f"{r['label']:<9}{r['score']:>7.3f}{r['hidden']:>8.2f}{r['visible']:>6.2f}"
                f"{r['p2p']:>6.2f}{r['delta']:>6.2f}  {','.join(r.get('findings') or []) or '-'}\n",
                style=style,
            )
        self.update(t)


class ApprovalBar(Vertical):
    """Full width, unmissable, and it blocks. Nothing irreversible happens behind it."""

    visible_flag = reactive(False)

    def compose(self) -> ComposeResult:
        yield Label("", id="approval-title")
        yield Static("", id="approval-body")
        with Horizontal(id="approval-buttons"):
            yield Button("Approve  (a)", variant="success", id="approve")
            yield Button("Deny  (d)", variant="error", id="deny")

    def show(self, tool: str, arguments: dict, extra: str = "") -> None:
        self.add_class("armed")
        self.query_one("#approval-title", Label).update(
            Text(f"  HOLD -- the agent wants to run an irreversible action: {tool}", style="bold black on yellow")
        )
        body = json.dumps(arguments, indent=2)[:1200] if arguments else "(no arguments)"
        self.query_one("#approval-body", Static).update(Text(body + ("\n\n" + extra if extra else ""), style="yellow"))
        self.display = True

    def hide(self) -> None:
        self.remove_class("armed")
        self.display = False


class RatchetApp(App):
    CSS_PATH = "theme.tcss"
    TITLE = "ratchet"
    SUB_TITLE = "the tests decide when it is done"

    BINDINGS = [
        Binding("a", "approve", "Approve"),
        Binding("d", "deny", "Deny"),
        Binding("q", "quit", "Quit"),
        Binding("f", "follow", "Follow"),
    ]

    def __init__(self, bus_path: Path, repo: Path) -> None:
        super().__init__()
        self.bus = Bus(bus_path)
        self.repo = repo
        self.pending_call: str | None = None
        self.follow = True

    # ------------------------------------------------------------ compose --

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Label("  agent", classes="pane-title")
                yield RichLog(id="stream", wrap=True, markup=False, highlight=False)
            with Vertical(id="right"):
                yield Label("  gauntlet", classes="pane-title")
                yield GateRail()
                yield Label("  ratchet", classes="pane-title")
                with VerticalScroll(id="spine-scroll"):
                    yield Spine()
                yield Label("  candidates", classes="pane-title")
                yield Scoreboard()
        yield ApprovalBar(id="approval")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#approval", ApprovalBar).display = False
        self.set_interval(0.25, self.drain)

    # ------------------------------------------------------------- events --

    def drain(self) -> None:
        log = self.query_one("#stream", RichLog)
        gates = self.query_one(GateRail)
        spine = self.query_one(Spine)
        for ev in self.bus.tail():
            p = ev.payload
            k = ev.kind
            if k == "run.started":
                self.sub_title = f"{p.get('task')}  ·  {p.get('backend')} backend  ·  {p.get('trunk')}"
                log.write(Text(f"run {p.get('run_id')} started from {str(p.get('base'))[:10]}", style="bold"))
            elif k == "agent.text":
                thread = p.get("thread", "main")
                style = "" if thread == "main" else "cyan"
                prefix = "" if thread == "main" else f"[{thread[:8]}] "
                log.write(Text(prefix + p.get("text", ""), style=style))
            elif k == "agent.tool":
                log.write(Text(f"  -> {p.get('tool')}  {'ok' if p.get('ok') else 'error'}", style="dim"))
            elif k == "attempt.submitted":
                gates.reset()
                log.write(Text(f"\nattempt {p.get('attempt')} on {p.get('branch')}: {p.get('rationale','')}", style="bold"))
            elif k == "gate.result":
                gates.set(p.get("gate", ""), bool(p.get("passed")), p.get("detail", ""))
            elif k == "verdict":
                self.on_verdict(p, log, spine)
            elif k == "rollback":
                log.write(Text(f"  rolled back to {p.get('to')}  ({p.get('reason')})", style="bold red"))
            elif k == "stall":
                spine.add_note(f"stalled after {p.get('attempts')} rejections", "bold yellow")
                log.write(Text(f"\n[stall] {p.get('attempts')} consecutive rejections -- fanning out\n", style="bold yellow"))
            elif k == "fanout":
                spine.add_note(f"fan-out: {', '.join(p.get('labels', []))}", "cyan")
                log.write(Text(f"[fan-out] {', '.join(p.get('labels', []))} from {p.get('base')}", style="cyan"))
            elif k == "arbitration":
                self.query_one(Scoreboard).show(p.get("rows", []))
            elif k == "docs.fetch":
                log.write(Text(f"  [bright data] {p.get('library')} {p.get('version')} via {p.get('via')}", style="magenta"))
            elif k == "docs.heal":
                log.write(
                    Text(
                        f"  [scraper repaired] {p.get('library')}: section {p.get('old_section')!r} -> {p.get('new_section')!r}",
                        style="bold magenta",
                    )
                )
                spine.add_note(f"scraper repaired: {p.get('library')}", "magenta")
            elif k == "approval.required":
                self.pending_call = p.get("tool_call_id")
                self.query_one("#approval", ApprovalBar).show(
                    p.get("tool", "?"), p.get("arguments") or {}, "This leaves the machine. Nothing has been pushed yet."
                )
                self.bell()
            elif k == "approval.resolved":
                self.query_one("#approval", ApprovalBar).hide()
                log.write(Text(f"  approval {'granted' if p.get('approved') else 'denied'}", style="bold"))
            elif k == "run.done":
                log.write(Text(f"\nrun finished: {p.get('status')}", style="bold green"))

    def on_verdict(self, p: dict, log: RichLog, spine: Spine) -> None:
        decision = p.get("decision")
        score = p.get("score", 0.0)
        if p.get("dry_run"):
            log.write(Text(f"  dry run: score {score:.3f} (nothing committed)", style="dim"))
            return
        if decision == "accepted":
            spine.add_green(p.get("commit_sha") or "", f"score {score:.3f}")
            log.write(Text(f"  ACCEPTED  score {score:.3f}  ratchet advanced", style="bold green"))
        elif decision == "disqualified":
            rules = ",".join(f["rule"] for f in p.get("findings", []))
            spine.add_red(p.get("attempt_id", ""), f"DQ {rules}")
            log.write(Text(f"  DISQUALIFIED  {rules}", style="bold white on red"))
        elif decision == "infra_failure":
            spine.add_note(f"infra failure on {p.get('attempt_id')}", "yellow")
        else:
            spine.add_red(p.get("attempt_id", ""), f"score {score:.3f}")
            log.write(Text(f"  REJECTED  score {score:.3f}  delta {p.get('delta', 0):.2f}", style="red"))

    # ------------------------------------------------------------ actions --

    def _decide(self, allow: bool) -> None:
        if not self.pending_call:
            return
        d = self.repo / ".ratchet" / "approvals"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{self.pending_call}.json").write_text(json.dumps({"allow": allow, "reason": "" if allow else "denied at the console"}))
        self.pending_call = None
        self.query_one("#approval", ApprovalBar).hide()

    def action_approve(self) -> None:
        self._decide(True)

    def action_deny(self) -> None:
        self._decide(False)

    def action_follow(self) -> None:
        self.follow = not self.follow

    @on(Button.Pressed, "#approve")
    def _approve_btn(self) -> None:
        self._decide(True)

    @on(Button.Pressed, "#deny")
    def _deny_btn(self) -> None:
        self._decide(False)


def run(bus_path: str, repo: str = ".") -> None:  # pragma: no cover
    RatchetApp(Path(bus_path), Path(repo)).run()
