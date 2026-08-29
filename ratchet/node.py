"""The search tree: nodes are restorable repo states, not log lines.

This is the thing that makes Ratchet a search rather than a loop with retries. A
node is a git commit plus a sandbox snapshot, so any node can be booted again with
its dependencies already installed and its build cache warm. That is what makes
`rewind` real and forking cheap.

The whole tree serialises to one JSON file under `.ratchet/`, because session
persistence is what lets a run survive a disconnect, and because a tree you can
`cat` is a tree you can debug at 3am.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .models import GauntletResult, Outcome, StageResult


@dataclass
class Node:
    id: str
    parent_id: str | None
    commit: str  # git sha on the scratch branch
    image: str  # sandbox snapshot ref
    patch: str  # diff from parent
    intent: str = ""  # one line, what this step was trying to do
    score: float = 0.0
    green: bool = False
    stage_results: dict[str, StageResult] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    last_failure: str = ""
    depth: int = 0
    fanout: int = 1
    model: str = ""
    tokens: int = 0
    cost_usd: float = 0.0
    outcome: str = Outcome.PROGRESS.value
    pruned: bool = False
    untried: bool = True  # never expanded
    created_at: float = field(default_factory=time.time)

    # ---------------------------------------------------------------- factory --

    @staticmethod
    def new_id(parent_id: str | None, patch: str, commit: str) -> str:
        return hashlib.sha256(f"{parent_id}{commit}{patch}".encode()).hexdigest()[:4]

    @classmethod
    def root(cls, *, commit: str, image: str, result: GauntletResult | None = None) -> Node:
        return cls(
            id="root",
            parent_id=None,
            commit=commit,
            image=image,
            patch="",
            intent="baseline",
            score=result.score if result else 0.0,
            green=bool(result and result.green),
            stage_results=dict(result.stages) if result else {},
            depth=0,
        )

    @classmethod
    def child_of(
        cls,
        parent: Node,
        *,
        commit: str,
        image: str,
        patch: str,
        intent: str,
        result: GauntletResult,
        model: str = "",
        tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> Node:
        return cls(
            id=cls.new_id(parent.id, patch, commit),
            parent_id=parent.id,
            commit=commit,
            image=image,
            patch=patch,
            intent=intent,
            score=result.score,
            green=result.green,
            stage_results=dict(result.stages),
            findings=[f.rule for f in result.findings],
            last_failure=result.last_failure,
            depth=parent.depth + 1,
            model=model,
            tokens=tokens,
            cost_usd=cost_usd,
            outcome=result.outcome.value,
        )

    # ------------------------------------------------------------------ views --

    @property
    def alive(self) -> bool:
        return not self.pruned

    def one_line(self) -> str:
        """How this node appears to a *sibling* as a dead end. Deliberately terse:
        the point of negative-sibling injection is to stop the model re-treading a
        path, not to hand it a transcript."""
        why = {
            Outcome.REGRESSED.value: "broke a passing test",
            Outcome.CHEATED.value: f"integrity violation ({', '.join(self.findings) or 'flagged'})",
            Outcome.BROKEN.value: "did not build",
        }.get(self.outcome, f"scored {self.score:.2f}")
        return f"{self.intent or 'unnamed attempt'} -> {why}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["stage_results"] = {k: v.to_dict() for k, v in self.stage_results.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Node:
        stages = {k: StageResult(**v) for k, v in (d.get("stage_results") or {}).items()}
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{**{k: v for k, v in d.items() if k in known}, "stage_results": stages})


class Tree:
    """The frontier, the history and the persistence, in one object."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.nodes: dict[str, Node] = {}
        self.order: list[str] = []

    # ------------------------------------------------------------------ basics --

    def add(self, node: Node) -> Node:
        # id collisions are possible in principle (4 hex chars); make them impossible
        while node.id in self.nodes:
            node.id = Node.new_id(node.parent_id, node.patch + node.id, node.commit)
        self.nodes[node.id] = node
        self.order.append(node.id)
        self.save()
        return node

    def get(self, node_id: str) -> Node:
        if node_id in self.nodes:
            return self.nodes[node_id]
        matches = [n for k, n in self.nodes.items() if k.startswith(node_id)]
        if len(matches) == 1:
            return matches[0]
        raise KeyError(f"no node {node_id!r}" if not matches else f"{node_id!r} is ambiguous")

    def __iter__(self) -> Iterator[Node]:
        return (self.nodes[i] for i in self.order)

    def __len__(self) -> int:
        return len(self.nodes)

    @property
    def root(self) -> Node:
        return self.nodes["root"]

    # ------------------------------------------------------------ relationships --

    def children(self, node: Node) -> list[Node]:
        return [n for n in self if n.parent_id == node.id]

    def siblings(self, node: Node) -> list[Node]:
        return [n for n in self if n.parent_id == node.parent_id and n.id != node.id]

    def failed_siblings(self, parent: Node) -> list[Node]:
        return [n for n in self.children(parent) if n.pruned]

    def path_to(self, node: Node) -> list[Node]:
        chain: list[Node] = []
        cur: Node | None = node
        while cur is not None:
            chain.append(cur)
            cur = self.nodes.get(cur.parent_id) if cur.parent_id else None
        return list(reversed(chain))

    def frontier(self) -> list[Node]:
        """Every live node is expandable. A deep node with a good score is still a
        candidate, and so is a shallow one we never came back to -- which is exactly
        what the scheduler needs in order to escape a tunnel."""
        return [n for n in self if n.alive]

    def best(self) -> Node:
        return max(self, key=lambda n: (n.green, n.score, -n.depth))

    def prune(self, node: Node, reason: str = "") -> None:
        node.pruned = True
        node.untried = False
        if reason:
            node.last_failure = (node.last_failure or reason)[:2000]
        self.save()

    # ------------------------------------------------------------- persistence --

    def save(self) -> None:
        payload = {"order": self.order, "nodes": {k: v.to_dict() for k, v in self.nodes.items()}}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str))
        tmp.replace(self.path)  # atomic: a half-written tree is worse than none

    @classmethod
    def load(cls, path: Path) -> Tree:
        t = cls(path)
        if path.exists():
            data = json.loads(path.read_text())
            t.order = data.get("order", [])
            t.nodes = {k: Node.from_dict(v) for k, v in (data.get("nodes") or {}).items()}
        return t

    # ------------------------------------------------------------------ render --

    def render(self, *, live_id: str | None = None, width: int = 30) -> list[tuple[str, str]]:
        """Return (line, style-hint) pairs. Shared by the TUI and `ratchet tree`, so
        what you see in a screenshot is what you see in a terminal."""
        out: list[tuple[str, str]] = []

        def walk(node: Node, prefix: str, is_last: bool, is_root: bool) -> None:
            glyph = "✗" if node.pruned else ("●" if not node.green else "★")
            style = "pruned" if node.pruned else ("green" if node.green else ("live" if node.id == live_id else "ok"))
            elbow = "" if is_root else ("└─" if is_last else "├─")
            label = f"{prefix}{elbow}{glyph} {node.id:<5} {node.score:.2f}"
            if node.id == live_id:
                label += " ←live"
            elif node.pruned:
                label += "  pruned"
            elif node.green:
                label += " ✓green"
            out.append((label, style))
            kids = [n for n in self if n.parent_id == node.id]
            for i, kid in enumerate(kids):
                nxt = prefix + ("" if is_root else ("   " if is_last else "│  "))
                walk(kid, nxt, i == len(kids) - 1, False)

        if self.nodes:
            walk(self.root, "", True, True)
        return out
