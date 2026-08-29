"""Git as the agent's memory, and as the rollback mechanism.

Every step the agent takes is a commit on a scratch branch. That gives three
things for free that are otherwise a week of engineering:

  * rollback      `git reset --hard <last green>` -- atomic, total, unarguable
  * audit trail   `git log` is the run transcript, with the verdict in the trailer
  * time travel   any commit can be checked out, diffed, or handed to a human

Rejected attempts are not thrown away. Before rolling back we park the attempt at
`refs/ratchet/rejected/<attempt-id>`, so nothing the agent produced is ever lost
and the TUI can render the discarded branches hanging off the green spine.

Nothing here shells out to `git` with user-controlled strings interpolated into a
shell -- every call is an argv list.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .models import Verdict

TRAILER = "Ratchet-Verdict"


class GitError(RuntimeError):
    pass


def git(*args: str, cwd: Path, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout.strip()


@dataclass
class Ledger:
    repo: Path
    run_id: str
    trunk: str  # e.g. ratchet/run-3f2a/trunk

    # ------------------------------------------------------------- setup --

    @classmethod
    def start(cls, repo: Path, run_id: str, *, base: str = "HEAD") -> Ledger:
        trunk = f"ratchet/{run_id}/trunk"
        git("checkout", "-B", trunk, base, cwd=repo)
        led = cls(repo=repo, run_id=run_id, trunk=trunk)
        led._dir.mkdir(parents=True, exist_ok=True)
        led.commit_empty("ratchet: run start", {"run_id": run_id, "base": led.head()})
        return led

    @property
    def _dir(self) -> Path:
        return self.repo / ".ratchet"

    @property
    def _jsonl(self) -> Path:
        return self._dir / f"{self.run_id}.jsonl"

    # -------------------------------------------------------------- reads --

    def head(self, ref: str = "HEAD") -> str:
        return git("rev-parse", ref, cwd=self.repo)

    def diff_from(self, base: str) -> str:
        return git("-c", "core.fileMode=false", "diff", base, cwd=self.repo)

    def working_diff(self) -> str:
        """Everything not yet committed, including untracked files."""
        git("add", "-A", cwd=self.repo)
        return git("-c", "core.fileMode=false", "diff", "--cached", cwd=self.repo)

    def log(self, n: int = 40) -> list[dict[str, str]]:
        raw = git("log", f"-{n}", "--pretty=format:%H%x1f%s%x1f%ct", cwd=self.repo)
        out = []
        for line in raw.splitlines():
            h, s, t = line.split("\x1f")
            out.append({"sha": h, "subject": s, "ts": t})
        return out

    # ------------------------------------------------------------- writes --

    def commit_empty(self, subject: str, meta: dict | None = None) -> str:
        args = ["commit", "--allow-empty", "-m", subject]
        if meta:
            args += ["-m", f"{TRAILER}: {json.dumps(meta, separators=(',', ':'))}"]
        git(*args, cwd=self.repo)
        return self.head()

    def commit_attempt(self, subject: str, verdict: Verdict) -> str:
        """Commit whatever is in the tree, stamping the verdict into the message."""
        git("add", "-A", cwd=self.repo)
        meta = {
            "attempt": verdict.attempt_id,
            "decision": verdict.decision.value,
            "score": round(verdict.score, 4),
            "hidden": round(verdict.f2p_hidden_rate, 3),
            "visible": round(verdict.f2p_visible_rate, 3),
            "p2p": round(verdict.p2p_rate, 3),
            "delta": round(verdict.delta, 3),
            "findings": [f.rule for f in verdict.findings],
        }
        git("commit", "--allow-empty", "-m", subject, "-m", f"{TRAILER}: {json.dumps(meta, separators=(',', ':'))}", cwd=self.repo)
        sha = self.head()
        self.append(verdict, sha)
        return sha

    def park_rejected(self, attempt_id: str) -> str:
        """Keep the rejected work reachable before we reset over it."""
        sha = self.head()
        git("update-ref", f"refs/ratchet/rejected/{attempt_id}", sha, cwd=self.repo)
        return sha

    def rollback(self, to_sha: str) -> None:
        git("reset", "--hard", to_sha, cwd=self.repo)
        git("clean", "-fd", cwd=self.repo)

    def append(self, verdict: Verdict, sha: str | None = None) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        rec = verdict.to_dict()
        rec["sha"] = sha
        rec["logged_at"] = time.time()
        with self._jsonl.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")

    def read_ledger(self) -> list[dict]:
        if not self._jsonl.exists():
            return []
        return [json.loads(line) for line in self._jsonl.read_text().splitlines() if line.strip()]

    # ------------------------------------------------------- candidates ----

    def fork_candidate(self, label: str, base_sha: str) -> str:
        branch = f"ratchet/{self.run_id}/{label}"
        git("branch", "-f", branch, base_sha, cwd=self.repo)
        return branch

    def adopt(self, branch: str) -> str:
        """Winner takes the trunk. Squash-free: we keep the candidate's history."""
        git("checkout", self.trunk, cwd=self.repo)
        git("merge", "--ff-only", branch, cwd=self.repo, check=False)
        if self.head() != git("rev-parse", branch, cwd=self.repo):
            git("reset", "--hard", branch, cwd=self.repo)
        return self.head()
