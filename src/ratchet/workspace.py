"""One git worktree per candidate branch, plus the read-only file API the agent
is allowed to use.

The agent never gets a shell in a graded worktree. It reads through `read`,
`grep` and `tree`, and the only way it can change anything is `propose_patch`,
which goes through the pawl. That asymmetry is the whole design: reads are cheap
and unrestricted, writes are expensive and adjudicated.

Path handling is deliberately paranoid -- every path is resolved and checked to be
inside the worktree root, so `../../etc/passwd` and symlink escapes both fail.
"""

from __future__ import annotations

import fnmatch
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .ledger import git

SKIP_DIRS = {".git", ".ratchet", "__pycache__", ".venv", "node_modules", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
MAX_READ_BYTES = 200_000


class PathEscape(ValueError):
    pass


@dataclass
class Workspace:
    root: Path  # the primary repo checkout
    run_id: str
    worktrees: dict[str, Path]  # label -> path

    @classmethod
    def create(cls, root: Path, run_id: str) -> Workspace:
        return cls(root=root.resolve(), run_id=run_id, worktrees={})

    # ----------------------------------------------------------- worktrees --

    def base_dir(self) -> Path:
        d = self.root.parent / f".ratchet-worktrees-{self.run_id}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def add(self, label: str, branch: str, base_sha: str) -> Path:
        if label in self.worktrees:
            return self.worktrees[label]
        path = self.base_dir() / label
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        git("branch", "-f", branch, base_sha, cwd=self.root)
        git("worktree", "add", "--force", str(path), branch, cwd=self.root)
        self.worktrees[label] = path
        return path

    def remove(self, label: str) -> None:
        path = self.worktrees.pop(label, None)
        if path:
            git("worktree", "remove", "--force", str(path), cwd=self.root, check=False)

    def cleanup(self) -> None:
        for label in list(self.worktrees):
            self.remove(label)
        git("worktree", "prune", cwd=self.root, check=False)

    def path_for(self, label: str | None) -> Path:
        if not label or label in ("trunk", "main"):
            return self.root
        return self.worktrees.get(label, self.root)

    # ---------------------------------------------------------------- reads --

    def _safe(self, wt: Path, rel: str) -> Path:
        p = (wt / rel).resolve()
        if not str(p).startswith(str(wt.resolve())):
            raise PathEscape(f"path escapes the worktree: {rel}")
        return p

    def read(self, label: str | None, rel: str, start: int = 1, end: int | None = None) -> str:
        wt = self.path_for(label)
        p = self._safe(wt, rel)
        if not p.is_file():
            raise FileNotFoundError(rel)
        data = p.read_bytes()[:MAX_READ_BYTES].decode("utf-8", errors="replace")
        lines = data.splitlines()
        end = end or len(lines)
        chunk = lines[max(0, start - 1) : end]
        width = len(str(end))
        return "\n".join(f"{i + start:>{width}}  {t}" for i, t in enumerate(chunk))

    def tree(self, label: str | None, rel: str = ".", depth: int = 3) -> str:
        wt = self.path_for(label)
        base = self._safe(wt, rel)
        out: list[str] = []
        base_depth = len(base.parts)
        for p in sorted(base.rglob("*")):
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            d = len(p.parts) - base_depth
            if d > depth:
                continue
            indent = "  " * (d - 1)
            out.append(f"{indent}{p.name}{'/' if p.is_dir() else ''}")
            if len(out) > 800:
                out.append("... truncated")
                break
        return "\n".join(out)

    def grep(self, label: str | None, pattern: str, glob: str = "*", max_hits: int = 120) -> str:
        wt = self.path_for(label)
        rx = re.compile(pattern)
        hits: list[str] = []
        for p in sorted(wt.rglob("*")):
            if not p.is_file() or any(part in SKIP_DIRS for part in p.parts):
                continue
            if not fnmatch.fnmatch(p.name, glob):
                continue
            try:
                text = p.read_text(errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{p.relative_to(wt)}:{i}: {line.strip()[:200]}")
                    if len(hits) >= max_hits:
                        return "\n".join(hits) + f"\n... stopped at {max_hits} hits"
        return "\n".join(hits) or "(no matches)"

    def read_test_sources(self, label: str | None, protected: list[str]) -> dict[str, str]:
        """Used by patchlint's special-casing rule. Never exposed to the agent."""
        wt = self.path_for(label)
        out: dict[str, str] = {}
        for pat in protected:
            base = wt / pat.rstrip("/")
            if base.is_dir():
                for p in base.rglob("*.py"):
                    out[str(p.relative_to(wt))] = p.read_text(errors="replace")
            elif base.is_file():
                out[str(base.relative_to(wt))] = base.read_text(errors="replace")
        return out
