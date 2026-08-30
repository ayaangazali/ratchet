"""`ratchet go <repo-url>` -- point it at a repository and it works out the rest.

The gap between "I have Ratchet installed" and "Ratchet is searching my repo" used
to be a hand-written `task.yaml`, and writing one requires knowing which tests are
currently red, which are green, and which files count as protected. All three are
observable, so this module observes them instead of asking:

  clone -> detect the framework -> provision an environment -> run the suite once
        -> failing tests become fail-to-pass, passing tests become pass-to-pass
        -> write the task file

The held-out split is the one judgement call. Half the currently-failing tests are
withheld from the agent by interleaving the sorted list, because adjacent test names
in a file usually cover the same behaviour with different inputs -- which is exactly
what a held-out slice is supposed to be. Take the tail instead and you tend to hold
out a whole distinct feature, which measures the wrong thing.

Nothing here writes to the target repository except the clone itself and a virtual
environment under `.ratchet/`, which the worktree sandbox provider discovers on its
own and puts on PATH for every node in the search.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .models import TestStatus
from .verifier import parsers

#: Long enough for a cold `pip install -e .` on a real project, short enough that a
#: hung network does not eat the afternoon.
INSTALL_TIMEOUT_S = 900

#: A repository's whole suite, once. Ten minutes is generous for most and far too
#: little for a few -- `--probe-timeout` exists for the few. It is deliberately not
#: unbounded: a probe that hangs forever is indistinguishable from a broken tool.
PROBE_TIMEOUT_S = 600

#: How often the probe redraws its progress line.
_TICK_S = 0.4

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

#: Colour off, every way a runner knows how to be asked. `pytest -rA` prints
#: `PASSED tests/x.py::test_y`, and the parser splits that line on whitespace -- so
#: one wrapping escape sequence turns a whole suite into "no tests ran". FORCE_COLOR
#: in particular is set by several terminals and CI systems and overrides the
#: runner's own tty detection, so an inherited environment is enough to break the
#: probe on a machine where it worked yesterday.
NO_COLOUR_ENV = {
    "NO_COLOR": "1",
    "PY_COLORS": "0",
    "FORCE_COLOR": "0",
    "TERM": "dumb",
    "CLICOLOR": "0",
    "CLICOLOR_FORCE": "0",
}


@dataclass
class Detected:
    framework: str
    test_cmd: str
    source_paths: list[str] = field(default_factory=list)
    protected_paths: list[str] = field(default_factory=list)
    install: list[list[str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _run(argv: list[str], *, cwd: Path, timeout: int, env: dict | None = None) -> tuple[int, str]:
    """argv lists only. Never a shell, and never an interpolated string -- the URL
    and the detected command both come from outside this process."""
    try:
        p = subprocess.run(
            argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
            env={**os.environ, **(env or {})},
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(argv)}"
    except FileNotFoundError:
        return 127, f"not found: {argv[0]}"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


# --------------------------------------------------------------------------- #
# clone
# --------------------------------------------------------------------------- #


def normalise_url(url: str) -> str:
    """Accept the three things people paste: a full URL, `owner/repo`, or an SSH ref."""
    u = url.strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    if Path(u).expanduser().exists():
        # A local path has to be absolute: the clone runs with its cwd set to the
        # destination's parent, so a relative path would resolve against the wrong
        # directory and fail with a confusing "repository does not exist".
        return str(Path(u).expanduser().resolve())
    if u.startswith(("http://", "https://", "git@", "ssh://", "file://")):
        return url.strip()
    if u.count("/") == 1 and " " not in u:
        return f"https://github.com/{u}"
    return url.strip()


def repo_name(url: str) -> str:
    return Path(normalise_url(url).rstrip("/")).name.removesuffix(".git") or "repo"


def clone(url: str, dest: Path) -> Path:
    """Full clone, not shallow: the search commits on top of history and `rewind`
    restores to a sha, so a depth-1 clone would fail the first time you rewound."""
    dest = dest.resolve()
    if (dest / ".git").exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    code, out = _run(["git", "clone", normalise_url(url), str(dest)], cwd=dest.parent, timeout=600)
    if code != 0:
        raise RuntimeError(f"clone failed:\n{out.strip()[-800:]}")
    return dest


# --------------------------------------------------------------------------- #
# detect
# --------------------------------------------------------------------------- #

_TEST_DIRS = ("tests", "test", "spec", "__tests__")
_CONFIG_FILES = ("conftest.py", "pyproject.toml", "setup.cfg", "setup.py", "pytest.ini",
                 "tox.ini", "package.json", "vitest.config.ts", "jest.config.js")


def _package_json(repo: Path) -> dict:
    try:
        return json.loads((repo / "package.json").read_text())
    except Exception:
        return {}


def _python_sources(repo: Path) -> list[str]:
    if (repo / "src").is_dir():
        return ["src/"]
    out = [
        f"{d.name}/"
        for d in sorted(repo.iterdir())
        if d.is_dir()
        and (d / "__init__.py").exists()
        and d.name not in _TEST_DIRS
        and not d.name.startswith(".")
    ]
    return out or ["."]


def detect(repo: Path) -> Detected:
    """Framework, test command, and what counts as protected. Best effort, and it
    says so -- `notes` is printed so a wrong guess is visible before the run, not
    after it."""
    protected = [f"{d}/" for d in _TEST_DIRS if (repo / d).is_dir()]
    protected += [f for f in _CONFIG_FILES if (repo / f).exists()]

    if (repo / "go.mod").exists():
        return Detected("gotest", "go test ./... -v", ["."], protected,
                        notes=["go: no dependency install step; `go test` fetches its own"])

    if (repo / "Cargo.toml").exists():
        return Detected("cargo", "cargo test", ["src/"], protected)

    pkg = _package_json(repo)
    if pkg:
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        install = [["npm", "ci"]] if (repo / "package-lock.json").exists() else [["npm", "install"]]
        srcs = [d for d in ("src/", "lib/", "app/") if (repo / d.rstrip("/")).is_dir()] or ["."]
        if "vitest" in deps:
            return Detected("vitest", "npx vitest run --reporter=verbose", srcs, protected, install)
        if "jest" in deps:
            return Detected("jest", "npx jest --verbose", srcs, protected, install)
        return Detected("jest", "npm test --", srcs, protected, install,
                        notes=["no jest or vitest in package.json; guessing `npm test`"])

    steps: list[list[str]] = []
    if (repo / "pyproject.toml").exists() or (repo / "setup.py").exists():
        steps.append(["pip", "install", "-e", "."])
    for req in ("requirements.txt", "requirements-dev.txt", "dev-requirements.txt"):
        if (repo / req).exists():
            steps.append(["pip", "install", "-r", req])
    steps.append(["pip", "install", "pytest"])
    return Detected("pytest", "python -m pytest -rA", _python_sources(repo), protected, steps)


# --------------------------------------------------------------------------- #
# provision
# --------------------------------------------------------------------------- #


def provision(repo: Path, det: Detected, *, echo=print) -> Path | None:
    """Build the environment the search will reuse for every node.

    For Python it goes at `<repo>/.ratchet/venv`, which is exactly where
    `WorktreeProvider._warm_venv` looks. Every worktree the search creates then
    inherits it on PATH, so one install serves the whole run.
    """
    if det.framework not in ("pytest",):
        for argv in det.install:
            echo(f"  $ {' '.join(argv)}")
            code, out = _run(argv, cwd=repo, timeout=INSTALL_TIMEOUT_S)
            if code != 0:
                echo(f"    ! exit {code}; continuing anyway\n{out.strip()[-400:]}")
        return None

    venv = repo / ".ratchet" / "venv"
    if not venv.exists():
        uv = shutil.which("uv")
        argv = [uv, "venv", str(venv)] if uv else ["python3", "-m", "venv", str(venv)]
        echo(f"  $ {' '.join(argv)}")
        code, out = _run(argv, cwd=repo, timeout=300)
        if code != 0:
            echo(f"    ! could not create a virtualenv; using the ambient interpreter\n{out.strip()[-300:]}")
            return None

    py = venv / "bin" / "python"
    uv = shutil.which("uv")
    for argv in det.install:
        full = [uv, "pip", "install", "--python", str(py), *argv[2:]] if uv else [str(py), "-m", *argv]
        echo(f"  $ {' '.join(full)}")
        code, out = _run(full, cwd=repo, timeout=INSTALL_TIMEOUT_S)
        if code != 0:
            echo(f"    ! exit {code}; continuing anyway\n{out.strip()[-400:]}")
    return venv


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #


def probe(
    repo: Path,
    det: Detected,
    venv: Path | None,
    *,
    timeout: int = PROBE_TIMEOUT_S,
    echo=print,
) -> tuple[dict[str, TestStatus], str]:
    """Run the suite once, exactly as the gauntlet will, and read the result.

    This is the whole trick behind `ratchet go`: the tests that are red right now
    are the definition of the job, and the tests that are green right now are the
    definition of the regressions that must not appear.

    It streams. Running somebody's entire test suite can take ten minutes, and a
    silent subprocess for ten minutes is indistinguishable from a hang -- which is
    the single most likely way for a first-time user to conclude the tool is broken
    and close the terminal. So the output goes to a file they can tail, and the
    line on screen keeps moving.
    """
    env = dict(NO_COLOUR_ENV)
    if venv:
        env["VIRTUAL_ENV"] = str(venv)
        env["PATH"] = f"{venv / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"

    log_path = repo / ".ratchet" / "probe.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    echo(f"        $ {det.test_cmd}")
    echo(f"        log -> {log_path}")
    echo("        this runs the repository's whole suite; ctrl-c to stop")

    argv = shlex.split(det.test_cmd)
    code = _stream(argv, cwd=repo, env=env, log_path=log_path, timeout=timeout)
    raw = log_path.read_text(errors="replace")
    if code == 124:
        echo(f"\n        ! the suite did not finish within {timeout}s."
             f"\n          re-run with --probe-timeout <seconds>, or narrow the"
             f" test command in the task file.")
    # Belt and braces: a runner can be told to colour in its own config file, where
    # no environment variable will reach it.
    log = f"{parsers.START}\n{_ANSI.sub('', raw)}\n{parsers.END}\n{parsers.EXIT} {code}\n"
    return parsers.parse(log, det.framework), log


_SPINNER = ("\u00b7", "\u2722", "\u2733", "\u2217", "\u273b", "\u273d")
_SEEN = re.compile(r"\b(PASSED|FAILED|ERROR|ok|FAIL|not ok)\b")


def _stream(argv: list[str], *, cwd: Path, env: dict, log_path: Path, timeout: int) -> int:
    """Run to a log file, redrawing one status line while we wait. Returns the exit
    code, or 124 on timeout, matching `_run`."""
    quiet = not sys.stdout.isatty()
    try:
        fh = open(log_path, "w")
    except OSError:
        return _run(argv, cwd=cwd, timeout=timeout, env=env)[0]
    with fh:
        try:
            proc = subprocess.Popen(
                argv, cwd=str(cwd), stdout=fh, stderr=subprocess.STDOUT,
                env={**os.environ, **env},
            )
        except FileNotFoundError:
            fh.write(f"not found: {argv[0]}\n")
            return 127
        start = time.time()
        tick = 0
        while proc.poll() is None:
            if time.time() - start > timeout:
                proc.kill()
                proc.wait()
                if not quiet:
                    sys.stdout.write("\r" + " " * 100 + "\r")
                    sys.stdout.flush()
                return 124
            if not quiet:
                seen, last = _tail(log_path)
                elapsed = int(time.time() - start)
                line = (f"        {_SPINNER[tick % len(_SPINNER)]} probing  "
                        f"{elapsed // 60}m{elapsed % 60:02d}s  ·  {seen} results  ·  {last}")
                sys.stdout.write("\r" + line[:118].ljust(118))
                sys.stdout.flush()
            tick += 1
            time.sleep(_TICK_S)
        if not quiet:
            sys.stdout.write("\r" + " " * 118 + "\r")
            sys.stdout.flush()
        return proc.returncode


def _tail(log_path: Path) -> tuple[int, str]:
    """How many test results so far, and the last line worth showing."""
    try:
        body = log_path.read_text(errors="replace")
    except OSError:
        return 0, ""
    lines = [_ANSI.sub("", ln).strip() for ln in body.splitlines() if ln.strip()]
    return len(_SEEN.findall(body)), (lines[-1][:58] if lines else "starting…")


def split_held_out(failing: list[str]) -> tuple[list[str], list[str]]:
    """Interleave rather than slice. Adjacent test names in a file usually cover the
    same behaviour with different inputs, which is what a held-out slice should be;
    taking the tail tends to hold out a whole separate feature instead."""
    ordered = sorted(failing)
    if len(ordered) < 2:
        return ordered, []
    return ordered[0::2], ordered[1::2]


# --------------------------------------------------------------------------- #
# write
# --------------------------------------------------------------------------- #

_TASK_TEMPLATE = """\
# Written by `ratchet go`. Everything here was observed, not assumed:
# the fail-to-pass list is what was red when the repository was cloned, and the
# pass-to-pass list is what was green. Edit it if the split is wrong -- and it is
# worth reading, because this file is what the run is graded against.
#
#   f2p_hidden is never shown to the agent. The gap between the visible rate and
#   the hidden rate is the overfitting signal, so do not move names out of it to
#   make a run look better.

