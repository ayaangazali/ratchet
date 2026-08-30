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

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .node import Node, Tree

MAX_FAILURE = 2400
MAX_LISTING = 400

#: directories never worth walking into. Pruned during the walk, not filtered
#: after it: `rglob("*")` descends into .venv and node_modules first and then
#: throws the results away, which took 28 seconds on one ordinary home directory
#: and looked exactly like a hung prompt.
SKIP_DIRS = frozenset({
    ".git", ".ratchet", "__pycache__", "node_modules", ".venv", "venv", ".env",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "dist", "build",
    ".next", ".nuxt", "target", ".gradle", ".idea", ".cache", "site-packages",
})


def walk_files(repo: Path, *, limit: int = 2000):
    """Repo-relative file paths, pruning heavy directories as it goes.

    Bounded by `limit` so the cost of assembling a prompt never depends on how
    much unrelated junk sits under the working directory.
    """
    import os

    repo = Path(repo)
    seen = 0
    for root, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            yield str((Path(root) / name).relative_to(repo))
            seen += 1
            if seen >= limit:
                return


def tree_listing(repo: Path, f2p_hidden: Iterable[str]) -> str:
    """The file listing a mapper model is allowed to see.

    Lives here, next to the rest of what reaches prompts, so the held-out
    exclusion (CLAUDE.md invariant 5) has exactly one implementation: any file a
    hidden test id points at never appears in a listing, because the mapper's
    output is reused by every later prompt in a run.
    """
    repo = Path(repo)
    hidden_files = {t.partition("::")[0] for t in f2p_hidden}
    lines: list[str] = []
    for rel in walk_files(repo, limit=MAX_LISTING + 1):
        if rel in hidden_files:
            continue
        lines.append(rel)
        if len(lines) > MAX_LISTING:
            lines.append("... truncated")
            break
    return "\n".join(sorted(lines))

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
