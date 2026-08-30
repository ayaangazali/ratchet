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
import time
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
    """Follow a run as a stream. Any width, scrollable, pipeable to a file."""
    import time as _t

    from .bus import Bus
    from .console import StreamConsole

    repo = Path(args.repo or ".").resolve()
    bus_path = Path(args.bus) if args.bus else _latest(repo, "bus.jsonl", args.run)
    if not bus_path:
        print("no run yet. `ratchet pipeline` shows the whole shape of one, "
              "or `ratchet run` starts a real search.", file=sys.stderr)
        return 1
    view = StreamConsole()
    bus = Bus(bus_path)
    for ev in bus.read_all():
        view.handle(ev)
    if not args.follow:
        return 0
    try:
        while True:                       # tail it, the way you would a log
            for ev in bus.tail():
                view.handle(ev)
            _t.sleep(0.2)
    except KeyboardInterrupt:
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



def cmd_build(args) -> int:
    """A goal, a repo or an issue in; a reviewed pull request out."""
    import threading
    import uuid as _uuid

    from .build import BuildRun, Pace, Target
    from .buildview import BuildView
    from .bus import Bus
    from .qodo_mcp import QodoMCP

    repo = Path(args.repo or ".").resolve()
    raw = args.target
    force = ""
    if raw.lower() == "research":
        # `ratchet build research "<paper url>"` reads better out loud than a flag
        if not args.rest:
            print("usage: ratchet build research <paper url>", file=sys.stderr)
            return 2
        raw, force = args.rest[0], "research"
    target = Target.parse(raw, force=force)
    run_id = args.run_id or f"build-{_uuid.uuid4().hex[:6]}"
    bus_path = repo / ".ratchet" / f"{run_id}.bus.jsonl"
    bus_path.parent.mkdir(parents=True, exist_ok=True)

    view = BuildView(animate=not args.no_animate, label_demo=args.label_demo)
    reader, done = Bus(bus_path), threading.Event()

    def follow() -> None:
        # the view follows the same file the dashboard and a replay would
        while not done.is_set():
            for ev in reader.tail():
                view.handle(ev)
            view.pump()
            time.sleep(0.05)
        for ev in reader.tail():
            view.handle(ev)

    watcher = threading.Thread(target=follow, daemon=True)
    watcher.start()
    result = BuildRun(target, repo, Bus(bus_path), run_id=run_id,
                      pace=Pace(beat=args.pace), qodo=QodoMCP("ayaangazali/ratchet"),
                      demo=not args.live).run()
    done.set()
    watcher.join(timeout=3)
    print(f"\n  bus: {bus_path}")
    return 0 if result.get("green") else 2


def cmd_live(args) -> int:
    """The real pipeline: real services, and it prints every call it made."""
    import uuid as _uuid

    from . import shark
    from .buildview import BuildView
    from .bus import Bus
    from .live import LiveRun
    from .qodo_mcp import QodoUnavailable

    repo = Path(args.repo or ".").resolve()
    run_id = args.run_id or f"live-{_uuid.uuid4().hex[:6]}"
    bus_path = repo / ".ratchet" / f"{run_id}.bus.jsonl"
    bus_path.parent.mkdir(parents=True, exist_ok=True)
    view, reader = BuildView(animate=not args.no_animate), Bus(bus_path)
    run = LiveRun(repo, Bus(bus_path), run_id=run_id, repo_slug=args.repo_slug,
                  goal=args.goal or "")

    def drain() -> None:
        for ev in reader.tail():
            view.handle(ev)

    view.out.print(shark.banner(args.goal or args.repo_slug))
    checks = run.preflight()
    drain()
    missing = [k for k, v in checks.items() if not v["ok"] and k != "gateway_only"]
    if missing and not args.force:
        view.line(f"not reachable: {', '.join(missing)} — nothing here is faked, so the run stops",
                  "#e5675c")
        return 1

    if args.goal:
        try:
            run.ask(args.goal, role="plan", max_tokens=args.max_tokens)
        except Exception as e:
            drain()
            view.line(f"model call failed: {e}", "#e5675c")
            return 1
        drain()

    review: dict | None = None
    if args.pr:
        try:
            review = run.review(args.pr)
        except QodoUnavailable as e:
            # A reviewer that never answered is not a reviewer that approved. The run
            # ends red here rather than printing a green summary over a silent gate.
            view.line(str(e)[:160], "#e5675c")
            # No `green` here: this path never ran the gauntlet, and `green` is set in
            # exactly one place. What a live run knows is what the reviewer said.
            run.finish(blocking=1, pr=args.pr, nodes=0, findings=0, reason=str(e)[:160])
            drain()
            print(f"\n  bus: {bus_path}")
            return 1
        drain()

    blocking = int(review["blocking"]) if review else 0
    findings = len(review["findings"]) if review else 0
    run.finish(blocking=blocking, pr=args.pr or "", nodes=0, findings=findings,
               reason=(f"{blocking} blocking finding(s) — the reviewer said no"
                       if blocking else
                       "live run complete — every call above actually happened"))
    drain()
    print(f"\n  bus: {bus_path}")
    return 1 if blocking else 0


