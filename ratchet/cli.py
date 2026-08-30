"""`ratchet` — one entry point for every part of the system.

    ratchet go <repo-url>                          clone, detect, probe, and start
    ratchet run "fix the auth token refresh bug"   search until green or budget out
    ratchet tree                                   the search tree, scores, live/pruned
    ratchet rewind <node>                          restore that state and branch from it
    ratchet diff                                   the squashed patch on the winning path
    ratchet verify                                 the gauntlet standalone, no agent
    ratchet ship                                   approval gate -> pull request
    ratchet replay <run>                           re-render a finished run from its bus

    ratchet graph --file <graph.yaml>              an objective graph: nodes fulfilled only by tests
    ratchet docs <library>                         upstream docs via Bright Data, pinned to the lockfile
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

from .config import Settings, load_task, resolve_data_path
from .sandbox import WorktreeProvider, bench_snapshot


def _run_id(args) -> str:
    return getattr(args, "run_id", None) or f"run-{uuid.uuid4().hex[:6]}"


def _resolve_task(spec: str):
    """A task path on disk, or one of the specs shipped inside the package.

    An installed `ratchet` has no repo checkout, so `tasks/demo-001-slugify/task.yaml`
    resolves to the packaged copy when the path does not exist. Accepts the repo
    path, a bare name (`demo-001-slugify`), or a real file.
    """
    from importlib import resources

    if Path(spec).exists():
        return load_task(spec)
    name = Path(spec).parent.name if Path(spec).name == "task.yaml" else Path(spec).stem
    packaged = resources.files("ratchet") / "tasks" / f"{name}.yaml"
    if packaged.is_file():
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(packaged.read_text())
        return load_task(fh.name)
    have = [f.name for f in (resources.files("ratchet") / "tasks").iterdir()]
    raise SystemExit(f"no task at {spec!r} and no packaged task named {name!r}; packaged: {have}")


def _docs_oracle(settings, repo: Path, bus):
    """The Bright Data docs oracle, or None when no key is configured.

    Built only when BRIGHTDATA_API_KEY is set, so an offline run is a clean no-op
    rather than a failed fetch. When present, a red verdict that looks like API
    drift attaches current upstream docs for the pinned version to the next prompt.
    """
    if not settings.brightdata_api_key:
        return None
    from .docs import DocsOracle

    return DocsOracle(repo, bus, settings)


def _qodo_oracle(settings, repo: Path, bus):
    """Hosted Qodo review context, or None. Advisory only -- never gates anything.

    Built only when RATCHET_QODO is on, `gh` is on PATH and the repo has a GitHub
    remote, so an offline or remote-less run is a clean no-op."""
    from .qodo import oracle_or_none

    return oracle_or_none(settings, repo, bus)


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


def _library(settings, repo: Path):
    from .config import resolve_data_path
    from .research.skills import SkillLibrary

    return SkillLibrary.load(resolve_data_path(settings.skills_dir))


def _backend(settings, scripted: str | None):
    from .subagents import ScriptedBackend

    if scripted:
        return ScriptedBackend(json.loads(Path(scripted).read_text()))
    from .harness.backend import HarnessBackend
    from .harness.client import TrueForgeClient

    return HarnessBackend(TrueForgeClient(settings.trueforge_base_url))


def _papers(scraper, query: str, limit: int):
    """Both sources through Bright Data, merged and ranked. Problems are reported,
    not raised: half a reading list beats a traceback."""
    from .research.sources import rank

    found, problems = [], []
    for source in ("arxiv", "huggingface"):
        papers, probs = scraper.search(query, limit=limit * 2, source=source)
        found += papers
        problems += [f"{source}: {p}" for p in probs]
    return rank(found, query, limit=limit), problems


def cmd_doctor(args) -> int:
    """Check everything a run depends on, before the run depends on it."""
    from . import doctor

    s = Settings.from_env()
    if args.repo:
        s.repo = args.repo
    if args.task:
        s.task_path = args.task
    # `demo-repo` is a default, not a promise about the current directory.
    s.repo = str(resolve_data_path(s.repo))
    s.task_path = str(resolve_data_path(s.task_path))
    checks, ok = doctor.run(s, live=not args.offline)
    print(doctor.render(checks, ok))
    return 0 if ok else 1


def cmd_research(args) -> int:
    """Read the literature, turn it into skills, and make each one prove itself."""
    from .research.distill import Distiller
    from .research.scrape import PaperScraper
    from .research.skills import ADOPTED, REJECTED

    s = Settings.from_env()
    repo = Path(args.repo or s.repo).resolve()
    lib = _library(s, repo)
    scraper = PaperScraper(s)

    if args.research_cmd == "list":
        if not len(lib):
            print(f"no skills in {lib.root}/ yet — try `ratchet research distill \"<topic>\"`")
            return 0
        print(f"\n  {len(lib)} skill(s) in {lib.root}/\n")
        for sk in sorted(lib, key=lambda x: (x.status, x.name)):
            print(sk.one_line())
        print("\n  ✓ adopted (in every prompt)   · proposed (never sent)   ✗ rejected (kept as a record)\n")
        return 0

    if args.research_cmd == "show":
        sk = lib.get(args.name)
        if not sk:
            print(f"no skill {args.name!r}; `ratchet research list` shows what there is", file=sys.stderr)
            return 1
        print(sk.to_markdown())
        return 0

    if args.research_cmd == "search":
        papers, problems = _papers(scraper, args.query, args.limit)
        for why in problems:
            print(f"  ! {why}")
        if not papers:
            print("nothing found — check BRIGHTDATA_API_KEY and the zones in ratchet/scrapers.yaml")
            return 1
        print(f"\n  {len(papers)} paper(s) for {args.query!r}\n")
        for pp in papers:
            print(f"  {pp.one_line()}")
        print(f"\n  distill them:  ratchet research distill {args.query!r}\n")
        return 0

    if args.research_cmd == "distill":
        papers, problems = _papers(scraper, args.query, args.limit)
        for why in problems:
            print(f"  ! {why}")
        if not papers:
            print("nothing found to distill", file=sys.stderr)
            return 1
        papers = [scraper.enrich(p) for p in papers]
        print(f"\n  reading {len(papers)} paper(s) for {args.query!r}\n")
        d = Distiller(_backend(s, args.scripted), s.model_researcher)
        kept = []
        for pp in papers:
            print(f"  · {pp.one_line()}")
            sk = d.distill(pp)
            if sk is None:
                continue
            path = lib.save(sk)
            kept.append(sk)
            print(f"      -> {sk.kind}: {sk.name}   {path}")
        print()
        for pp, why in d.skipped:
            print(f"  no technique · {pp.id}  {why}")
        print(f"\n  {len(kept)} proposed skill(s), {len(d.skipped)} paper(s) with nothing actionable.")
        if kept:
            print("  They are PROPOSED and reach no prompt. Prove one:")
            print(f"    ratchet research trial {kept[0].slug()} --task <task.yaml> --repo <repo>\n")
        return 0

    if args.research_cmd == "trial":
        from .research.trial import run_trial
        from .subagents import Subagents

        sk = lib.get(args.name)
        if not sk:
            print(f"no skill {args.name!r}", file=sys.stderr)
            return 1
        task = _resolve_task(args.task or s.task_path)
        backend = _backend(s, args.scripted)
        agents = Subagents(backend, s.roles())
        out = run_trial(
            sk, task=task, repo=repo, subagents=agents,
            provider_factory=lambda rid: _provider(s, repo, rid),
            n=args.trials, max_nodes=args.budget,
        )
        print(out.report())
        if out.n:
            sk.trial = out.to_trial()
            sk.status = ADOPTED if out.verdict == ADOPTED else REJECTED
            path = lib.save(sk)
            print(f"\n  {path} updated: status {sk.status}")
            print("  commit it — the verdict is evidence, and it belongs in the diff.\n")
            return 0
        print("\n  status unchanged (still proposed).\n")
        return 3

    print("unknown research subcommand", file=sys.stderr)
    return 1
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
    task = _resolve_task(s.task_path)
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
        from .providers import trueforge_alive

        # a raw ConnectError sixty seconds into a run is not an error message
        if not trueforge_alive(ttl=0):
            raise SystemExit(
                f"no TrueForge answering at {s.trueforge_base_url} — start it with\n"
                "  npx @truefoundry/trueforge@latest\n"
                "or run offline: ratchet run --repo demo-repo --scripted demo-repo/patches/scripted.json"
            )
        backend = HarnessBackend(TrueForgeClient(s.trueforge_base_url))

    agents = Subagents(backend, s.roles())
    scheduler = Scheduler(s.budget(), patience=s.patience)
    if args.fanout:
        scheduler.patience = 0 if args.fanout > 1 else scheduler.patience

    bus = Bus(repo / ".ratchet" / f"{run_id}.bus.jsonl")
    run = SearchRun(
        task=task,
        repo=repo,
        provider=_provider(s, repo, run_id),
        subagents=agents,
        run_id=run_id,
        scheduler=scheduler,
        bus=bus,
        docs=_docs_oracle(s, repo, bus),
        qodo=_qodo_oracle(s, repo, bus),
        skills=None if args.no_skills else _library(s, repo),
        parallel=s.parallel,
    )
    adopted = [x for x in (run.skills or []) if x.adopted]
    print(f"run {run_id} · task {task.task_id} · provider {run.provider.name}"
          + (f" · {len(adopted)} adopted skill(s)" if adopted else ""))
    result = run.run()
    print(f"\n{result.stopped_because}")
    print(f"winner {result.winner.id} · score {result.winner.score:.3f} · {len(result.tree)} nodes explored")
    print(run.scheduler.budget.line())
    if result.green and not args.no_ship:
        def _announce(req):
            print(f"\napproval {req.id} pending — nothing ships until you answer:")
            print(f"  approve: echo '{{\"allow\": true}}' > {repo}/.ratchet/approvals/{req.id}.json")
            print(f"  deny:    echo '{{\"allow\": false}}' > {repo}/.ratchet/approvals/{req.id}.json")
            print(f"  waiting up to {args.gate_timeout:.0f}s (--gate-timeout, or --no-ship to skip the gate)")

        req, decision = run.request_ship(result.winner, timeout_s=args.gate_timeout, on_request=_announce)
        print(f"approval {req.id}: {'approved' if decision.allow else 'denied'} {decision.reason}")
    return 0 if result.green else 2


def cmd_graph(args) -> int:
    """Run an objective graph: each node fulfilled only by its own tests."""
    from .bus import Bus
    from .objective import GraphRun, decompose, load_graph
    from .subagents import ModelBackend, ScriptedBackend, Subagents

    s = Settings.from_env()
    if args.repo:
        s.repo = args.repo
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

    if bool(args.decompose) == bool(args.file):
        print("exactly one of --file or --decompose is required", file=sys.stderr)
        return 2
    if args.decompose:
        # review-before-run is the contract: a model-produced graph is written to
        # --out for a human to read; it is never executed in the same breath
        if not args.out:
            print("--decompose requires --out: the decomposed graph must be reviewed before it runs", file=sys.stderr)
            return 2
        graph, block = decompose(args.decompose, repo, agents)
        Path(args.out).write_text(block)
        print(f"decomposed graph validated and written to {args.out}")
        print(" · ".join(graph.order))
        print(f"review it, then run: ratchet graph --file {args.out} --repo {repo}")
        return 0
    graph = load_graph(Path(args.file), repo)

    gbus = Bus(repo / ".ratchet" / f"{run_id}.bus.jsonl")
    run = GraphRun(
        graph=graph,
        repo=repo,
        provider=_provider(s, repo, run_id),
        subagents=agents,
        run_id=run_id,
        bus=gbus,
        escalation_budget=s.budget(),
        parallel=s.parallel,
        docs=_docs_oracle(s, repo, gbus),
    )
    print(f"graph {graph.graph_id} · run {run_id} · {len(graph.order)} node(s) · provider {run.provider.name}")
    summary = run.run()
    for node_id in graph.order:
        n = graph.nodes[node_id]
        mark = {"fulfilled": "✓", "failed": "✗", "blocked": "∅"}.get(n.status, "·")
        extra = " (escalated to tree search)" if n.escalated else ""
        print(f"  {mark} {node_id:<14} {n.status:<10} attempts {n.attempts}{extra}")
    print("all fulfilled" if summary.all_fulfilled else "NOT fulfilled", "·", json.dumps(summary.to_dict()))
    return 0 if summary.all_fulfilled else 2


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

    task = _resolve_task(args.task)
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
            "winner", "library", "new_section", "approved", "pr", "counts")
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
    demo_task = _resolve_task(args.task or "tasks/demo-001-slugify/task.yaml")
    canary = _resolve_task(args.canary or "tasks/canary-impossible/task.yaml")
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
    if not bus_path and args.repo is None and (Path("demo-repo") / ".ratchet").exists():
        # bare `ratchet` inside a checkout: the runs live in demo-repo/
        repo = Path("demo-repo").resolve()
        bus_path = _latest(repo, "bus.jsonl", args.run)
    if not bus_path:
        # no run yet is not an error -- open the console anyway, on an empty bus,
        # and let the idle splash say what to do next. Inside a git repo the bus
        # lives with the project; anywhere else (say, $HOME) it goes to the cache
        # dir rather than littering the directory you happened to be in.
        from .gitstate import is_repo

        base = repo if is_repo(repo) else Path.home() / ".cache" / "ratchet"
        bus_path = base / ".ratchet" / "session.bus.jsonl" if is_repo(repo) else base / "session.bus.jsonl"
        bus_path.parent.mkdir(parents=True, exist_ok=True)
        bus_path.touch(exist_ok=True)
    RatchetApp(bus_path, repo).run()
    return 0


def cmd_docs(args) -> int:
    """Exercise the Bright Data docs oracle: fetch, extract by heading, validate,
    and self-heal on drift -- the whole pipeline, standalone."""
    from .bus import Bus
    from .docs import DocsOracle

    s = Settings.from_env()
    repo = Path(args.repo or s.repo).resolve()
    if not s.brightdata_api_key:
        print("BRIGHTDATA_API_KEY is not set. Add it to .env; see ratchet/scrapers.yaml for sources.",
              file=sys.stderr)
        return 1
    bus = Bus(repo / ".ratchet" / f"docs-{uuid.uuid4().hex[:6]}.bus.jsonl")
    oracle = DocsOracle(repo, bus, s)
    print(oracle.lookup(args.library, symbol=args.symbol or "", topic=args.topic or ""))
    return 0


def cmd_qodo_mcp(args) -> int:
    """The QODO MCP server, stdio. Register once; any MCP client drives reviews."""
    from .qodo_mcp import main as qodo_mcp_main

    qodo_mcp_main()
    return 0


def cmd_qodo_fix(args) -> int:
    """The review->revise loop: Qodo reviews the PR, its per-finding agent prompts
    drive chat turns, the push waits at the human gate, then Qodo re-reviews.

    Advisory in, gated out: findings only ever become prompts, and nothing is
    pushed without a yes at the gate (invariant 7).
    """
    import subprocess as sp

    from .bus import Bus
    from .chat import ChatSession
    from .gate import Gate
    from .qodo import QodoOracle

    repo = Path(args.repo or ".").resolve()
    bus = Bus(repo / ".ratchet" / "qodo-fix.bus.jsonl")
    oracle = QodoOracle(repo, bus)
    if not oracle.available():
        print("qodo-fix needs `gh` on PATH and a GitHub origin remote", file=sys.stderr)
        return 2
    pr = args.pr

    # the one real foot-gun: fixing PR N while a different branch is checked out
    head = sp.run(["gh", "pr", "view", str(pr), "--repo", str(oracle.slug),
                   "--json", "headRefName", "--jq", ".headRefName"],
                  capture_output=True, text=True, timeout=30).stdout.strip()
    branch = sp.run(["git", "-C", str(repo), "branch", "--show-current"],
                    capture_output=True, text=True, timeout=30).stdout.strip()
    if not head or head != branch:
        print(f"PR #{pr} head is {head or '?'} but the checked-out branch is {branch or '?'} -- "
              "check out the PR branch first", file=sys.stderr)
        return 2

    gate = Gate(repo, bus=bus)
    session = ChatSession(repo, bus=bus)  # the finding IS the prompt; no self-injection
    emit = lambda kind, text: print(f"  {text}")  # noqa: E731 - console narration

    review = oracle.latest_review(pr, fresh=True)
    if review is None or not review.findings:
        print(f"no Qodo review on PR #{pr} yet -- commanding one (/review, ~2 min)")
        oracle.trigger_review(pr)
        review = oracle.wait_for_review(pr, since=review.reviewed_at if review else "",
                                        timeout_s=args.timeout)
    if review is None or not review.findings:
        print("Qodo reports no findings -- nothing to fix")
        return 0

    for rnd in range(1, args.rounds + 1):
        todo = [f for f in review.findings if f.agent_prompt]
        print(f"round {rnd}: {len(review.findings)} finding(s), {len(todo)} with agent prompts")
        before = sp.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                        capture_output=True, text=True, timeout=30).stdout.strip()
        for f in todo:
            print(f"* fixing {f.n}. {f.title}")
            session.run_turn(f"Qodo review finding {f.n}: {f.title}\n\n{f.agent_prompt}", emit)
        after = sp.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                       capture_output=True, text=True, timeout=30).stdout.strip()
        if after == before:
            print("no turn produced a commit -- stopping")
            return 2

        diff = sp.run(["git", "-C", str(repo), "diff", f"{before}..{after}"],
                      capture_output=True, text=True, timeout=30).stdout
        req = gate.request(action="push",
                           summary=f"qodo-fix PR #{pr} round {rnd}: {len(todo)} finding(s) addressed",
                           diff=diff, stats={"pr": pr, "round": rnd, "findings": len(todo)})
        decision = gate.wait(req)
        if not decision.allow:
            print(f"push denied at the gate: {decision.reason or 'no reason given'}")
            return 2
        sp.run(["git", "-C", str(repo), "push"], check=True, timeout=120)
        print("pushed -- commanding a fresh Qodo review")

        since = review.reviewed_at
        oracle.trigger_review(pr)
        review = oracle.wait_for_review(pr, since=since, timeout_s=args.timeout)
        if review is None:
            print("no fresh review landed in time -- stopping")
            return 2
        if not review.findings:
            print(f"Qodo review is clean after round {rnd}")
            return 0
    print(f"rounds exhausted; {len(review.findings)} finding(s) remain")
    return 2


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


def cmd_export(args) -> int:
    """The session report, from outside the console."""
    from . import report

    repo = Path(args.repo or ".").resolve()
    bus_path = _latest(repo, "bus.jsonl", args.run)
    if args.json:
        print(report.to_json(repo))
        return 0
    path = report.write(repo, bus_path=bus_path)
    print(path)
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
    from . import __version__

    ap = argparse.ArgumentParser(
        "ratchet",
        description="a coding agent that cannot decide it is done. Bare `ratchet` opens the console.",
    )
    ap.add_argument("--version", action="version", version=f"ratchet {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=False)

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
    p.add_argument("--gate-timeout", type=float, default=900,
                   help="seconds to wait for a human at the approval gate (default 900)")
    p.add_argument("--no-skills", action="store_true", help="ignore skills/ for this run")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("doctor", help="check everything a run depends on, before it does not work")
    p.add_argument("--repo")
    p.add_argument("--task")
    p.add_argument("--offline", action="store_true", help="skip the live model call")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("research", help="read papers, distil skills, and trial them")
    rsub = p.add_subparsers(dest="research_cmd", required=True)

    q = rsub.add_parser("search", help="find papers on arXiv and Hugging Face")
    q.add_argument("query")
    q.add_argument("--limit", type=int, default=8)
    q.add_argument("--category", action="append", help="arXiv category, e.g. cs.SE (repeatable)")
    q.add_argument("--repo")

    q = rsub.add_parser("distill", help="turn papers into proposed skills")
    q.add_argument("query")
    q.add_argument("--limit", type=int, default=6)
    q.add_argument("--category", action="append")
    q.add_argument("--scripted", help="canned responses, for testing the plumbing")
    q.add_argument("--repo")

    q = rsub.add_parser("trial", help="A/B a proposed skill and adopt it only if it wins")
    q.add_argument("name")
    q.add_argument("--task")
    q.add_argument("--repo")
    q.add_argument("--trials", type=int, default=3)
    q.add_argument("--budget", type=int, default=12)
    q.add_argument("--scripted")

    q = rsub.add_parser("list", help="the skill library and each skill's verdict")
    q.add_argument("--repo")

    q = rsub.add_parser("show", help="print one skill, front matter and all")
    q.add_argument("name")
    q.add_argument("--repo")

    p.set_defaults(fn=cmd_research)

    p = sub.add_parser("graph", help="run an objective graph: nodes fulfilled only by their tests")
    p.add_argument("--file", help="a graph yaml (see objectives/demo-graph.yaml)")
    p.add_argument("--decompose", help="a goal to decompose into a graph via the planner model")
    p.add_argument("--out", help="where to write a decomposed graph")
    p.add_argument("--repo")
    p.add_argument("--run-id")
    p.add_argument("--scripted", help="a JSON list of canned model responses; runs with no harness")
    p.set_defaults(fn=cmd_graph)

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

    p = sub.add_parser("docs", help="fetch upstream docs for a library through Bright Data")
    p.add_argument("library")
    p.add_argument("--symbol")
    p.add_argument("--topic")
    p.add_argument("--repo")
    p.set_defaults(fn=cmd_docs)

    p = sub.add_parser("qodo-mcp", help="the QODO review MCP server, stdio")
    p.set_defaults(fn=cmd_qodo_mcp)

    p = sub.add_parser("qodo-fix", help="Qodo reviews the PR; its findings drive fix turns; the push waits at the gate")
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--rounds", type=int, default=2)
    p.add_argument("--repo")
    p.add_argument("--timeout", type=float, default=900, help="seconds to wait for each hosted review pass")
    p.set_defaults(fn=cmd_qodo_fix)

    p = sub.add_parser("dashboard", help="the same run, in a browser")
    p.add_argument("--bus")
    p.add_argument("--repo")
    p.add_argument("--run")
    p.add_argument("--host", default="127.0.0.1", help="loopback by default: this endpoint can approve a pull request")
    p.add_argument("--port", type=int, default=8788)
    p.set_defaults(fn=cmd_dashboard)

    p = sub.add_parser("export", help="write a session report: turns, files, commits, verdicts")
    p.add_argument("--repo")
    p.add_argument("--run")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("demo", help="seed the demo repository")
    p.add_argument("--dir")
    p.set_defaults(fn=cmd_demo)

    args = ap.parse_args(argv)
    if args.cmd is None:
        # bare `ratchet`: straight into the console on the latest run (or an empty
        # bus with the quick-start splash), the way `claude` starts a session
        return cmd_console(argparse.Namespace(repo=None, bus=None, run=None))
    return args.fn(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
