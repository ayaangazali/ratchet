"""Git as the tree's backing store, and as the thing that makes rollback unarguable.

Every node is a commit on a scratch branch. That buys three properties that would
otherwise be a week of engineering:

  rollback     restoring a node is a checkout, not a replay
  audit trail  `git log` is the run transcript, and the commit message carries the
               verdict, so the story is readable with no tooling at all
  handoff      the winning path squashes to one clean diff a human can review

Rejected work is never destroyed. Before a branch is pruned it is parked at
`refs/ratchet/pruned/<node>`, so a dead end can still be inspected, diffed, or
resurrected with `ratchet rewind`.

Every call here is an argv list. Nothing is interpolated into a shell.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(*args: str, cwd: Path | str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout.strip()


def commit_message(*, node_id: str, step: int, intent: str, before: float, after: float, verifier_line: str, tests_line: str) -> str:
    """The format from the build spec, so `git log` alone tells the story:

        [ratchet 0f3a] step 12 · score 0.62 → 0.81 · f2p 3/4 · p2p 118/118
        intent: widen the token refresh window to cover clock skew
        verifier: types ok, lint ok, cheat-check clean
    """
    subject = f"[ratchet {node_id}] step {step} · score {before:.2f} → {after:.2f} · {tests_line}"
    return f"{subject}\n\nintent: {intent}\nverifier: {verifier_line}\n"


@dataclass
class GitState:
    repo: Path
    run_id: str
    trunk: str  # the scratch branch the run lives on
    home_ref: str = ""  # where the user was before the run; restored on finish

    # ------------------------------------------------------------------ setup --

    @classmethod
    def start(cls, repo: Path, run_id: str, *, base: str = "HEAD") -> GitState:
        repo = Path(repo).resolve()
        # remember where the user was: leaving a real project stranded on a
        # ratchet scratch branch after the run is not acceptable (found by audit)
        home = git("symbolic-ref", "--short", "-q", "HEAD", cwd=repo, check=False) or git(
            "rev-parse", "HEAD", cwd=repo
        )
        trunk = f"ratchet/{run_id}/trunk"
        git("checkout", "-B", trunk, base, cwd=repo)
        st = cls(repo=repo, run_id=run_id, trunk=trunk, home_ref=home)
        (repo / ".ratchet").mkdir(parents=True, exist_ok=True)
        return st

    def return_home(self) -> None:
        """Back to the ref the run started from. The scratch branch and every
        parked node stay reachable by name; only the checkout moves."""
        if self.home_ref:
            git("checkout", "-q", self.home_ref, cwd=self.repo, check=False)

    # ------------------------------------------------------------------ reads --

    def head(self, ref: str = "HEAD", *, cwd: Path | None = None) -> str:
        return git("rev-parse", ref, cwd=cwd or self.repo)

    def diff(self, base: str, target: str = "HEAD", *, cwd: Path | None = None) -> str:
        return git("-c", "core.fileMode=false", "diff", base, target, cwd=cwd or self.repo)

    def working_diff(self, *, cwd: Path | None = None) -> str:
        """Everything not yet committed, untracked files included."""
        wd = cwd or self.repo
        git("add", "-A", cwd=wd)
        return git("-c", "core.fileMode=false", "diff", "--cached", cwd=wd)

    def changed_files(self, base: str, target: str = "HEAD", *, cwd: Path | None = None) -> list[str]:
        out = git("diff", "--name-only", base, target, cwd=cwd or self.repo)
        return [line for line in out.splitlines() if line.strip()]

    def log(self, n: int = 40) -> list[dict[str, str]]:
        raw = git("log", f"-{n}", "--pretty=format:%H%x1f%s%x1f%ct", cwd=self.repo)
        rows = []
        for line in raw.splitlines():
            h, s, t = line.split("\x1f")
            rows.append({"sha": h, "subject": s, "ts": t})
        return rows

    # ----------------------------------------------------------------- writes --

    def commit_node(self, message: str, *, cwd: Path | None = None) -> str:
        wd = cwd or self.repo
        git("add", "-A", cwd=wd)
        git("commit", "--allow-empty", "-m", message, cwd=wd)
        return self.head(cwd=wd)

    def park(self, node_id: str, sha: str) -> str:
        """Keep pruned work reachable. Nothing the agent produced is ever destroyed."""
        git("update-ref", f"refs/ratchet/pruned/{node_id}", sha, cwd=self.repo)
        return sha

    def restore(self, sha: str, *, cwd: Path | None = None) -> None:
        wd = cwd or self.repo
        git("reset", "--hard", sha, cwd=wd)
        git("clean", "-fd", cwd=wd)

    def squash(self, base_sha: str, tip_sha: str, message: str, *, branch: str | None = None) -> str:
        """Collapse the winning path into one reviewable commit.

        A human at an approval gate should see the change, not eleven steps of the
        search that produced it. The full path stays on the scratch branch.
        """
        target = branch or f"ratchet/{self.run_id}/ship"
        git("checkout", "-B", target, base_sha, cwd=self.repo)
        git("merge", "--squash", tip_sha, cwd=self.repo, check=False)
        git("add", "-A", cwd=self.repo)
        git("commit", "--allow-empty", "-m", message, cwd=self.repo)
        return self.head()

    def squashed_diff(self, base_sha: str, tip_sha: str) -> str:
        """The single clean diff, without creating a branch. What the gate shows."""
        return git("-c", "core.fileMode=false", "diff", base_sha, tip_sha, cwd=self.repo)


def is_repo(path: Path) -> bool:
    try:
        git("rev-parse", "--git-dir", cwd=path)
        return True
    except (GitError, FileNotFoundError):
        return False