def cmd_pipeline(args) -> int:
    """The whole shape of the product: harness, verifier, gate, review, merge."""
    import uuid as _uuid

    from .bus import Bus
    from .console import StreamConsole
    from .pipeline import Pace, PipelineRun

    repo = Path(args.repo or ".").resolve()
    run_id = args.run_id or f"pipeline-{_uuid.uuid4().hex[:6]}"
    bus_path = repo / ".ratchet" / f"{run_id}.bus.jsonl"
    bus_path.parent.mkdir(parents=True, exist_ok=True)
    bus = Bus(bus_path)
    view = StreamConsole()

    if args.demo:
        view.out.print()
        view.note("demo mode — scripted stages, real event stream, no live services", "#e0a44a")

    # render as it happens: the pipeline emits, the console follows the same file
    import threading

    done = threading.Event()

    def follow() -> None:
        while not done.is_set():
            for ev in bus.tail():
                view.handle(ev)
            time.sleep(0.05)
        for ev in bus.tail():
            view.handle(ev)

    watcher = threading.Thread(target=follow, daemon=True)
    watcher.start()
    result = PipelineRun(repo, Bus(bus_path), run_id=run_id,
                         pace=Pace(beat=args.pace), demo=args.demo).run()
    done.set()
    watcher.join(timeout=3)
    print()
    print(f"  bus: {bus_path}")
    return 0 if result.get("green") else 2


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

    p = sub.add_parser("console", help="follow a run as a stream")
    p.add_argument("--bus")
    p.add_argument("--repo")
    p.add_argument("--run")
    p.add_argument("--follow", "-f", action="store_true", help="keep following as the run continues")
    p.set_defaults(fn=cmd_console)

    p = sub.add_parser("docs", help="fetch upstream docs for a library through Bright Data")
    p.add_argument("library")
    p.add_argument("--symbol")
    p.add_argument("--topic")
    p.add_argument("--repo")
    p.set_defaults(fn=cmd_docs)

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

    p = sub.add_parser("build", help="a goal, repo or issue in; a reviewed pull request out")
    p.add_argument("target", help="a prompt, a repo url, an issue url, a paper url, or `research`")
    p.add_argument("rest", nargs="*", help="the paper url, when the target is `research`")
    p.add_argument("--repo", help="where to work (default: here)")
    p.add_argument("--run-id")
    p.add_argument("--pace", type=float, default=0.35, help="seconds per beat; 0 runs it instantly")
    p.add_argument("--no-animate", action="store_true", help="plain lines, for logs and CI")
    p.add_argument("--live", action="store_true", help="use real services instead of the demo script")
    p.add_argument("--label-demo", action="store_true",
                   help="say on screen which stages are scripted (the stream always records it)")
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("live", help="the real pipeline: real services, every API call printed")
    p.add_argument("--goal", help="a prompt to send through the gateway")
    p.add_argument("--pr", help="a pull request for Qodo to report on")
    p.add_argument("--repo-slug", default="ayaangazali/ratchet")
    p.add_argument("--repo")
    p.add_argument("--run-id")
    p.add_argument("--max-tokens", type=int, default=400)
    p.add_argument("--no-animate", action="store_true")
    p.add_argument("--force", action="store_true", help="continue even if a service is unreachable")
    p.set_defaults(fn=cmd_live)

    p = sub.add_parser("pipeline", help="the whole shape of a run: harness, verifier, gate, review, merge")
    p.add_argument("--repo")
    p.add_argument("--run-id")
    p.add_argument("--pace", type=float, default=0.45, help="seconds per beat; 0 runs it instantly")
    p.add_argument("--demo", action="store_true", default=True)
    p.add_argument("--live", dest="demo", action="store_false", help="drive real services instead")
    p.set_defaults(fn=cmd_pipeline)

    p = sub.add_parser("demo", help="seed the demo repository")
    p.add_argument("--dir")
    p.set_defaults(fn=cmd_demo)

    args = ap.parse_args(argv)
    if args.cmd is None:
        # bare `ratchet`: follow the newest run. With none, show what one looks like
        # rather than an empty screen -- the first thing a new user sees should be
        # the product working, not a blank box.
        import argparse as _ap

        repo = Path(".").resolve()
        if _latest(repo, "bus.jsonl", None) is None:
            return cmd_pipeline(_ap.Namespace(repo=None, run_id=None, pace=0.45, demo=True))
        return cmd_console(_ap.Namespace(repo=None, bus=None, run=None, follow=True))
    if False:
        pass
    return args.fn(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
