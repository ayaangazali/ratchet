"""`ratchet doctor` -- find out why a run will not work, before it does not work.

Every failure this project actually hit in practice was a configuration failure that
announced itself three layers down as something else: a harness with no provider
looked like an agent that produced empty patches; a model name that no provider
carried looked like a search that explored nothing. The verifier was never the
problem, and neither was the loop.

So this is the first thing to run on a new machine. Each check is independent, states
what it looked at, and -- when it fails -- prints the exact command that fixes it.
Nothing here mutates anything.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

OK = "ok"
WARN = "warn"
FAIL = "fail"

_MARK = {OK: "  ok  ", " warn ": " warn ", WARN: " warn ", FAIL: " FAIL "}


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""

    def render(self) -> str:
        mark = {OK: "  ok ", WARN: "warn ", FAIL: "FAIL "}[self.status]
        line = f"  [{mark}] {self.name:<24} {self.detail}"
        if self.fix and self.status != OK:
            line += f"\n           fix: {self.fix}"
        return line


def _check_harness(settings) -> tuple[Check, list[str]]:
    """Reachability and the model catalog, in one call. Returns the available models
    so the later checks do not each pay for their own round trip."""
    from .harness.client import TrueForgeClient

    base = settings.trueforge_base_url
    try:
        client = TrueForgeClient(base, timeout=15.0)
        models = [m.get("name") or m.get("model_id") or "" for m in (client.models() or [])]
        models = [m for m in models if m]
    except Exception as e:
        return (
            Check(
                "harness",
                FAIL,
                f"cannot reach TrueForge at {base} ({type(e).__name__})",
                "start it:  npx @truefoundry/trueforge@latest",
            ),
            [],
        )
    if not models:
        return (
            Check(
                "harness",
                FAIL,
                f"reachable at {base}, but no models are configured",
                "python scripts/setup_harness.py openai --key sk-...",
            ),
            [],
        )
    return Check("harness", OK, f"{base} — {len(models)} model(s)"), models


def _check_routing(settings, available: list[str]) -> list[Check]:
    """The check that would have saved the most time: does the routing we are about to
    use name models this harness actually has?"""
    if not available:
        return [Check("model routing", FAIL, "skipped — no models to route onto", "configure a provider first")]

    from .harness.catalog import resolve_roles

    try:
        resolved, notes = resolve_roles(settings.roles(), available)
    except Exception as e:
        return [Check("model routing", FAIL, str(e)[:160])]

    subs = [n for n in notes if n.substituted]
    checks = []
    if subs:
        detail = f"{len(subs)} of {len(notes)} requested model(s) do not exist here; substituted"
        checks.append(
            Check(
                "model routing",
                WARN,
                detail,
                "pin real names in .env (RATCHET_GENERATORS=...) to silence this",
            )
        )
        for n in subs:
            checks.append(Check("", OK, f"       {n.requested} -> {n.resolved}  ({n.reason})"))
    else:
        checks.append(Check("model routing", OK, "every requested model exists on this harness"))

    checks.append(
        Check("fan-out diversity", OK if len(set(resolved.generators)) > 1 else WARN,
              f"{len(set(resolved.generators))} distinct generator model(s): {', '.join(resolved.generators)}",
              "configure a second provider so the fan-out explores different models")
    )
    return checks


def _check_live_call(settings, available: list[str]) -> Check:
    """The only check that proves the whole path works: ask for a token and get one."""
    if not available:
        return Check("live model call", FAIL, "skipped — no models")
    from .harness.backend import HarnessBackend
    from .harness.catalog import resolve_roles
    from .harness.client import TrueForgeClient

    try:
        resolved, _ = resolve_roles(settings.roles(), available)
        backend = HarnessBackend(TrueForgeClient(settings.trueforge_base_url, timeout=90.0))
        text, tokens, cost = backend.complete(
            "Reply with exactly the word: READY", model=resolved.cartographer, role="doctor", max_tokens=16
        )
    except Exception as e:
        return Check("live model call", FAIL, f"{type(e).__name__}: {str(e)[:200]}",
                     "check the provider key:  python scripts/setup_harness.py --list")
    if "READY" not in text.upper():
        return Check("live model call", WARN, f"replied {text.strip()[:60]!r} — the path works, the model ignored the instruction")
    return Check("live model call", OK, f"{resolved.cartographer} replied — {tokens} tokens, ${cost:.5f}")


def _check_git(settings) -> Check:
    repo = Path(settings.repo)
    if not repo.exists():
        return Check("target repo", FAIL, f"{repo} does not exist", "ratchet demo    # seed the demo repository")
    if not (repo / ".git").exists():
        return Check("target repo", FAIL, f"{repo} is not a git repository",
                     f"git -C {repo} init && git -C {repo} add -A && git -C {repo} commit -m init")
    dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True)
    n = len([x for x in dirty.stdout.splitlines() if x.strip()])
    if n:
        return Check("target repo", WARN, f"{repo} has {n} uncommitted change(s); the run branches from HEAD",
                     f"git -C {repo} stash")
    return Check("target repo", OK, f"{repo} — clean git tree")


def _check_task(settings) -> Check:
    from .cli import _resolve_task

    try:
        task = _resolve_task(settings.task_path)
    except Exception as e:
        return Check("task", FAIL, f"{settings.task_path}: {str(e)[:140]}", "ratchet demo")
    if not task.f2p_all:
        return Check("task", FAIL, f"{task.task_id} declares no fail-to-pass tests — nothing to grade",
                     "add f2p_visible to the task yaml")
    return Check("task", OK, f"{task.task_id} — {len(task.f2p_visible)} visible + {len(task.f2p_hidden)} hidden f2p, {len(task.p2p)} p2p")


def _check_runner(settings) -> Check:
    """The gauntlet shells out to the test command; if that binary is missing every
    node scores zero and the search looks broken rather than unconfigured."""
    from .cli import _resolve_task

    try:
        cmd = _resolve_task(settings.task_path).test_cmd
    except Exception:
        return Check("test runner", WARN, "skipped — task did not load")
    exe = cmd.split()[0] if cmd else ""
    if not exe:
        return Check("test runner", WARN, "the task declares no test command")

    # The sandbox does not inherit this process's PATH. WorktreeProvider puts the
    # warm venv at <repo>/.ratchet/venv on PATH for every node, which is how `python`
    # resolves on a macOS host that only ships `python3` -- so checking the host PATH
    # alone reports a failure for a setup that grades green.
    venv_bin = Path(settings.repo) / ".ratchet" / "venv" / "bin"
    if (venv_bin / exe).exists():
        return Check("test runner", OK, f"{cmd}   (via the warm venv at {venv_bin.parent})")
    if shutil.which(exe) or Path(exe).exists():
        return Check("test runner", OK, f"{cmd}")
    hint = (f"build the warm venv the sandbox uses:  python3 -m venv {venv_bin.parent} && "
            f"{venv_bin}/pip install -e {settings.repo} pytest")
    return Check("test runner", FAIL, f"{exe!r} resolves neither on PATH nor in the warm venv "
                                      f"(test_cmd: {cmd})", hint)


def _check_sandbox(settings) -> Check:
    from .harness.sandboxes import describe_provider

    try:
        name, detail = describe_provider(settings)
    except Exception as e:
        return Check("sandbox", WARN, f"could not determine provider: {str(e)[:120]}")
    return Check("sandbox", OK, f"{name} — {detail}")


def _check_brightdata(settings) -> Check:
    if not settings.brightdata_api_key:
        return Check("bright data", WARN, "no BRIGHTDATA_API_KEY — the docs oracle and research scrape are offline",
                     "set BRIGHTDATA_API_KEY in .env")
    return Check("bright data", OK, f"key present, unlocker zone {settings.brightdata_unlocker_zone}")


def run(settings, *, live: bool = True) -> tuple[list[Check], bool]:
    """Every check. Returns the checks and whether a run is viable."""
    checks: list[Check] = []
    harness, available = _check_harness(settings)
    checks.append(harness)
    checks.extend(_check_routing(settings, available))
    if live and available:
        checks.append(_check_live_call(settings, available))
    checks.append(_check_git(settings))
    checks.append(_check_task(settings))
    checks.append(_check_runner(settings))
    checks.append(_check_sandbox(settings))
    checks.append(_check_brightdata(settings))
    ok = not any(c.status == FAIL for c in checks)
    return checks, ok


def render(checks: list[Check], ok: bool) -> str:
    lines = ["", "  ratchet doctor", ""]
    lines += [c.render() for c in checks]
    lines.append("")
    lines.append("  ready to run" if ok else "  not ready — fix the FAIL lines above")
    lines.append("")
    return "\n".join(lines)
