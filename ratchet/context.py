"""What the model is shown before it writes the next patch.

Four inputs, and the fourth is the one people leave out:

  repo_map      a cheap-model summary of the repository, built once at startup
  failure       the trimmed failure from the node being expanded
  diff_so_far   the accumulated change from the root to this node
  dead_ends     one line per pruned sibling -- **negative-sibling injection**

Without dead ends, parallel branches rediscover the same wrong idea and you have
paid N times for a best-of-1. One line each is enough: the point is to rule a path
out, not to hand over a transcript. Everything here is budgeted in characters,
because context is the scarcest resource in the loop and an unbounded failure dump
is how a search run dies of its own logs.
"""

from __future__ import annotations

from dataclasses import dataclass

from .node import Node, Tree

MAX_FAILURE = 2400
MAX_DIFF = 6000
MAX_MAP = 3000
MAX_DEAD_ENDS = 6


@dataclass
class Context:
    task: str
    repo_map: str
    failure: str
    diff_so_far: str
    dead_ends: list[str]
    docs: str = ""
    depth: int = 0
    hint: str = ""

    def render(self) -> str:
        parts = [f"# Task\n{self.task}"]
        if self.repo_map:
            parts.append(f"# Repository map\n{self.repo_map[:MAX_MAP]}")
        if self.diff_so_far.strip():
            parts.append(f"# Change so far (root -> here, depth {self.depth})\n```diff\n{self.diff_so_far[:MAX_DIFF]}\n```")
        if self.failure:
            parts.append(f"# What the verifier said last\n```\n{self.failure[:MAX_FAILURE]}\n```")
        if self.dead_ends:
            parts.append(
                "# Already tried from this state, and pruned\n"
                + "\n".join(f"- {d}" for d in self.dead_ends[:MAX_DEAD_ENDS])
                + "\n\nDo not repeat these. If your idea resembles one of them, pick a different one."
            )
        if self.docs:
            parts.append(f"# Current upstream documentation\n{self.docs[:MAX_DIFF]}")
        if self.hint:
            parts.append(f"# Note\n{self.hint}")
        parts.append(
            "# What to produce\n"
            "A single unified diff against the current state, and one line saying what it is trying to do.\n"
            "You cannot mark yourself finished: a verifier you do not control decides whether this sticks.\n"
            "Editing tests, skipping them, weakening assertions or special-casing on test inputs is detected "
            "statically before your patch runs, and prunes the branch."
        )
        return "\n\n".join(parts)


def assemble(
    *,
    task: str,
    node: Node,
    tree: Tree,
    repo_map: str,
    diff_so_far: str,
    docs: str = "",
    hint: str = "",
) -> Context:
    parent = tree.nodes.get(node.parent_id) if node.parent_id else None
    dead = tree.failed_siblings(parent) if parent else []
    dead += [c for c in tree.children(node) if c.pruned]
    return Context(
        task=task,
        repo_map=repo_map,
        failure=node.last_failure,
        diff_so_far=diff_so_far,
        dead_ends=[d.one_line() for d in dead],
        docs=docs,
        depth=node.depth,
        hint=hint,
    )
