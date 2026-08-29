"""Where candidate patches actually run.

Ratchet does not orchestrate containers itself. Isolation, snapshotting and
concurrency are the harness's job, and re-implementing them in `subprocess` calls
would throw away the entire argument for building on one. So this module is an
interface with two implementations and a benchmark that decides between them:

  HarnessProvider    execution goes through TrueForge's sandbox (`exec`), which owns
                     isolation, lifetime and per-branch separation. Forking a node
                     means asking the harness for a sandbox seeded from a snapshot,
                     so a child inherits its parent's installed dependencies and warm
                     build cache.

  WorktreeProvider   the documented fallback from the build spec §7: one git worktree
                     per node off a prebuilt base, every attempt sharing a pre-warmed
                     virtualenv. No snapshotting, no warm-cache flex, same search.

**Decide between them before noon, not at three o'clock.** `ratchet bench-snapshot`
round-trips a fork and prints the number: under ~5s and the tree search runs on real
snapshots; over it, take the fallback and stop touching it. The search is the
product; snapshotting is an optimisation of the search.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .gitstate import git

APPLY_CHAIN = [
    ["git", "apply", "--verbose"],
    ["git", "apply", "--verbose", "--3way"],
    ["patch", "--batch", "--fuzz=5", "-p1", "-i"],
]


@dataclass
class ExecResult:
    code: int
    out: str
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.code == 0

    def tail(self, n: int = 40) -> str:
        return "\n".join(self.out.strip().splitlines()[-n:])


class Sandbox(Protocol):
    """A place a patch can be applied and graded, and then thrown away."""

    id: str
    workdir: Path

    def exec(self, cmd: str, *, timeout: int = 300, cwd: str | None = None) -> ExecResult: ...
    def apply_patch(self, patch: str) -> ExecResult: ...
    def snapshot(self) -> str: ...
    def destroy(self) -> None: ...


class Provider(Protocol):
    name: str
    supports_snapshots: bool

    def fork(self, image: str, *, label: str) -> Sandbox: ...
    def base_image(self) -> str: ...


# --------------------------------------------------------------------------- #
def _runs(exe: str | None) -> bool:
    """Does this interpreter actually start? Existence is not the same as working."""
    if not exe:
        return False
    try:
        return subprocess.run([exe, "-c", "import sys"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# fallback: git worktrees off a prebuilt base, sharing one warm venv
# --------------------------------------------------------------------------- #


class WorktreeSandbox:
    def __init__(self, repo: Path, workdir: Path, sha: str, label: str, venv: Path | None,
                 shim: Path | None = None) -> None:
        self.repo = repo
        self.workdir = workdir
        self.sha = sha
        self.id = label
        self.venv = venv
        self.shim = shim

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.venv and self.venv.exists():
            # one pre-warmed environment shared by every attempt: this is the whole
            # reason the fallback is fast enough to search with
            env["VIRTUAL_ENV"] = str(self.venv)
            env["PATH"] = f"{self.venv / 'bin'}{os.pathsep}{env.get('PATH', '')}"
        if self.shim is not None:
            # Behind the venv, so a real environment always wins.
            env["PATH"] = f"{env.get('PATH', '')}{os.pathsep}{self.shim}"
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        return env

    def exec(self, cmd: str, *, timeout: int = 300, cwd: str | None = None) -> ExecResult:
        t0 = time.time()
        try:
            proc = subprocess.run(
                ["/bin/bash", "-lc", cmd],
                cwd=str(Path(cwd) if cwd else self.workdir),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._env(),
            )
            return ExecResult(proc.returncode, (proc.stdout or "") + (proc.stderr or ""), time.time() - t0)
        except subprocess.TimeoutExpired:
            return ExecResult(124, ">>>>> timed out", time.time() - t0)

    def apply_patch(self, patch: str) -> ExecResult:
        """Escalating apply chain. The patch file lives outside the worktree because
        the cleanup between attempts (`git clean -fd`) would otherwise delete it."""
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".diff", prefix="ratchet-")
        with os.fdopen(fd, "w") as fh:
            fh.write(patch)
        try:
            for argv in APPLY_CHAIN:
                proc = subprocess.run([*argv, path], cwd=str(self.workdir), capture_output=True, text=True)
                if proc.returncode == 0:
                    return ExecResult(0, "patch applied")
                git("checkout", "--", ".", cwd=self.workdir, check=False)
                git("clean", "-fd", cwd=self.workdir, check=False)
            rev = subprocess.run(
                ["git", "apply", "--check", "--reverse", path], cwd=str(self.workdir), capture_output=True
            )
            if rev.returncode == 0:
                return ExecResult(0, "patch already applied")
            return ExecResult(1, "patch did not apply")
        finally:
            os.unlink(path)

    def snapshot(self) -> str:
        """Without container snapshots the state *is* the commit."""
        return git("rev-parse", "HEAD", cwd=self.workdir)

    def destroy(self) -> None:
        git("worktree", "remove", "--force", str(self.workdir), cwd=self.repo, check=False)


class WorktreeProvider:
    name = "worktree"
    supports_snapshots = False

    def __init__(self, repo: Path, run_id: str, *, venv: Path | None = None) -> None:
        self.repo = Path(repo).resolve()
        self.run_id = run_id
        self.root = self.repo.parent / f".ratchet-wt-{run_id}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.venv = venv or self._warm_venv()
        self.shim = self._python_shim()

    def _python_shim(self) -> Path | None:
        """Make `python` resolve when only `python3` exists.

        macOS ships `python3` and no `python`, and a task whose `test_cmd` starts
        with `python` -- which is most of them -- then fails inside the sandbox with
        "command not found". The gauntlet reads that as a failed build, so no patch
        can ever go green and the search explores a repository where nothing works.

        The shim sits at the *end* of PATH, so a warm venv or a real `python` always
        wins; it only fills a hole that would otherwise be fatal.
        """
        if _runs(shutil.which("python")):
            return None
        # Not "does it exist" but "does it work": macOS ships /usr/bin/python as a
        # stub that exits non-zero with "requires the command line developer tools",
        # so `which` finds it, the shim never fires, and every graded command dies
        # on a binary that was never real.
        # `sys.executable` first, not `python3`: the interpreter running Ratchet is
        # the one that demonstrably has pytest and the project installed, whereas a
        # system python3 is typically bare and would fail at "no module named pytest"
        # -- which the gauntlet reads as a failed build, one layer removed from the
        # real cause.
        target = sys.executable if _runs(sys.executable) else shutil.which("python3")
        if not target:
            return None
        d = self.repo / ".ratchet" / "shim"
        d.mkdir(parents=True, exist_ok=True)
        link = d / "python"
        try:
            if link.is_symlink() or link.exists():
                link.unlink()
            # A wrapper, not a symlink. Python locates its virtualenv by looking for
            # `pyvenv.cfg` next to `sys.executable`; a symlink makes sys.executable
            # the link's own path, whose directory has no pyvenv.cfg, so the venv is
            # silently not activated and the interpreter starts without pytest. exec
            # keeps sys.executable pointing at the real binary.
            link.write_text(f'#!/bin/sh\nexec {shlex.quote(str(target))} "$@"\n')
            link.chmod(0o755)
        except OSError:
            return None
        return d

    def _warm_venv(self) -> Path | None:
        """One environment, built once, shared by every node. If it isn't there we
        simply use the ambient interpreter -- the search still works."""
        cand = self.repo / ".ratchet" / "venv"
        return cand if cand.exists() else None

    def base_image(self) -> str:
        return git("rev-parse", "HEAD", cwd=self.repo)

    def fork(self, image: str, *, label: str) -> WorktreeSandbox:
        path = self.root / label
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        branch = f"ratchet/{self.run_id}/{label}"
        git("branch", "-f", branch, image, cwd=self.repo)
        git("worktree", "add", "--force", str(path), branch, cwd=self.repo)
        return WorktreeSandbox(self.repo, path, image, label, self.venv, self.shim)

    def cleanup(self) -> None:
        git("worktree", "prune", cwd=self.repo, check=False)
        shutil.rmtree(self.root, ignore_errors=True)


# --------------------------------------------------------------------------- #
# primary: execution through the harness
# --------------------------------------------------------------------------- #


class HarnessSandbox:
    """Runs commands inside a TrueForge sandbox rather than a container we manage.

    The harness owns isolation and lifetime. We own what gets run and what the
    result means. `snapshot()` returns whatever reference the provider gives us for
    a restorable state; when the provider has no snapshot primitive it degrades to
    the commit sha and `Provider.supports_snapshots` is False, which is exactly the
    signal `bench-snapshot` reports.
    """

    def __init__(self, client, session_id: str, workdir: Path, label: str, repo_url: str | None = None) -> None:
        self.client = client
        self.session_id = session_id
        self.workdir = workdir
        self.id = label
        self.repo_url = repo_url

    def exec(self, cmd: str, *, timeout: int = 300, cwd: str | None = None) -> ExecResult:
        t0 = time.time()
        res = self.client.sandbox_exec(
            self.session_id, command=cmd, cwd=cwd or str(self.workdir), timeout=timeout,
            intent="ratchet verifier stage",
        )
        return ExecResult(res.get("exit_code", 0), res.get("output", ""), time.time() - t0)

    def apply_patch(self, patch: str) -> ExecResult:
        heredoc = "\n".join(
            [
                "cat > /tmp/ratchet.diff <<'RATCHET_EOF'",
                patch,
                "RATCHET_EOF",
                "git apply --verbose /tmp/ratchet.diff || git apply --verbose --3way /tmp/ratchet.diff",
            ]
        )
        return self.exec(heredoc, timeout=120)

    def snapshot(self) -> str:
        ref = self.client.sandbox_snapshot(self.session_id)
        return ref or self.exec("git rev-parse HEAD").out.strip()

    def destroy(self) -> None:
        self.client.sandbox_release(self.session_id, self.id)


class HarnessProvider:
    name = "harness"

    def __init__(self, client, *, repo_url: str, workdir: str = "/work") -> None:
        self.client = client
        self.repo_url = repo_url
        self.workdir = Path(workdir)
        self.supports_snapshots = bool(getattr(client, "supports_snapshots", False))

    def base_image(self) -> str:
        return self.client.base_snapshot() or "HEAD"

    def fork(self, image: str, *, label: str) -> HarnessSandbox:
        session_id = self.client.fork_sandbox(image, label=label)
        return HarnessSandbox(self.client, session_id, self.workdir, label, self.repo_url)


# --------------------------------------------------------------------------- #
# the 11:15 decision
# --------------------------------------------------------------------------- #


@dataclass
class BenchResult:
    provider: str
    snapshots: bool
    fork_s: float
    restore_s: float
    verdict: str

    def render(self) -> str:
        lines = [
            f"provider        {self.provider}",
            f"snapshots       {'yes' if self.snapshots else 'no (commit-only)'}",
            f"fork            {self.fork_s:.2f}s",
            f"restore + exec  {self.restore_s:.2f}s",
            "",
            self.verdict,
        ]
        return "\n".join(lines)


def bench_snapshot(provider, *, rounds: int = 3) -> BenchResult:
    """Round-trip a fork and time it. Under ~5s, run the full tree search; over it,
    take the worktree fallback and stop spending time on this."""
    forks, restores = [], []
    for i in range(rounds):
        base = provider.base_image()
        t0 = time.time()
        sb = provider.fork(base, label=f"bench-{uuid.uuid4().hex[:4]}-{i}")
        forks.append(time.time() - t0)
        t1 = time.time()
        sb.exec("python -c 'import sys; sys.exit(0)'", timeout=60)
        restores.append(time.time() - t1)
        sb.destroy()
    fork_s = sum(forks) / len(forks)
    restore_s = sum(restores) / len(restores)
    total = fork_s + restore_s
    if total <= 5.0:
        verdict = f"round trip {total:.2f}s — under the 5s line. Run the full tree search."
    else:
        verdict = (
            f"round trip {total:.2f}s — over the 5s line. Take the fallback now: worktrees off a "
            "prebuilt base with a shared warm venv. You lose the warm-cache flex, you keep the search."
        )
    return BenchResult(provider.name, getattr(provider, "supports_snapshots", False), fork_s, restore_s, verdict)
