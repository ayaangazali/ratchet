"""`ratchet` — one entry point for every part of the system.

    ratchet go <repo-url>                          clone, detect, probe, and start
    ratchet run "fix the auth token refresh bug"   search until green or budget out
    ratchet tree                                   the search tree, scores, live/pruned
    ratchet rewind <node>                          restore that state and branch from it
    ratchet diff                                   the squashed patch on the winning path
    ratchet verify                                 the gauntlet standalone, no agent
    ratchet ship                                   approval gate -> pull request
    ratchet replay <run>                           re-render a finished run from its bus

    ratchet bench-snapshot                         the 11:15 decision: tree or fallback
    ratchet redteam                                score the verifier against known cheats
    ratchet audit                                  verify a run's receipt chain
    ratchet evals                                  linear vs search, on our own bug suite
    ratchet console                                the TUI
    ratchet dashboard                              the same run, in a browser
    ratchet demo                                   seed the demo repository

`verify` matters more than it looks. It means the entire verification story can be
demonstrated, and unit-tested, with no model, no API key and no network — which is
what makes the demo survivable and the claim checkable.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from .config import Settings, load_task
from .sandbox import WorktreeProvider, bench_snapshot


def _run_id(args) -> str:
    return getattr(args, "run_id", None) or f"run-{uuid.uuid4().hex[:6]}"


def _provider(settings: Settings, repo: Path, run_id: str):
    """Harness first, worktree fallback. `bench-snapshot` is how you choose."""
    if settings.provider in ("harness", "auto"):
        try:
            from .harness.client import TrueForgeClient
            from .harness.sandboxes import harness_provider

            client = TrueForgeClient(settings.trueforge_base_url)
            prov = harness_provider(client, repo)
            if prov is not None:
                return prov
        except Exception as e:  # pragma: no cover - depends on a live harness
            if settings.provider == "harness":
                raise SystemExit(f"harness sandbox provider unavailable: {e}") from e
    return WorktreeProvider(repo, run_id, venv=Path(settings.venv) if settings.venv else None)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def cmd_go(args) -> int:
    """One command from a URL to a live search.

    Everything it needs is observable, so it observes it: the framework from the
    files, the environment from the manifest, and the task itself from which tests
    are red right now. See `onboard.py` for why the held-out split is interleaved.
    """
    from .onboard import go

    try:
        return go(
            args.url,
            workdir=Path(args.dir).resolve() if args.dir else None,
            goal=args.goal,
            run=not args.no_run,
            console=not args.no_console,
            budget=args.budget,
            scripted=args.scripted,
            probe_timeout=args.probe_timeout,
        )
    except RuntimeError as e:  # a clone that did not happen is not a stack trace
        print(f"\n  {e}", file=sys.stderr)
        return 2


def cmd_run(args) -> int:
    from .bus import Bus
    from .loop import SearchRun
    from .scheduler import Scheduler
    from .subagents import ModelBackend, ScriptedBackend, Subagents

    s = Settings.from_env()
    if args.task:
        s.task_path = args.task
    if args.repo:
        s.repo = args.repo
    if args.budget:
        s.max_nodes = args.budget
    task = load_task(s.task_path)
    if args.goal:
        task.statement = args.goal
    repo = Path(s.repo).resolve()
    run_id = _run_id(args)

    backend: ModelBackend
    if args.scripted:
        backend = ScriptedBackend(json.loads(Path(args.scripted).read_text()))
    else:
        from .harness.backend import HarnessBackend
        from .harness.client import TrueForgeClient

        backend = HarnessBackend(TrueForgeClient(s.trueforge_base_url))

    agents = Subagents(backend, s.roles())
    scheduler = Scheduler(s.budget(), patience=s.patience)
    if args.fanout:
        scheduler.patience = 0 if args.fanout > 1 else scheduler.patience

    run = SearchRun(
        task=task,
        repo=repo,
        provider=_provider(s, repo, run_id),
        subagents=agents,
        run_id=run_id,
        scheduler=scheduler,
        bus=Bus(repo / ".ratchet" / f"{run_id}.bus.jsonl"),
        parallel=s.parallel,
    )
    print(f"run {run_id} · task {task.task_id} · provider {run.provider.name}")
    result = run.run()
    print(f"\n{result.stopped_because}")
    print(f"winner {result.winner.id} · score {result.winner.score:.3f} · {len(result.tree)} nodes explored")
    print(run.scheduler.budget.line())
    if result.green and not args.no_ship:
        req, decision = run.request_ship(result.winner)
        print(f"approval {req.id}: {'approved' if decision.allow else 'denied'} {decision.reason}")
    return 0 if result.green else 2


def cmd_tree(args) -> int:
    from .node import Tree

    path = _latest(Path(args.repo or "."), "tree.json", args.run)
    if not path:
        print("no run found", file=sys.stderr)
        return 1
    tree = Tree.load(path)
    live = max((n for n in tree if n.alive), key=lambda n: n.score, default=None)
    for line, _style in tree.render(live_id=live.id if live else None):
        print(line)
    print(f"\n{len(tree)} nodes · {sum(1 for n in tree if n.pruned)} pruned · "
          f"best {tree.best().id} at {tree.best().score:.2f}")
    return 0


def cmd_rewind(args) -> int:
    from .gitstate import GitState
    from .node import Tree

    repo = Path(args.repo or ".").resolve()
    path = _latest(repo, "tree.json", args.run)
    if not path:
        print("no run found", file=sys.stderr)
        return 1
    tree = Tree.load(path)
    node = tree.get(args.node)
    run_id = path.name.split(".")[0]
    git = GitState(repo=repo, run_id=run_id, trunk=f"ratchet/{run_id}/trunk")
    git.restore(node.commit)
    node.untried = True  # it becomes expandable again; that is the point of rewinding
    node.pruned = False
    tree.save()
    print(f"restored {node.id} at {node.commit[:10]} · score {node.score:.2f} · intent: {node.intent}")
    print("the branch is live again; `ratchet run` will expand from here")
    return 0


def cmd_diff(args) -> int:
    from .gitstate import GitState
    from .node import Tree

    repo = Path(args.repo or ".").resolve()
    path = _latest(repo, "tree.json", args.run)
    if not path:
        print("no run found", file=sys.stderr)
        return 1
    tree = Tree.load(path)
    winner = tree.get(args.node) if args.node else tree.best()
    run_id = path.name.split(".")[0]
    git = GitState(repo=repo, run_id=run_id, trunk=f"ratchet/{run_id}/trunk")
    print(git.squashed_diff(tree.root.commit, winner.commit))
    return 0


def cmd_verify(args) -> int:
    """The gauntlet, standalone. No model, no key, no network."""
    from .verifier.gauntlet import Gauntlet

    task = load_task(args.task)
    repo = Path(args.repo or task.repo_path).resolve()
    patch = Path(args.diff).read_text() if args.diff else sys.stdin.read()
    run_id = f"verify-{uuid.uuid4().hex[:4]}"
    provider = WorktreeProvider(repo, run_id)
    base = args.base or provider.base_image()
    sources = {}
    for pat in task.protected_paths:
        b = repo / pat.rstrip("/")
        if b.is_dir():
            sources.update({str(p.relative_to(repo)): p.read_text(errors="replace") for p in b.rglob("*.py")})
    sb = provider.fork(base, label="verify")
    try:
        result = Gauntlet(task, repo_dir=".", test_sources=sources).run(sb, patch, base_commit=base)
    finally:
        sb.destroy()
        provider.cleanup()
    print(result.to_observation())
    print(f"\n{result.reason}")
    return 0 if result.green else 1


def cmd_ship(args) -> int:
    from .gate import Gate
    from .gitstate import GitState
    from .node import Tree

    repo = Path(args.repo or ".").resolve()
    path = _latest(repo, "tree.json", args.run)
    if not path:
        print("no run found", file=sys.stderr)
        return 1
    tree = Tree.load(path)
    winner = tree.get(args.node) if args.node else tree.best()
    run_id = path.name.split(".")[0]
    git = GitState(repo=repo, run_id=run_id, trunk=f"ratchet/{run_id}/trunk")
    diff = git.squashed_diff(tree.root.commit, winner.commit)
    gate = Gate(repo)
    req = gate.request(action="open_pull_request", summary=winner.intent or "ratchet fix", diff=diff,
                       stats={"score": winner.score, "green": winner.green, "nodes": len(tree)})
    print(f"approval requested: {req.id}")
    print(f"  approve: echo '{{\"allow\": true}}' > .ratchet/approvals/{req.id}.json")
    decision = gate.wait(req, timeout_s=args.timeout)
    if not decision.allow:
        print(f"denied: {decision.reason}")
        return 1
    sha = git.squash(tree.root.commit, winner.commit, f"ratchet: {winner.intent}\n\nscore {winner.score:.2f}")
    print(f"squashed to {sha[:10]} on ratchet/{run_id}/ship")
    print("push and open the PR from there when you are ready")
    return 0


def cmd_replay(args) -> int:
    import time as _t

    from .bus import Bus

    repo = Path(args.repo or ".").resolve()
    path = Path(args.bus) if args.bus else _latest(repo, "bus.jsonl", args.run)
    if not path:
        print("no run found", file=sys.stderr)
        return 1
    events = Bus(path).read_all()
    keep = ("run_id", "task", "provider", "node", "id", "parent", "score", "green", "outcome", "intent",
            "model", "stage", "passed", "detail", "label", "fanout", "depth", "findings", "reason",
            "winner", "library", "new_section", "approved")
    t0 = events[0].ts if events else 0
    try:
        for e in events:
            if args.speed > 0:
                _t.sleep(min(2.0, (e.ts - t0) / args.speed))
                t0 = e.ts
            payload = {k: v for k, v in e.payload.items() if k in keep}
            print(f"{e.kind:<18} {json.dumps(payload)}")
    except BrokenPipeError:
        pass
    return 0


def cmd_bench(args) -> int:
    s = Settings.from_env()
    repo = Path(args.repo or s.repo).resolve()
    provider = _provider(s, repo, f"bench-{uuid.uuid4().hex[:4]}")
    res = bench_snapshot(provider, rounds=args.rounds)
    print(res.render())
    if hasattr(provider, "cleanup"):
        provider.cleanup()
    return 0


def cmd_redteam(args) -> int:
    from . import redteam

    repo = Path(args.repo or "demo-repo").resolve()
    demo_task = load_task(args.task or "tasks/demo-001-slugify/task.yaml")
    canary = load_task(args.canary or "tasks/canary-impossible/task.yaml")
    results = redteam.run(repo, demo_task, canary)
    print(redteam.report(results))
    return 0 if all(r.correct for r in results) else 1


def cmd_audit(args) -> int:
    from .receipts import ReceiptBook

    repo = Path(args.repo or ".").resolve()
    path = Path(args.receipts) if args.receipts else _latest(repo, "receipts.jsonl", args.run)
    if not path:
        print("no receipts found", file=sys.stderr)
        return 1
    book = ReceiptBook(path)
    ok, problems = book.verify()
    for k, v in book.summary().items():
        print(f"  {k:<14}{v}")
    if ok:
        print("\nchain intact: every result is in the order it was issued and none has been edited.")
        return 0
    print("\nCHAIN BROKEN:")
    for p in problems:
        print(f"  - {p}")
    return 1


def cmd_evals(args) -> int:
    from .evals.suite import run_suite

    return run_suite(Path(args.repo or "demo-repo").resolve(), trials=args.trials, verbose=True)


def cmd_console(args) -> int:
    from .tui.app import RatchetApp

    repo = Path(args.repo or ".").resolve()
    bus_path = Path(args.bus) if args.bus else _latest(repo, "bus.jsonl", args.run)
    if not bus_path:
        print("no run found; start one with `ratchet run`, or `make fixture` for a recorded one", file=sys.stderr)
        return 1
    RatchetApp(bus_path, repo).run()
    return 0


def cmd_dashboard(args) -> int:
    """The console's twin. Same bus, same palette, same approval gate -- the only
    difference is that a browser can be handed to somebody who does not want a
    terminal put in front of them."""
    from .dashboard import serve

    repo = Path(args.repo or ".").resolve()
    bus_path = Path(args.bus) if args.bus else _latest(repo, "bus.jsonl", args.run)
    if not bus_path:
        print("no run found; start one with `ratchet run`, or `make fixture` for a recorded one", file=sys.stderr)
        return 1
    serve(bus_path, repo, host=args.host, port=args.port)
    return 0


def cmd_demo(args) -> int:
    from .demo import seed

    seed(Path(args.dir or "demo-repo"))
    return 0


# --------------------------------------------------------------------------- #


def _latest(repo: Path, suffix: str, run: str | None = None) -> Path | None:
    d = repo / ".ratchet"
    if not d.exists():
        return None
    pattern = f"{run}.{suffix}" if run else f"*.{suffix}"
    files = sorted(d.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser("ratchet", description="a coding agent that cannot decide it is done")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("go", help="clone a repository, work out the task, and start searching")
    p.add_argument("url", help="a GitHub URL, `owner/repo`, or a local path")
    p.add_argument("--goal", help="override the auto-written task statement")
    p.add_argument("--dir", help="where to clone (default ./.ratchet-work)")
    p.add_argument("--budget", type=int, help="max nodes")
    p.add_argument("--scripted", help="canned model responses; runs with no harness")
    p.add_argument("--probe-timeout", type=int, default=600,
                   help="seconds to let the repository's own suite run (default 600)")
    p.add_argument("--no-run", action="store_true", help="write the task and stop")
    p.add_argument("--no-console", action="store_true", help="search without attaching the TUI")
    p.set_defaults(fn=cmd_go)

    p = sub.add_parser("run", help="search until green or the budget runs out")
    p.add_argument("goal", nargs="?", help="override the task statement")
    p.add_argument("--task")
    p.add_argument("--repo")
    p.add_argument("--run-id")
    p.add_argument("--fanout", type=int, help="force parallel exploration from the start")
    p.add_argument("--budget", type=int, help="max nodes")
    p.add_argument("--scripted", help="a JSON list of canned model responses; runs with no harness")
    p.add_argument("--no-ship", action="store_true", help="stop before the approval gate")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("tree", help="the search tree")
    p.add_argument("--repo")
    p.add_argument("--run")
    p.set_defaults(fn=cmd_tree)

    p = sub.add_parser("rewind", help="restore a node and branch from it")
    p.add_argument("node")
    p.add_argument("--repo")
    p.add_argument("--run")
    p.set_defaults(fn=cmd_rewind)

    p = sub.add_parser("diff", help="the squashed patch on the winning path")
    p.add_argument("--node")
    p.add_argument("--repo")
    p.add_argument("--run")
    p.set_defaults(fn=cmd_diff)

    p = sub.add_parser("verify", help="the gauntlet standalone, no agent")
    p.add_argument("--task", required=True)
    p.add_argument("--diff")
    p.add_argument("--repo")
    p.add_argument("--base")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("ship", help="approval gate, then squash for the PR")
    p.add_argument("--node")
    p.add_argument("--repo")
    p.add_argument("--run")
    p.add_argument("--timeout", type=float, default=900)
    p.set_defaults(fn=cmd_ship)

    p = sub.add_parser("replay", help="re-render a finished run from its bus file")
    p.add_argument("--bus")
    p.add_argument("--repo")
    p.add_argument("--run")
    p.add_argument("--speed", type=float, default=0.0, help="0 = instant, 1 = real time, 4 = 4x")
    p.set_defaults(fn=cmd_replay)

    p = sub.add_parser("bench-snapshot", help="time a fork round trip; decides tree vs fallback")
    p.add_argument("--repo")
    p.add_argument("--rounds", type=int, default=3)
    p.set_defaults(fn=cmd_bench)

    p = sub.add_parser("redteam", help="fire known cheating patches at the verifier and score it")
    p.add_argument("--repo")
    p.add_argument("--task")
    p.add_argument("--canary")
    p.set_defaults(fn=cmd_redteam)

    p = sub.add_parser("audit", help="verify a run's receipt chain")
    p.add_argument("--receipts")
    p.add_argument("--repo")
    p.add_argument("--run")
    p.set_defaults(fn=cmd_audit)

    p = sub.add_parser("evals", help="linear vs search on our own seeded bugs")
    p.add_argument("--repo")
    p.add_argument("--trials", type=int, default=5)
    p.set_defaults(fn=cmd_evals)

    p = sub.add_parser("console", help="the TUI")
    p.add_argument("--bus")
    p.add_argument("--repo")
    p.add_argument("--run")
    p.set_defaults(fn=cmd_console)

    p = sub.add_parser("dashboard", help="the same run, in a browser")
    p.add_argument("--bus")
    p.add_argument("--repo")
    p.add_argument("--run")
    p.add_argument("--host", default="127.0.0.1", help="loopback by default: this endpoint can approve a pull request")
    p.add_argument("--port", type=int, default=8788)
    p.set_defaults(fn=cmd_dashboard)

    p = sub.add_parser("demo", help="seed the demo repository")
    p.add_argument("--dir")
    p.set_defaults(fn=cmd_demo)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
