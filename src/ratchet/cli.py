"""`ratchet` -- one entry point for every process in the system.

    ratchet serve     the MCP server the harness talks to
    ratchet run       create the TrueForge session and drive the task
    ratchet console   the TUI
    ratchet verify    grade a diff from the command line (no model involved)
    ratchet demo      seed the demo repo, including the deliberately-cheating branch

`verify` matters more than it looks: it means the whole verification story can be
demonstrated, and unit-tested, with no model, no API key and no network.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from .bus import Bus
from .config import Settings, load_task
from .gauntlet.runner import Backend, Pawl, docker_available
from .workspace import Workspace


def cmd_serve(args: argparse.Namespace) -> int:
    from .mcp_server import build_server

    s = Settings.from_env()
    if args.task:
        s.task_path = args.task
    if args.repo:
        s.repo_path = args.repo
    s.run_id = args.run_id or s.run_id
    mcp, svc = build_server(s)
    print(f"ratchet mcp server on http://{s.mcp_host}:{s.mcp_port}/mcp   run={svc.run_id}  backend={svc.pawl.backend.value}")
    print(f"bus: {svc.bus.path}")
    mcp.settings.host = s.mcp_host
    mcp.settings.port = s.mcp_port
    mcp.run(transport="streamable-http")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .harness.orchestrator import Orchestrator

    s = Settings.from_env()
    if args.task:
        s.task_path = args.task
    run_id = args.run_id or f"run-{uuid.uuid4().hex[:6]}"
    bus = Bus(Path(s.repo_path) / ".ratchet" / f"{run_id}.bus.jsonl")
    orch = Orchestrator(s, bus, run_id)
    task = load_task(s.task_path)
    first = (
        "Start by calling task_brief, then status. Work the task using repo_read, repo_grep and dry_run. "
        "When you believe you have a fix, call propose_patch. You cannot mark yourself finished -- "
        f"the verifier decides. Task id: {task.task_id}."
    )
    print(f"run {run_id}: driving TrueForge at {s.trueforge_base_url}")
    orch.start(first)
    return 0


def cmd_console(args: argparse.Namespace) -> int:
    from .tui.app import RatchetApp

    repo = Path(args.repo or ".")
    bus_path = Path(args.bus) if args.bus else _latest_bus(repo)
    if not bus_path:
        print("no run found; start `ratchet serve` first", file=sys.stderr)
        return 1
    RatchetApp(bus_path, repo).run()
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Grade a diff with no model in the loop. This is the honest core of the demo."""
    task = load_task(args.task)
    diff = Path(args.diff).read_text() if args.diff else sys.stdin.read()
    backend = Backend.DOCKER if (args.backend == "docker" and docker_available()) else Backend.LOCAL
    pawl = Pawl(backend=backend, image=task.image)
    worktree = Path(args.repo or task.repo_path)
    base = args.base or "HEAD"
    # load the graded tests so patchlint can spot literals lifted out of them
    ws = Workspace.create(worktree, "cli")
    v = pawl.run_gauntlet(
        task=task,
        worktree=worktree,
        base_commit=base,
        diff=diff,
        branch="cli",
        test_sources=ws.read_test_sources(None, task.protected_paths),
        type_cmd=None,
        lint_cmd=None,
    )
    print(v.to_observation())
    return 0 if v.green else 1


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import seed

    seed(Path(args.dir or "demo-repo"))
    return 0


def _latest_bus(repo: Path) -> Path | None:
    d = repo / ".ratchet"
    if not d.exists():
        return None
    buses = sorted(d.glob("*.bus.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return buses[0] if buses else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser("ratchet", description="a coding agent that cannot declare itself finished")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the MCP server the harness talks to")
    p.add_argument("--task")
    p.add_argument("--repo")
    p.add_argument("--run-id")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("run", help="create a TrueForge session and drive the task")
    p.add_argument("--task")
    p.add_argument("--run-id")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("console", help="the TUI")
    p.add_argument("--bus")
    p.add_argument("--repo")
    p.set_defaults(fn=cmd_console)

    p = sub.add_parser("verify", help="grade a diff, no model involved")
    p.add_argument("--task", required=True)
    p.add_argument("--diff")
    p.add_argument("--repo")
    p.add_argument("--base")
    p.add_argument("--backend", default="docker")
    p.add_argument("--fast", action="store_true")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("demo", help="seed the demo repository")
    p.add_argument("--dir")
    p.set_defaults(fn=cmd_demo)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
