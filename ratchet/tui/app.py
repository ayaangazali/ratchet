"""The Ratchet console.

Split pane, and each half answers a different question a stranger would ask:

    left    where has the search been      the tree, with scores, live and pruned
    right   what is it doing right now     the current stage of the gauntlet, and the
                                           alerts from anything it just pruned
    bottom  what is it costing, and what   budget, and the two keys that matter
            can I do about it

The ambient counters -- subagents spawned, sandboxes live, approvals pending -- stay
on screen at all times. They are free proof that the harness is loaded, in every
screenshot anyone takes.

Everything renders off the JSONL bus, so the console can be started, killed and
restarted mid-run without disturbing the search, and a finished run can be replayed
into it. Build it against `make fixture` before you point it at a live run.
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


class TreePane(Static):
    """The search tree. Newest branch highlighted, pruned nodes struck through."""

    def __init__(self) -> None:
        super().__init__(id="tree")
        self.nodes: dict[str, dict] = {}
        self.order: list[str] = []
        self.live: str | None = None
        self.draw()

    def upsert(self, node: dict, *, pruned: bool = False) -> None:
        nid = node.get("id") or "?"
        node = {**self.nodes.get(nid, {}), **node, "pruned": pruned}
        if nid not in self.nodes:
            self.order.append(nid)
        self.nodes[nid] = node
        if not pruned:
            self.live = nid
        self.draw()

    def draw(self) -> None:
        t = Text()
        if not self.nodes:
            t.append("  waiting for the root node...\n", style="dim")
        children: dict[str | None, list[str]] = {}
        for nid in self.order:
            children.setdefault(self.nodes[nid].get("parent"), []).append(nid)

        def walk(nid: str, prefix: str, last: bool, root: bool) -> None:
            n = self.nodes[nid]
            pruned, green = n.get("pruned"), n.get("green")
            glyph = "✗" if pruned else ("★" if green else "●")
            style = "red" if pruned else ("bold green" if green else ("bold yellow" if nid == self.live else "white"))
            elbow = "" if root else ("└─" if last else "├─")
            t.append(f"{prefix}{elbow}", style="dim")
            t.append(f"{glyph} ", style=style)
            t.append(f"{nid:<6}", style=style)
            t.append(f"{n.get('score', 0):.2f}", style="dim" if pruned else "")
            if nid == self.live and not pruned:
                t.append("  ←live", style="bold yellow")
            elif pruned:
                t.append(f"  {(n.get('reason') or 'pruned')[:26]}", style="red")
            elif green:
                t.append("  ✓green", style="bold green")
            if n.get("model"):
                t.append(f"  {n['model'].split('/')[-1][:14]}", style="dim cyan")
            t.append("\n")
            kids = children.get(nid, [])
            for i, kid in enumerate(kids):
                walk(kid, prefix + ("" if root else ("   " if last else "│  ")), i == len(kids) - 1, False)

        roots = children.get(None, [])
        for r in roots:
            walk(r, "", True, True)
        self.update(t)


class Counters(Static):
    """The ambient proof that the harness is doing the work."""

    def __init__(self) -> None:
        super().__init__(id="counters")
        self.subagents = 0
        self.sandboxes_live = 0
        self.sandboxes_total = 0
        self.approvals = 0
        self.provider = "-"
        self.draw()

    def draw(self) -> None:
        t = Text()
        t.append(f" subagents {self.subagents}", style="cyan")
        t.append(" · ", style="dim")
        t.append(f"sandboxes {self.sandboxes_live} live/{self.sandboxes_total}", style="cyan")
        t.append(" · ", style="dim")
        t.append(f"approvals {self.approvals}", style="bold yellow" if self.approvals else "cyan")
        t.append(f"\n provider {self.provider}", style="dim")
        self.update(t)


class StagePane(Static):
    """The gauntlet, in order, never scrolled off screen."""

    def __init__(self) -> None:
        super().__init__(id="stages")
        self.reset()

    def reset(self, label: str = "") -> None:
        self.label = label
        self.state: dict[str, tuple[str, str]] = {s: ("pending", "") for s in STAGES}
        self.draw()

    def set(self, stage: str, passed: bool, detail: str, skipped: bool = False) -> None:
        self.state[stage] = ("skip" if skipped else ("pass" if passed else "fail"), detail)
        self.draw()

    def draw(self) -> None:
        done = sum(1 for s in STAGES if self.state.get(s, ("pending", ""))[0] != "pending")
        t = Text()
        t.append(f" stage {done}/{len(STAGES)}", style="bold")
        if self.label:
            t.append(f" · {self.label}", style="dim")
        t.append("\n\n")
        for s in STAGES:
            status, detail = self.state.get(s, ("pending", ""))
            mark, style = {
                "pass": ("PASS", "bold green"),
                "fail": ("FAIL", "bold red"),
                "skip": ("skip", "dim"),
                "pending": ("....", "dim"),
            }[status]
            t.append(f" {mark:5}", style=style)
            t.append(f"{STAGE_LABEL[s]:<16}", style="bold" if status in ("pass", "fail") else "dim")
            t.append(f"{detail[:34]}\n", style="dim")
        self.update(t)


class ApprovalBar(Vertical):
    """Full width, and it blocks. Nothing irreversible happens behind it."""

    armed = reactive(False)

    def compose(self) -> ComposeResult:
        yield Label("", id="approval-title")
        yield Static("", id="approval-body")
        with Horizontal(id="approval-buttons"):
            yield Button("Approve  (a)", variant="success", id="approve")
            yield Button("Deny  (d)", variant="error", id="deny")

    def show(self, payload: dict) -> None:
        self.add_class("armed")
        self.query_one("#approval-title", Label).update(
            Text(f"  HOLD — {payload.get('action', 'irreversible action')}: {payload.get('summary', '')}",
                 style="bold black on yellow")
        )
        stats = payload.get("stats") or {}
        body = Text()
        body.append(f"{json.dumps(stats)}\n\n", style="yellow")
        body.append((payload.get("diff_preview") or "")[:1400], style="")
        self.query_one("#approval-body", Static).update(body)
        self.display = True

    def hide(self) -> None:
        self.remove_class("armed")
        self.display = False


class RatchetApp(App):
    CSS_PATH = "theme.tcss"
    TITLE = "ratchet"
    SUB_TITLE = "the agent doesn't decide it's done"

    BINDINGS = [
        Binding("a", "approve", "Approve"),
        Binding("d", "deny", "Deny"),
        Binding("r", "rewind", "Rewind"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, bus_path: Path, repo: Path) -> None:
        super().__init__()
        self.bus = Bus(bus_path)
        self.repo = Path(repo)
        self.pending_id: str | None = None
        self.budget_line = "budget: —"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Label("  tree", classes="pane-title")
                with VerticalScroll(id="tree-scroll"):
                    yield TreePane()
                yield Counters()
            with Vertical(id="right"):
                yield Label("  verifier", classes="pane-title")
                yield StagePane()
                yield Label("  events", classes="pane-title")
                yield RichLog(id="log", wrap=True, markup=False, highlight=False)
        yield ApprovalBar(id="approval")
        yield Static(self.budget_line, id="budget")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#approval", ApprovalBar).display = False
        self.set_interval(0.2, self.drain)

    # ------------------------------------------------------------------ bus --

    def drain(self) -> None:
        tree = self.query_one(TreePane)
        stages = self.query_one(StagePane)
        counters = self.query_one(Counters)
        log = self.query_one("#log", RichLog)

        for ev in self.bus.tail():
            p, k = ev.payload, ev.kind
            if k == "run.started":
                self.sub_title = f"{p.get('task')} · {p.get('provider')} sandboxes"
                counters.provider = f"{p.get('provider')}" + ("" if p.get("snapshots") else " (no snapshots)")
                counters.draw()
                self._budget(p.get("budget"))
                log.write(Text(f"run {p.get('run_id')} started", style="bold"))
            elif k == "repo.mapped":
                counters.subagents += 1
                counters.draw()
                log.write(Text(f"  cartographer mapped the repo ({p.get('lines')} lines)", style="cyan"))
            elif k == "sandbox.created":
                counters.sandboxes_live += 1
                counters.sandboxes_total += 1
                counters.draw()
            elif k == "expand":
                log.write(Text(f"\nexpand {p.get('node')} · fanout {p.get('fanout')} · "
                               f"{p.get('dead_ends', 0)} dead end(s) in context", style="bold"))
            elif k == "verify.started":
                stages.reset(f"{p.get('intent', '')[:38]}")
                counters.subagents += 1
                counters.draw()
                log.write(Text(f"  {p.get('model', '')}: {p.get('intent', '')[:60]}", style="dim cyan"))
            elif k == "stage.result":
                stages.set(p.get("stage", ""), bool(p.get("passed")), p.get("detail", ""), bool(p.get("skipped")))
            elif k == "node.added":
                tree.upsert(p)
                counters.sandboxes_live = max(0, counters.sandboxes_live - 1)
                counters.draw()
                style = "bold green" if p.get("green") else "green"
                log.write(Text(f"  kept {p.get('id')} score {p.get('score', 0):.2f}"
                               + ("  GREEN" if p.get("green") else ""), style=style))
            elif k == "node.pruned":
                tree.upsert(p, pruned=True)
                counters.sandboxes_live = max(0, counters.sandboxes_live - 1)
                counters.draw()
                reason = p.get("reason") or p.get("outcome")
                findings = ",".join(p.get("findings") or [])
                log.write(Text(f"  ⚠ {p.get('id')} pruned: {reason}" + (f" [{findings}]" if findings else ""),
                               style="bold red"))
            elif k == "stall":
                log.write(Text(f"\n[stall] no improvement for 3 expansions — forking {p.get('fanout')} ways "
                               f"from {p.get('node')} at depth {p.get('depth')}", style="bold yellow"))
            elif k == "docs.fetch":
                log.write(Text(f"  [bright data] {p.get('library')} {p.get('version')}", style="magenta"))
            elif k == "docs.heal":
                log.write(Text(f"  [scraper repaired] {p.get('library')}: "
                               f"{p.get('old_section')!r} → {p.get('new_section')!r}", style="bold magenta"))
            elif k == "approval.required":
                self.pending_id = p.get("id")
                counters.approvals += 1
                counters.draw()
                self.query_one("#approval", ApprovalBar).show(p)
                self.bell()
            elif k == "approval.resolved":
                counters.approvals = max(0, counters.approvals - 1)
                counters.draw()
                self.query_one("#approval", ApprovalBar).hide()
                log.write(Text(f"  approval {'granted' if p.get('approved') else 'denied'}", style="bold"))
            elif k == "run.done":
                self._budget(p.get("budget"))
                log.write(Text(f"\nfinished: {p.get('reason')} · winner {p.get('winner')} "
                               f"score {p.get('score', 0):.2f}", style="bold green" if p.get("green") else "bold"))

    def _budget(self, b: dict | None) -> None:
        if not b:
            return
        m, s = divmod(int(b.get("elapsed", 0)), 60)
        self.budget_line = (
            f" budget: {b.get('nodes_used', 0)}/{b.get('max_nodes', 0)} nodes · "
            f"{m}m{s:02d}s · ${b.get('usd_used', 0):.2f} of ${b.get('max_usd', 0):.2f}"
        )
        self.query_one("#budget", Static).update(self.budget_line)

    # -------------------------------------------------------------- actions --

    def _decide(self, allow: bool) -> None:
        if not self.pending_id:
            return
        d = self.repo / ".ratchet" / "approvals"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{self.pending_id}.json").write_text(
            json.dumps({"allow": allow, "reason": "" if allow else "denied at the console"})
        )
        self.pending_id = None
        self.query_one("#approval", ApprovalBar).hide()

    def action_approve(self) -> None:
        self._decide(True)

    def action_deny(self) -> None:
        self._decide(False)

    def action_rewind(self) -> None:
        self.query_one("#log", RichLog).write(
            Text("  rewind: pick a node with `ratchet rewind <id>` in another pane", style="yellow")
        )

    @on(Button.Pressed, "#approve")
    def _approve_btn(self) -> None:
        self._decide(True)

    @on(Button.Pressed, "#deny")
    def _deny_btn(self) -> None:
        self._decide(False)


def run(bus_path: str, repo: str = ".") -> None:  # pragma: no cover
    RatchetApp(Path(bus_path), Path(repo)).run()
