"""The console: a stream, not a screen.

The previous console painted fixed panes and hid the ones that would not fit,
which meant an ordinary 80-column terminal silently lost the search tree, the
gauntlet rail and the waiting-on panel -- and the tool read as dead while it was
working. A stream cannot do that. Lines arrive, scroll, and stay; every width
works; you can scroll back; and piping it to a file gives the same story.

Everything here renders `bus.Event`s, so the same renderer serves a live run, a
replay, and the pipeline demo. Nothing in this module knows how work is produced.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from rich.console import Console
from rich.rule import Rule
from rich.text import Text

from .bus import Event

# one palette, shared with the dashboard, so a screenshot of either is the same product
from .design import ACCENT, AMBER, DIM, GREEN, MUTED, RED, VIOLET  # noqa: E402

STAGE_LABEL = {
    "build": "build / install", "cheat": "cheat check", "f2p": "fail-to-pass",
    "p2p": "pass-to-pass", "types": "type check", "lint": "lint",
    "hygiene": "diff hygiene", "apply": "patch applies",
}


@dataclass
class Totals:
    """What the run has cost and produced so far -- printed at the end, and on
    demand, rather than kept in a box that has to fit somewhere."""

    started: float = field(default_factory=time.time)
    subagents: int = 0
    sandboxes: int = 0
    nodes: int = 0
    pruned: int = 0
    files: set[str] = field(default_factory=set)
    commits: list[str] = field(default_factory=list)
    findings: int = 0
    reviews: int = 0
    approvals: int = 0

    def line(self) -> str:
        secs = int(time.time() - self.started)
        bits = [f"{secs // 60}m{secs % 60:02d}s"]
        if self.nodes:
            bits.append(f"{self.nodes} node(s), {self.pruned} pruned")
        if self.subagents:
            bits.append(f"{self.subagents} sub-agent(s)")
        if self.files:
            bits.append(f"{len(self.files)} file(s)")
        if self.commits:
            bits.append(f"{len(self.commits)} commit(s)")
        if self.reviews:
            bits.append(f"{self.reviews} review(s), {self.findings} finding(s)")
        return " · ".join(bits)


class StreamConsole:
    """Turns bus events into lines. One method per family of event, so adding a
    new kind is adding one branch and nothing else moves."""

    def __init__(self, console: Console | None = None) -> None:
        self.out = console or Console(highlight=False, soft_wrap=True)
        self.totals = Totals()
        self._t0 = time.time()

    # ------------------------------------------------------------- primitives --

    def _stamp(self) -> Text:
        secs = time.time() - self._t0
        return Text(f"{int(secs // 60):02d}:{int(secs % 60):02d} ", style=DIM)

    def step(self, verb: str, detail: str = "", colour: str = ACCENT) -> None:
        t = self._stamp()
        t.append("● ", style=f"bold {colour}")
        t.append(verb, style="bold white")
        if detail:
            t.append(f"  {detail}", style=MUTED)
        self.out.print(t)

    def note(self, body: str, colour: str = MUTED, indent: int = 6) -> None:
        t = self._stamp()
        t.append(" " * (indent - 6) + "⎿  ", style=DIM)
        t.append(body, style=colour)
        self.out.print(t)

    def rule(self, title: str) -> None:
        self.out.print(Rule(Text(title, style=f"bold {ACCENT}"), style=DIM))

    # ------------------------------------------------------------------ events --

    def handle(self, ev: Event) -> None:
        p, k = ev.payload, ev.kind
        fn = getattr(self, f"_on_{k.replace('.', '_')}", None)
        if fn:
            fn(p)

    # run lifecycle
    def _on_run_started(self, p: dict) -> None:
        self.rule(f"run {p.get('run_id', '')}  ·  {p.get('task', '')}")
        self.note(f"provider {p.get('provider', '?')}"
                  + ("  ·  snapshots" if p.get("snapshots") else "  ·  worktrees"), DIM)

    def _on_repo_mapped(self, p: dict) -> None:
        self.totals.subagents += 1
        self.step("Cartographer", f"mapped the repository ({p.get('lines', 0)} lines)", VIOLET)

    def _on_sandbox_created(self, p: dict) -> None:
        self.totals.sandboxes += 1

    def _on_expand(self, p: dict) -> None:
        self.step("Expand", f"node {p.get('node', '')} · fanout {p.get('fanout', 1)}"
                            f" · {p.get('dead_ends', 0)} dead end(s) in context")

    def _on_verify_started(self, p: dict) -> None:
        self.totals.subagents += 1
        self.step("Verify", f"{p.get('label', '')}  ·  {p.get('model', '')}", VIOLET)
        if p.get("intent"):
            self.note(str(p["intent"])[:110])

    def _on_stage_result(self, p: dict) -> None:
        stage = str(p.get("stage", ""))
        if p.get("skipped"):
            return                                   # a skipped stage is not news
        ok = bool(p.get("passed"))
        mark = "PASS" if ok else "FAIL"
        self.note(f"{mark:<5}{STAGE_LABEL.get(stage, stage):<16}{p.get('detail', '')}",
                  GREEN if ok else RED)

    def _on_node_added(self, p: dict) -> None:
        self.totals.nodes += 1
        green = p.get("green")
        self.note(f"kept {p.get('id', '')} at {float(p.get('score', 0)):.2f}"
                  + ("   ★ every gate green" if green else ""), GREEN if green else MUTED)

    def _on_node_pruned(self, p: dict) -> None:
        self.totals.nodes += 1
        self.totals.pruned += 1
        self.note(f"pruned {p.get('id', '')} — {p.get('reason', '')}", RED)

    def _on_stall(self, p: dict) -> None:
        self.step("Stall", f"no improvement for 3 expansions — forking {p.get('fanout', 3)} ways "
                           f"from {p.get('node', '')}", AMBER)

    # chat / agentic session
    def _on_chat_turn(self, p: dict) -> None:
        self.rule(f"turn  ·  {p.get('provider', '')}/{p.get('model', '')}")
        self.note(str(p.get("prompt", ""))[:140], "white")

    def _on_chat_step(self, p: dict) -> None:
        self.note(str(p.get("text", ""))[:140])

    def _on_chat_done(self, p: dict) -> None:
        files = p.get("files") or []
        self.totals.files.update(files)
        if p.get("commit"):
            self.totals.commits.append(str(p["commit"]))
        if p.get("error"):
            self.note(str(p["error"])[:140], RED)
        else:
            self.note(f"done in {p.get('seconds', 0)}s · {len(files)} file(s)"
                      + (f" · commit {p.get('commit')}" if p.get("commit") else ""), GREEN)

    # review
    def _on_review_started(self, p: dict) -> None:
        self.totals.reviews += 1
        self.step("Qodo", f"reviewing {p.get('pr', '')}  ·  {p.get('files', 0)} file(s) changed", VIOLET)

    def _on_review_finding(self, p: dict) -> None:
        self.totals.findings += 1
        sev = str(p.get("severity", "medium")).lower()
        colour = {"high": RED, "critical": RED, "medium": AMBER}.get(sev, MUTED)
        self.note(f"[{sev}] {p.get('title', '')}", colour)
        if p.get("detail"):
            self.note(str(p["detail"])[:130], DIM, indent=9)

    def _on_review_done(self, p: dict) -> None:
        n = int(p.get("findings", 0))
        self.note(f"review complete — {n} finding(s)" if n else "review complete — nothing to fix",
                  AMBER if n else GREEN)

    def _on_fix_started(self, p: dict) -> None:
        self.step("Fix", f"addressing {p.get('finding', '')}", ACCENT)

    def _on_fix_done(self, p: dict) -> None:
        self.note(f"{p.get('summary', 'fixed')}", GREEN)

    # gate + shipping
    def _on_approval_required(self, p: dict) -> None:
        self.totals.approvals += 1
        self.step("Gate", f"{p.get('action', '')} — {p.get('summary', '')}", AMBER)
        stats = p.get("stats") or {}
        if stats:
            self.note(" · ".join(f"{k} {v}" for k, v in stats.items()), DIM)

    def _on_approval_resolved(self, p: dict) -> None:
        ok = bool(p.get("approved"))
        self.note("approved" if ok else f"denied — {p.get('reason', '')}", GREEN if ok else RED)

    def _on_pr_opened(self, p: dict) -> None:
        self.step("Pull request", f"{p.get('url', p.get('pr', ''))}  ·  {p.get('title', '')}", ACCENT)

    def _on_pr_merged(self, p: dict) -> None:
        self.step("Merged", str(p.get("pr", "")), GREEN)

    def _on_run_done(self, p: dict) -> None:
        green = p.get("green")
        self.out.print()
        self.rule("done" if green else "stopped")
        self.note(str(p.get("reason", "")), GREEN if green else MUTED)
        self.note(self.totals.line(), DIM)
