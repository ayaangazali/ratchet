"""What the model is shown before it writes the next patch.

Four inputs, and the fourth is the one people leave out:

  repo_map      a cheap-model summary of the repository, built once at startup
  failure       the trimmed failure from the node being expanded
  diff_so_far   the accumulated change from the root to this node
  dead_ends     one line per pruned sibling -- **negative-sibling injection**
  skills        techniques distilled from papers, and *only* the ones that have
                won a measured trial -- see `research/skills.py`

Without dead ends, parallel branches rediscover the same wrong idea and you have
paid N times for a best-of-1. One line each is enough: the point is to rule a path
out, not to hand over a transcript. Everything here is budgeted in characters,
because context is the scarcest resource in the loop and an unbounded failure dump
is how a search run dies of its own logs.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .node import Node, Tree

MAX_FAILURE = 2400
MAX_REVIEW = 2000
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

def editable_sources(repo: Path, task, *, limit_bytes: int = 24000) -> list[tuple[str, str]]:
    """The files a patch is allowed to touch, with their exact current contents.

    Same exclusion rules as `tree_listing`, and for the same reason (invariant 5):
    a held-out test's *contents* leak far more than its name, so protected paths and
    hidden test files are never read here. What is left is the source the agent is
    supposed to be fixing, which it needs to see in order to write a diff that
    applies to it.

    `allowed_paths` narrows this when a task sets it; otherwise everything outside the
    protected paths is fair game, smallest files first so a budget spent on one huge
    module does not crowd out the one that matters.
    """
    repo = Path(repo)
    skip = {".git", ".ratchet", "__pycache__", "node_modules", ".venv", ".pytest_cache"}
    hidden_files = {t.partition("::")[0] for t in getattr(task, "f2p_hidden", [])}
    protected = [pp.rstrip("/") for pp in getattr(task, "protected_paths", [])]
    allowed = [ap.rstrip("/") for ap in getattr(task, "allowed_paths", [])]

    def is_under(rel: str, roots: list[str]) -> bool:
        return any(rel == r or rel.startswith(r + "/") for r in roots)

    picked: list[tuple[int, str, str]] = []
    for f in sorted(repo.rglob("*")):
        if any(part in skip for part in f.parts) or not f.is_file():
            continue
        rel = str(f.relative_to(repo))
        if rel in hidden_files or is_under(rel, protected):
            continue
        if allowed and not is_under(rel, allowed):
            continue
        if f.suffix in {".pyc", ".so", ".png", ".jpg", ".gif", ".pdf", ".zip", ".lock"}:
            continue
        try:
            body = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # a binary or unreadable file is not something to patch
        picked.append((len(body), rel, body))

    out: list[tuple[str, str]] = []
    spent = 0
    for size, rel, body in sorted(picked):
        if spent + size > limit_bytes and out:
            break
        out.append((rel, body))
        spent += size
    return out


MAX_DIFF = 6000
MAX_MAP = 3000
MAX_DEAD_ENDS = 6
MAX_SKILLS = 3
MAX_SOURCE = 24000
MAX_SOURCE_FILE = 8000


@dataclass
class Context:
    task: str
    repo_map: str
    failure: str
    diff_so_far: str
    dead_ends: list[str]
    docs: str = ""
    skills: list[str] = field(default_factory=list)
    #: (path, contents) for every file the patch is allowed to touch. Sending these
    #: is not a nicety. Without them the model is asked for a unified diff against a
    #: file it has never seen, so it invents the context lines -- plausible code,
    #: correct intent, and a patch that cannot apply. Every node in an early live run
    #: was pruned "patch did not apply" for exactly this reason while the model's
    #: stated intent was right every time.
    sources: list[tuple[str, str]] = field(default_factory=list)
    depth: int = 0
    hint: str = ""
    review: str = ""

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
        if self.skills:
            # Placed after the failure and before the instruction: a technique is
            # advice about how to attack *this* failure, and advice read before the
            # problem is advice that gets ignored.
            parts.append(
                "# Techniques that measurably helped on tasks like this\n"
                + "\n\n".join(self.skills[:MAX_SKILLS])
                + "\n\nEach was distilled from a cited paper and kept only because an A/B trial "
                  "showed it improved the outcome. Apply one if it fits; ignore them if it does not."
            )
        if self.docs:
            parts.append(f"# Current upstream documentation\n{self.docs[:MAX_DIFF]}")
        if self.sources:
            budget = MAX_SOURCE
            blocks = []
            for path, body in self.sources:
                if budget <= 0:
                    break
                chunk = body[:min(MAX_SOURCE_FILE, budget)]
                budget -= len(chunk)
                truncated = "\n# ... truncated ..." if len(chunk) < len(body) else ""
                blocks.append(f"`{path}`\n```\n{chunk}{truncated}\n```")
            parts.append(
                "# The files you may edit, exactly as they are on disk right now\n"
                + "\n\n".join(blocks)
                + "\n\nYour diff's context lines must match this text character for character."
            )
        if self.review:
            parts.append(
                "# Latest Qodo review of this repo's open PR (advisory -- may lag local changes)\n"
                + self.review[:MAX_REVIEW]
            )
        if self.hint:
            parts.append(f"# Note\n{self.hint}")
        parts.append(
            "# What to produce\n"
            "One line `intent: <what this is trying to do>`, then the change, in either form:\n\n"
            "  1. a unified diff in a ```diff block, or\n"
            "  2. the complete new contents of each file you changed, in a fenced block whose\n"
            "     info string is `file:<path>` -- e.g. ```file:src/pkg/mod.py\n\n"
            "Form 2 is usually the safer one: hand-counted hunk headers are the most common\n"
            "reason a correct fix is rejected before it is ever run. The harness converts it\n"
            "into a real diff against the text above, so both forms are graded identically.\n"
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
    skills: list[str] | None = None,
    sources: list[tuple[str, str]] | None = None,
    hint: str = "",
    review: str = "",
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
        skills=list(skills or []),
        sources=list(sources or []),
        review=review,
        depth=node.depth,
        hint=hint,
    )
