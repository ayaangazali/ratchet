"""The view for `ratchet build`: animated while it works, permanent when it lands.

Each stage that takes time shows a swimming line that updates in place; when the
stage resolves the animation is replaced by a single line that stays. So the
transcript you scroll back through is clean, and the thing you watch is alive.
Nothing lives in a fixed pane, so nothing can be hidden by a narrow terminal.
"""

from __future__ import annotations

import time

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

from . import shark as sk
from .bus import Event

STAGE_LABEL = {"cheat": "cheat check", "build": "build / install", "f2p": "fail-to-pass",
               "p2p": "pass-to-pass", "types": "type check", "lint": "lint",
               "hygiene": "diff hygiene"}
SEV_COLOUR = {"critical": sk.RED, "high": sk.RED, "medium": sk.AMBER, "low": sk.MUTED}


class BuildView:
    def __init__(self, console: Console | None = None, *, animate: bool = True,
                 label_demo: bool = False) -> None:
        self.out = console or Console(highlight=False)
        self.animate = animate and self.out.is_terminal
        # The event stream always records `demo: true`; whether the screen says so
        # is a presentation choice, and this is the switch. Off while showing what
        # the product is meant to be, on when the distinction matters.
        self.label_demo = label_demo
        self.live: Live | None = None
        self.swimmer: sk.Swimmer | None = None
        self.t0 = time.time()
        self.findings: list[dict] = []
        self.nodes: dict[str, str] = {}

    # ------------------------------------------------------------ primitives --

    def _stop(self) -> None:
        if self.live:
            self.live.stop()
            self.live = None
            self.swimmer = None

    def start(self, label: str, detail: str = "") -> None:
        """Begin an animated stage. Ends when anything else is printed."""
        self._stop()
        if not self.animate:
            return
        self.swimmer = sk.Swimmer(label, detail)
        self.live = Live(self.swimmer.frame(), console=self.out, refresh_per_second=12,
                         transient=True)
        self.live.start()

    def pump(self) -> None:
        if self.live and self.swimmer:
            self.live.update(self.swimmer.frame())

    def head(self, verb: str, detail: str = "", colour: str = sk.BRIGHT) -> None:
        self._stop()
        t = Text("  ")
        t.append("◆ ", style=f"bold {colour}")
        t.append(verb, style=f"bold {sk.TEXT}")
        if detail:
            t.append(f"  {detail}", style=sk.MUTED)
        self.out.print(t)

    def line(self, body: str, colour: str = sk.MUTED, indent: int = 4) -> None:
        self._stop()
        t = Text(" " * indent)
        t.append("· ", style=sk.DIM)
        t.append(body, style=colour)
        self.out.print(t)

    def rule(self, text: str) -> None:
        self._stop()
        self.out.print()
        self.out.print(Text(f"  ── {text} ", style=f"bold {sk.FIN}"))

    # ---------------------------------------------------------------- events --

    def handle(self, ev: Event) -> None:
        fn = getattr(self, f"_on_{ev.kind.replace('.', '_')}", None)
        if fn:
            fn(ev.payload)

    def _on_build_started(self, p: dict) -> None:
        self.out.print(sk.banner(p.get("goal", "")))
        if p.get("demo") and self.label_demo:
            self.line("demo — scripted stages over a real event stream", sk.AMBER)
        self.rule("intake")
        kind = p.get("target")
        if kind == "paper":
            self.head("Research paper", str(p.get("goal", "")))
        elif kind == "issue":
            self.head("Issue", f"{p.get('repo')} {p.get('issue')}")
        elif kind == "repo":
            self.head("Repository", str(p.get("repo")))
        else:
            self.head("Goal", str(p.get("goal"))[:90])

    def _on_issue_read(self, p: dict) -> None:
        self.line(str(p.get("title", "")), sk.TEXT)
        self.line(str(p.get("body", ""))[:150], sk.DIM)

    def _on_paper_read(self, p: dict) -> None:
        self.head(str(p.get("ident", "paper")), str(p.get("title", ""))[:80])
        self.line(str(p.get("claim", ""))[:170], sk.TEXT)
        self.line(f"to reproduce: {p.get('reproduce', '')}", sk.MUTED)

    def _on_paper_method(self, p: dict) -> None:
        self.rule("method")
        for i, step in enumerate(p.get("steps") or [], 1):
            self.line(f"{i}. {step}", sk.TEXT)
        out = p.get("out_of_scope") or []
        if out:
            self.line(f"out of scope: {', '.join(out)}", sk.DIM)

    def _on_reproduce_started(self, p: dict) -> None:
        self.rule("reproduction")
        self.head("Claim", str(p.get("claim", "")))
        self.start("reproducing", f"{p.get('runs', 0)} runs")

    def _on_reproduce_result(self, p: dict) -> None:
        ok = bool(p.get("matches"))
        self.line(f"measured {p.get('measured')}  ·  paper claims {p.get('claimed')}  "
                  f"({p.get('tolerance')})", sk.GREEN if ok else sk.RED)
        if p.get("note"):
            self.line(str(p["note"]), sk.DIM)
        self.line("reproduced" if ok else "does not reproduce — the build does not ship",
                  sk.GREEN if ok else sk.RED)

    def _on_graph_planned(self, p: dict) -> None:
        self.rule("objective graph")
        nodes = p.get("nodes") or []
        for n in nodes:
            deps = n.get("deps") or []
            after = f"  after {', '.join(deps)}" if deps else "  (no dependencies)"
            self.head(n["id"], f"{n['tests']} test(s){after}", sk.BRIGHT)
            self.line(str(n.get("goal", ""))[:100], sk.MUTED)
        self.line(f"{len(nodes)} node(s); a node is fulfilled by its tests and nothing else", sk.DIM)

    def _on_wave_started(self, p: dict) -> None:
        ids = p.get("nodes") or []
        if len(ids) > 1:
            self.rule(f"working {len(ids)} nodes in parallel — {', '.join(ids)}")
        else:
            self.rule(f"working {ids[0] if ids else ''}")

    def _on_node_started(self, p: dict) -> None:
        self.head(str(p.get("id")), str(p.get("goal", ""))[:80])
        self.start(f"{p.get('id')}", f"sub-agent on {p.get('model', '')}")

    def _on_sandbox_created(self, p: dict) -> None:
        self.line(f"sandbox {p.get('label')} · {p.get('provider')}"
                  + (" · snapshot" if p.get("snapshot") else ""), sk.DIM)

    def _on_verify_started(self, p: dict) -> None:
        self.line(f"{p.get('model', '')}  {p.get('intent', '')}"[:110], sk.MUTED)
        self.start("verifying", str(p.get("label", "")))

    def _on_stage_result(self, p: dict) -> None:
        ok = bool(p.get("passed"))
        stage = STAGE_LABEL.get(str(p.get("stage")), str(p.get("stage")))
        self.line(f"{'PASS' if ok else 'FAIL':<5}{stage:<16}{p.get('detail', '')}",
                  sk.GREEN if ok else sk.RED, indent=6)

    def _on_node_pruned(self, p: dict) -> None:
        self.line(f"pruned — {p.get('reason', '')}", sk.RED, indent=6)

    def _on_node_fulfilled(self, p: dict) -> None:
        self.line(f"fulfilled — {p.get('tests')} test(s) green", sk.GREEN, indent=6)

    def _on_review_started(self, p: dict) -> None:
        pass_no = p.get("pass_no", 1)
        self.rule(f"qodo review (mcp) — pass {pass_no}"
                  + (", before the commit exists" if pass_no == 1 else ", after the fixes"))
        if p.get("scripted") and self.label_demo:
            self.line("scripted reviewer: the qodo CLI is not installed here", sk.AMBER)
        self.start("reviewing the diff", "qodo · review_diff")

    def _on_review_finding(self, p: dict) -> None:
        self.findings.append(p)
        sev = str(p.get("severity", "medium"))
        self.head(f"[{sev}]", str(p.get("title", ""))[:80], SEV_COLOUR.get(sev, sk.MUTED))
        self.line(str(p.get("detail", ""))[:150], sk.DIM)
        where = p.get("path")
        if where:
            self.line(f"{where}:{p.get('line', 0)}   → {p.get('fix', '')}", sk.MUTED)

    def _on_review_done(self, p: dict) -> None:
        n, blocking = int(p.get("findings", 0)), int(p.get("blocking", 0))
        if not n:
            self.line("clean — nothing blocking the commit", sk.GREEN)
        else:
            self.line(f"{n} finding(s), {blocking} blocking — none of this is committed yet",
                      sk.AMBER)

    def _on_fix_started(self, p: dict) -> None:
        self.head("Fix", str(p.get("title", ""))[:80], sk.BRIGHT)
        self.start("fixing", str(p.get("path", "")))

    def _on_fix_done(self, p: dict) -> None:
        self.line(str(p.get("summary", "fixed")), sk.GREEN, indent=6)

    def _on_approval_required(self, p: dict) -> None:
        self.rule("the gate")
        self.head("Approval", str(p.get("action", "")), sk.AMBER)
        self.line(str(p.get("summary", "")), sk.TEXT)
        stats = p.get("stats") or {}
        if stats:
            self.line(" · ".join(f"{k}: {v}" for k, v in stats.items()), sk.DIM)
        self.start("waiting on a human", "nothing irreversible happens without one")

    def _on_approval_resolved(self, p: dict) -> None:
        self.line("approved" if p.get("approved") else "denied", sk.GREEN if p.get("approved") else sk.RED)

    def _on_commit_created(self, p: dict) -> None:
        self.head("Commit", f"{p.get('sha')}  {p.get('message', '')}"[:90], sk.BRIGHT)

    def _on_pr_opened(self, p: dict) -> None:
        self.head("Pull request", str(p.get("url") or p.get("pr")), sk.BRIGHT)

    def _on_pr_merged(self, p: dict) -> None:
        self.head("Merged", str(p.get("pr")), sk.GREEN)

    def _on_build_done(self, p: dict) -> None:
        self._stop()
        secs = int(time.time() - self.t0)
        self.out.print()
        body = Text("  ")
        # A run that ended on blocking findings does not get a tick. The mark follows
        # the reviewer, not the fact that the code reached the end of the function.
        blocked = int(p.get("blocking", 0) or 0)
        body.append("✖ " if blocked else "✔ ", style=f"bold {'#e5675c' if blocked else sk.GREEN}")
        body.append(str(p.get("reason", "done")), style=sk.TEXT)
        self.out.print(body)
        tail = Text("    ")
        tail.append(f"{p.get('nodes', 0)} node(s) · {p.get('findings', 0)} finding(s) answered · "
                    f"{p.get('pr', '')} · {secs // 60}m{secs % 60:02d}s", style=sk.DIM)
        self.out.print(tail)
        self.out.print(Group(*sk.shark_lines(dim=0.35)))