task_id: {task_id}
repo_path: {repo_path}
framework: {framework}
test_cmd: {test_cmd}
timeout_s: 900

statement: |
{statement}

allowed_paths:
{allowed}

f2p_visible:
{f2p_visible}

f2p_hidden:
{f2p_hidden}

p2p:
{p2p}

protected_paths:
{protected}
"""


def _yaml_list(items: list[str], indent: str = "  ") -> str:
    return "\n".join(f"{indent}- {i}" for i in items) if items else f"{indent}[]"


def write_task(
    *,
    repo: Path,
    det: Detected,
    statuses: dict[str, TestStatus],
    out: Path,
    goal: str | None = None,
    task_id: str | None = None,
) -> tuple[Path, list[str], list[str], list[str]]:
    failing = [t for t, s in statuses.items() if s in (TestStatus.FAILED, TestStatus.ERROR)]
    passing = [t for t, s in statuses.items() if s is TestStatus.PASSED]
    visible, hidden = split_held_out(failing)

    named = ", ".join(visible[:3]) or "the failing tests"
    statement = goal or (
        f"The test suite in {repo.name} has {len(failing)} failing test(s).\n"
        f"Make them pass without breaking the {len(passing)} that already pass.\n"
        f"Start with {named}.\n"
        f"Do not change the tests."
    )
    body = _TASK_TEMPLATE.format(
        task_id=task_id or f"{repo.name}-auto",
        repo_path=str(repo),
        framework=det.framework,
        test_cmd=det.test_cmd,
        statement="\n".join(f"  {line}" for line in statement.splitlines()),
        allowed=_yaml_list(det.source_paths),
        f2p_visible=_yaml_list(visible),
        f2p_hidden=_yaml_list(hidden),
        p2p=_yaml_list(passing),
        protected=_yaml_list(det.protected_paths),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body)
    return out, visible, hidden, passing


# --------------------------------------------------------------------------- #
# the one-shot
# --------------------------------------------------------------------------- #


def harness_up(base_url: str, timeout: float = 1.0) -> bool:
    """Is TrueForge listening? Checked before the search starts rather than after.

    Without the harness there is no model backend, so the run would exit on its
    first generate call and the console would sit there rendering an empty tree --
    the worst possible failure, because it looks like the search is thinking."""
    import socket
    from urllib.parse import urlparse

    u = urlparse(base_url)
    try:
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 80), timeout):
            return True
    except OSError:
        return False


def go(
    url: str,
    *,
    workdir: Path | None = None,
    goal: str | None = None,
    run: bool = True,
    console: bool = True,
    budget: int | None = None,
    scripted: str | None = None,
    probe_timeout: int = PROBE_TIMEOUT_S,
    echo=print,
) -> int:
    """Clone, provision, probe, write the task, and start searching.

    Returns a process exit code. The search runs as a child process writing to the
    bus, and the console attaches to that bus in the foreground -- the same split
    as running the two commands by hand, because the console has to survive being
    killed and restarted without disturbing the search.
    """
    import subprocess as sp
    import sys
    import uuid

    name = repo_name(url)
    root = (workdir or Path.cwd() / ".ratchet-work").resolve()
    repo = root / name

    echo(f"\n  ratchet go  ·  {normalise_url(url)}\n")
    echo("  [1/5] clone")
    if (repo / ".git").exists():
        echo(f"        already cloned at {repo}")
    else:
        clone(url, repo)
        echo(f"        {repo}")

    echo("  [2/5] detect")
    det = detect(repo)
    echo(f"        framework   {det.framework}")
    echo(f"        test        {det.test_cmd}")
    echo(f"        sources     {', '.join(det.source_paths)}")
    echo(f"        protected   {', '.join(det.protected_paths) or '(none found)'}")
    for note in det.notes:
        echo(f"        note        {note}")

    echo("  [3/5] provision")
    venv = provision(repo, det, echo=echo)
    echo(f"        {venv}" if venv else "        using the ambient interpreter")

    echo("  [4/5] probe -- running the suite once to see what is red")
    statuses, log = probe(repo, det, venv, timeout=probe_timeout, echo=echo)
    if not parsers.suite_ran(log):
        echo("\n  the suite did not run. Ratchet grades against tests, so it cannot start.")
        echo(f"  try it by hand:  cd {repo} && {det.test_cmd}")
        return 2

    task_path = Path("tasks") / f"{name}-auto.yaml"
    task_path, visible, hidden, passing = write_task(
        repo=repo, det=det, statuses=statuses, out=task_path.resolve(), goal=goal
    )
    echo(f"        {len(visible) + len(hidden)} failing · {len(passing)} passing")
    if not (visible or hidden):
        echo("\n  every test already passes, so there is nothing to ratchet.")
        echo(f"  point it at a repository with a failing test, or write {task_path} by hand.")
        return 0
    echo(f"        fail-to-pass visible : {len(visible)}")
    echo(f"        fail-to-pass held out: {len(hidden)}   (never shown to the agent)")
    echo(f"  [5/5] task written -> {task_path}")

    if run and not scripted:
        from .config import Settings

        base = Settings.from_env().trueforge_base_url
        if not harness_up(base):
            echo(f"\n  the task is ready, but TrueForge is not up at {base},")
            echo("  so there is no model to write patches. Start it and re-run:")
            echo("\n    npx @truefoundry/trueforge@latest")
            echo("\n  or drive the same search from canned patches, no model needed:")
            echo(f"    ratchet run --task {task_path} --repo {repo} --scripted <patches.json>")
            return 3

    run_id = f"go-{uuid.uuid4().hex[:6]}"
    bus = repo / ".ratchet" / f"{run_id}.bus.jsonl"
    argv = [sys.executable, "-m", "ratchet.cli", "run", "--task", str(task_path),
            "--repo", str(repo), "--run-id", run_id]
    if budget:
        argv += ["--budget", str(budget)]
    if scripted:
        argv += ["--scripted", scripted]

    if not run:
        echo("\n  next:")
        echo(f"    ratchet {' '.join(argv[3:])}")
        echo(f"    ratchet console --repo {repo} --bus {bus}")
        return 0

    bus.parent.mkdir(parents=True, exist_ok=True)
    bus.touch(exist_ok=True)
    log_path = repo / ".ratchet" / f"{run_id}.log"
    echo(f"\n  starting the search · run {run_id}")
    echo(f"  search log -> {log_path}\n")
    with open(log_path, "w") as fh:
        proc = sp.Popen(argv, stdout=fh, stderr=sp.STDOUT)

    if not console:
        return proc.wait()

    from .tui.app import RatchetApp

    try:
        RatchetApp(bus, repo).run()
    finally:
        if proc.poll() is None:
            echo("\n  the search is still running in the background.")
            echo(f"  watch it again:  python -m ratchet.cli console --repo {repo} --bus {bus}")
            echo(f"  stop it:         kill {proc.pid}")
    return 0
