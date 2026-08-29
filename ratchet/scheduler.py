"""Where to spend the next unit of compute, and when to stop spending it.

Two jobs, both small, both decisive:

**Selection.** Which live node to expand next. Score is the main term, but novelty
is the one that earns its keep: without it, N parallel branches converge on the same
patch and you have paid N times for a best-of-1. Novelty is measured as line-level
distance between a node's diff and its siblings' diffs -- cheap, no model call.

**Budget.** Hard caps on nodes, wall clock and dollars, checked before every
expansion and rendered in the UI at all times. A search with no budget is a way to
spend your afternoon and your credits discovering that you have no demo.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .node import Node, Tree

# --- weights from the spec; kept in one place so they can be tuned live -------
W_NOVELTY = 0.30
W_DEPTH = 0.05
W_UNTRIED = 0.10

STALL_PATIENCE = 3  # expansions without improvement before we fan out
STALL_FANOUT = 3


@dataclass
class Budget:
    max_nodes: int = 40
    max_seconds: float = 900.0
    max_usd: float = 3.0
    started_at: float = field(default_factory=time.time)
    nodes_used: int = 0
    usd_used: float = 0.0

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    @property
    def remaining_nodes(self) -> int:
        return max(0, self.max_nodes - self.nodes_used)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.max_seconds - self.elapsed)

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.max_usd - self.usd_used)

    def ok(self) -> bool:
        return self.remaining_nodes > 0 and self.remaining_seconds > 0 and self.remaining_usd > 0

    def exhausted_reason(self) -> str:
        if self.remaining_nodes <= 0:
            return f"node budget spent ({self.max_nodes})"
        if self.remaining_seconds <= 0:
            return f"wall clock spent ({self.max_seconds:.0f}s)"
        if self.remaining_usd <= 0:
            return f"spend cap reached (${self.max_usd:.2f})"
        return ""

    def spend(self, *, nodes: int = 0, usd: float = 0.0) -> None:
        self.nodes_used += nodes
        self.usd_used += usd

    def line(self) -> str:
        m, s = divmod(int(self.elapsed), 60)
        return f"budget: {self.nodes_used}/{self.max_nodes} nodes · {m}m{s:02d}s · ${self.usd_used:.2f}"

    def to_dict(self) -> dict:
        return {
            "nodes_used": self.nodes_used,
            "max_nodes": self.max_nodes,
            "elapsed": round(self.elapsed, 1),
            "max_seconds": self.max_seconds,
            "usd_used": round(self.usd_used, 4),
            "max_usd": self.max_usd,
        }


# --------------------------------------------------------------------------- #
# novelty
# --------------------------------------------------------------------------- #


def _changed_lines(patch: str) -> set[str]:
    return {
        line[1:].strip()
        for line in patch.splitlines()
        if (line.startswith("+") or line.startswith("-"))
        and not line.startswith(("+++", "---"))
        and line[1:].strip()
    }


def diff_distance(a: str, b: str) -> float:
    """1.0 = nothing in common, 0.0 = identical change sets (Jaccard distance)."""
    sa, sb = _changed_lines(a), _changed_lines(b)
    if not sa and not sb:
        return 0.0
    if not sa or not sb:
        return 1.0
    return 1.0 - len(sa & sb) / len(sa | sb)


def novelty(node: Node, siblings: list[Node]) -> float:
    """Mean distance from the node's siblings. A node with no siblings is maximally
    novel -- nothing has been tried alongside it yet."""
    others = [s for s in siblings if s.id != node.id]
    if not others:
        return 1.0
    return sum(diff_distance(node.patch, s.patch) for s in others) / len(others)


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #


def select_score(node: Node, tree: Tree) -> float:
    return (
        node.score
        + W_NOVELTY * novelty(node, tree.siblings(node))
        - W_DEPTH * node.depth
        + W_UNTRIED * (1.0 if node.untried else 0.0)
    )


class Scheduler:
    def __init__(self, budget: Budget | None = None, *, patience: int = STALL_PATIENCE) -> None:
        self.budget = budget or Budget()
        self.patience = patience
        self.best_seen = -1.0
        self.since_improvement = 0
        self.stalls = 0

    def select(self, tree: Tree) -> Node | None:
        frontier = tree.frontier()
        if not frontier:
            return None
        return max(frontier, key=lambda n: (select_score(n, tree), -n.depth))

    def observe(self, tree: Tree) -> None:
        """Call once per expansion, after the children are in. Tracks the stall."""
        best = max((n.score for n in tree.frontier()), default=0.0)
        if best > self.best_seen + 1e-6:
            self.best_seen = best
            self.since_improvement = 0
        else:
            self.since_improvement += 1

    @property
    def stalled(self) -> bool:
        return self.since_improvement >= self.patience

    def fanout_for(self, node: Node) -> int:
        return STALL_FANOUT if self.stalled else node.fanout

    def stall_target(self, tree: Tree) -> Node | None:
        """When stalled, fork from the highest-scoring *shallow* node rather than the
        deepest one. This is the specific move that stops the search tunnelling into
        a dead branch and calling it progress."""
        live = tree.frontier()
        if not live:
            return None
        shallow_cut = min(n.depth for n in live) + 1
        shallow = [n for n in live if n.depth <= shallow_cut] or live
        return max(shallow, key=lambda n: n.score)

    def note_stall(self) -> None:
        self.stalls += 1
        self.since_improvement = 0  # give the fan-out a fair run before stalling again
