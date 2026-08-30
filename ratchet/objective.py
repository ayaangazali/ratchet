"""The objective graph: a goal decomposed into nodes that tests can rule on.

An agent asked "is this sub-task done?" gives a subjective ruling. This module makes
the ruling objective: a goal is decomposed into a DAG of sub-objectives, and every
node carries the tests that must pass for it to count as fulfilled. Fulfilment is
set from `GauntletResult.green` and from nowhere else -- an ObjectiveNode has no
method an agent can call to mark itself done, for the same reason the loop has no
`done` tool (CLAUDE.md invariant 1).

Execution, per node, in dependency order:

  1. **linear attempts** -- up to `max_attempts` single candidates, each graded by
     the full gauntlet from the current graph state, rotating model providers so a
     retry is a different prior, not the same idea rephrased.
  2. **escalation** -- a node that exhausts its attempts is handed, whole, to the
     search engine (`loop.SearchRun`): tree search over restorable states, sandbox
     per branch, multi-provider fan-out on stall, the same verifier as the value
     function. The node is fulfilled only if the search reaches green.

The repo state *advances* as nodes fulfil: node B forks from the commit node A's
winning patch produced, so the graph composes patches the way a series of merged
PRs would. Protected paths are still reverted from the ORIGINAL base before every
grading -- tests never advance, only source does.

The graph is either authored (a YAML file, reviewable in a PR) or decomposed from a
goal by a planner model -- and a decomposed graph goes through the same validator,
which refuses any node whose tests it cannot find in the repo. A plan whose tests
do not exist is an opinion, not a plan.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bus import Bus
from .context import Context, tree_listing
from .gitstate import GitState, git
from .models import TaskSpec
from .node import Node
from .receipts import ReceiptBook
from .scheduler import Budget, Scheduler
from .subagents import Subagents
from .verifier.gauntlet import Gauntlet

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

# graph-level bus events; unknown kinds are ignored by the TUI, printed by the CLI
GRAPH_STARTED = "graph.started"
GRAPH_NODE_STARTED = "graph.node.started"
GRAPH_NODE_ATTEMPT = "graph.node.attempt"
GRAPH_NODE_FULFILLED = "graph.node.fulfilled"
GRAPH_NODE_ESCALATED = "graph.node.escalated"
GRAPH_NODE_FAILED = "graph.node.failed"
GRAPH_DONE = "graph.done"

PENDING, RUNNING, FULFILLED, FAILED, BLOCKED = "pending", "running", "fulfilled", "failed", "blocked"


@dataclass
class GraphSummary:
    """The graph's bookkeeping, not a verdict. `all_fulfilled` is deliberately not
    called `green`: green belongs to the gauntlet (invariant 1), and this field is
    derived purely from per-node statuses that themselves only flip on green
    GauntletResults. A second authority is not created here, and the name says so."""

    graph: str
    fulfilled: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    escalated: list[str] = field(default_factory=list)

    @property
    def all_fulfilled(self) -> bool:
        return not self.failed and not self.blocked

    def to_dict(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d["all_fulfilled"] = self.all_fulfilled
        return d


@dataclass
class ObjectiveNode:
    """One sub-objective and the tests that decide it. There is deliberately no
    `mark_done()` here: `fulfilled` flips only when a GauntletResult says green."""

    id: str
    goal: str
    f2p_visible: list[str] = field(default_factory=list)
    f2p_hidden: list[str] = field(default_factory=list)
    deps: list[str] = field(default_factory=list)
    max_attempts: int = 3
    status: str = PENDING
    escalated: bool = False
    attempts: int = 0
    commit: str = ""  # the state this node's winning patch produced
    sub_run_id: str = ""  # set when the node was escalated to a search run

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class ObjectiveGraph:
    graph_id: str
    repo_path: str
    statement: str
    nodes: dict[str, ObjectiveNode]
    order: list[str]  # topological
    test_cmd: str = "python -m pytest -rA"
    framework: str = "pytest"
    p2p: list[str] = field(default_factory=list)
    protected_paths: list[str] = field(default_factory=lambda: ["tests/"])
    allowed_paths: list[str] = field(default_factory=list)
    timeout_s: int = 300

    def task_for(self, node: ObjectiveNode) -> TaskSpec:
        """The node's objective contract, in the shape the gauntlet already grades."""
        return TaskSpec(
            task_id=f"{self.graph_id}:{node.id}",
            repo_path=self.repo_path,
            statement=f"{self.statement}\n\nThis step's objective: {node.goal}",
            test_cmd=self.test_cmd,
            framework=self.framework,
            f2p_visible=list(node.f2p_visible),
            f2p_hidden=list(node.f2p_hidden),
            p2p=list(self.p2p),
            protected_paths=list(self.protected_paths),
            allowed_paths=list(self.allowed_paths),
            timeout_s=self.timeout_s,
        )

    def all_hidden(self) -> list[str]:
        return [t for n in self.nodes.values() for t in n.f2p_hidden]

    def ready(self) -> list[ObjectiveNode]:
        done = {i for i, n in self.nodes.items() if n.status == FULFILLED}
        return [
            self.nodes[i]
            for i in self.order
            if self.nodes[i].status == PENDING and all(d in done for d in self.nodes[i].deps)
        ]

    def summary(self) -> GraphSummary:
        return GraphSummary(
            graph=self.graph_id,
            fulfilled=[i for i in self.order if self.nodes[i].status == FULFILLED],
            failed=[i for i in self.order if self.nodes[i].status == FAILED],
            blocked=[i for i in self.order if self.nodes[i].status == BLOCKED],
            escalated=[i for i in self.order if self.nodes[i].escalated],
        )


# --------------------------------------------------------------------------- #
# loading and validation
# --------------------------------------------------------------------------- #


def _topo(nodes: dict[str, ObjectiveNode]) -> list[str]:
    """Kahn's algorithm. A cycle or an unknown dependency is a config error, and it
    is caught here rather than discovered as a hang mid-run."""
    for n in nodes.values():
        for d in n.deps:
            if d not in nodes:
                raise ValueError(f"node {n.id!r} depends on unknown node {d!r}")
    seen: list[str] = []
    while len(seen) < len(nodes):
        progressed = False
        for i in sorted(nodes):
            if i not in seen and all(d in seen for d in nodes[i].deps):
                seen.append(i)
                progressed = True
        if not progressed:
            raise ValueError(f"dependency cycle among: {sorted(set(nodes) - set(seen))}")
    return seen


def _validate_tests_exist(graph: ObjectiveGraph, repo: Path) -> None:
    """Every node's tests must be real files containing the named test function.
    This is the line between an objective graph and a wish list: a node whose tests
    cannot be found can never be objectively fulfilled, so it is refused up front."""
    problems: list[str] = []
    for node in graph.nodes.values():
        if not node.f2p_visible and not node.f2p_hidden:
            problems.append(f"node {node.id!r} has no fail-to-pass tests; nothing can ever fulfil it")
            continue
        for test_id in [*node.f2p_visible, *node.f2p_hidden]:
            path, _, name = test_id.partition("::")
            f = repo / path
            if not f.is_file():
                problems.append(f"node {node.id!r}: test file {path} does not exist")
            elif name:
                fn = name.split("::")[-1]
                text = f.read_text(errors="replace")
                # for python files, demand an actual definition -- a mention in a
                # comment or a longer test's name is not a test (found by review)
                defined = (
                    re.search(rf"def\s+{re.escape(fn)}\s*\(", text)
                    if f.suffix == ".py"
                    else fn in text
                )
                if not defined:
                    problems.append(f"node {node.id!r}: {name} not defined in {path}")
    if problems:
        raise ValueError("objective graph failed validation:\n  - " + "\n  - ".join(problems))


def load_graph(source: str | Path, repo: Path | None = None) -> ObjectiveGraph:
    """Parse a graph from YAML text or a file path, and validate it."""
    text = Path(source).read_text() if isinstance(source, Path) or "\n" not in str(source) else str(source)
    if yaml is None:  # pragma: no cover
        raise RuntimeError("pip install pyyaml")
    data = yaml.safe_load(text)
    raw_nodes = data.get("nodes") or []
    ids = [d["id"] for d in raw_nodes]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        # a dict comprehension would silently keep only the last entry, and the
        # overwritten objective's tests would vanish before validation ever ran
        raise ValueError(f"duplicate node ids: {dupes}")
    nodes = {
        d["id"]: ObjectiveNode(
            id=d["id"],
            goal=d["goal"],
            f2p_visible=list(d.get("f2p_visible") or []),
            f2p_hidden=list(d.get("f2p_hidden") or []),
            deps=list(d.get("deps") or []),
            max_attempts=int(d.get("max_attempts", 3)),
        )
        for d in raw_nodes
    }
    if not nodes:
        raise ValueError("graph has no nodes")
    graph = ObjectiveGraph(
        graph_id=data.get("graph_id", "graph"),
        repo_path=data.get("repo_path", "."),
        statement=data.get("statement", ""),
        nodes=nodes,
        order=_topo(nodes),
        test_cmd=data.get("test_cmd", "python -m pytest -rA"),
        framework=data.get("framework", "pytest"),
        p2p=list(data.get("p2p") or []),
        protected_paths=list(data.get("protected_paths") or ["tests/"]),
        allowed_paths=list(data.get("allowed_paths") or []),
        timeout_s=int(data.get("timeout_s", 300)),
    )
    _validate_tests_exist(graph, Path(repo or graph.repo_path))
    return graph


# --------------------------------------------------------------------------- #
# decomposition: goal text -> objective graph, through the same validator
# --------------------------------------------------------------------------- #

_DECOMPOSE_PROMPT = """You are decomposing a coding objective into an objective graph.

Objective:
{goal}

Files in the repository:
{listing}

Reply with ONLY a yaml document (in a ```yaml fence) with this shape:

graph_id: <slug>
statement: <one paragraph restating the objective>
test_cmd: <the exact test command>
p2p: [<test ids that already pass and must stay green>]
protected_paths: [tests/]
nodes:
  - id: <slug>
    goal: <one sentence>
    f2p_visible: [<failing test ids this step must turn green>]
    deps: [<ids of steps this depends on>]

Rules: every node MUST list at least one f2p test that exists in the repository
today and fails today. Do not invent test names. Do not add nodes without tests --
a step whose completion cannot be checked is not a step, it is a hope."""


def decompose(
    goal: str, repo: Path, subagents: Subagents, *, model_role: str = "cartographer"
) -> tuple[ObjectiveGraph, str]:
    """Ask a planner model for the graph, then hold it to the validator.

    The model proposes; the validator disposes. A graph naming tests that do not
    exist is rejected with the exact reasons -- nothing unvalidated ever reaches
    execution. Returns (graph, yaml_text) so the caller can write the plan to a
    file for human review; the CLI refuses to execute a decomposed graph in the
    same invocation on purpose.
    """
    listing = tree_listing(repo, [])
    prompt = _DECOMPOSE_PROMPT.format(goal=goal, listing=listing[:6000])
    text, _tok, _cost = subagents.backend.complete(
        prompt, model=subagents.roles.cartographer, role=model_role, max_tokens=2000
    )
    block = text.split("```yaml", 1)[-1].split("```", 1)[0] if "```" in text else text
    graph = load_graph(block, repo)
    return graph, block


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #


class GraphRun:
    """Walk the graph in dependency order; fulfil nodes only through the gauntlet."""

    def __init__(
        self,
        *,
        graph: ObjectiveGraph,
        repo: Path,
        provider,
        subagents: Subagents,
        run_id: str,
        bus: Bus | None = None,
        escalation_budget: Budget | None = None,
        parallel: bool = True,
        docs=None,
    ) -> None:
        self.graph = graph
        self.docs = docs
        self.repo = Path(repo).resolve()
        self.provider = provider
        self.agents = subagents
        self.run_id = run_id
        self.bus = bus or Bus(self.repo / ".ratchet" / f"{run_id}.bus.jsonl")
        self.escalation_budget = escalation_budget or Budget()
        self.parallel = parallel

        self.git = GitState.start(self.repo, run_id)
        self.receipts = ReceiptBook(self.repo / ".ratchet" / f"{run_id}.receipts.jsonl")
        self.state_path = self.repo / ".ratchet" / f"{run_id}.graph.json"
        self.base_commit = self.git.head()
        self.state_commit = self.base_commit
        self.repo_map = ""

    # ---------------------------------------------------------------- state --

    def _save(self) -> None:
        payload = {
            "graph_id": self.graph.graph_id,
            "run_id": self.run_id,
            "base": self.base_commit,
            "state": self.state_commit,
            "order": self.graph.order,
            "nodes": {i: n.to_dict() for i, n in self.graph.nodes.items()},
            "saved_at": time.time(),
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self.state_path)

    # ------------------------------------------------------------------ run --

    def run(self) -> GraphSummary:
        self.bus.emit(GRAPH_STARTED, run_id=self.run_id, graph=self.graph.graph_id,
                      nodes=self.graph.order, provider=self.provider.name)
        # one cheap map for the whole graph; held-out test files for EVERY node are
        # excluded from the listing, not just the current node's
        self.repo_map = self.agents.map_repo(
            tree_listing(self.repo, self.graph.all_hidden()), self.graph.statement
        )

        while True:
            ready = self.graph.ready()
            if not ready:
                break
            for node in ready:
                self._run_node(node)
                self._save()

        # anything still pending has an unfulfilled dependency
        for node in self.graph.nodes.values():
            if node.status == PENDING:
                node.status = BLOCKED
        self._save()
        try:
            self.receipts.seal(f"graph={self.graph.graph_id} state={self.state_commit}")
        except RuntimeError:
            pass
        summary = self.graph.summary()
        self.bus.emit(GRAPH_DONE, run_id=self.run_id, **summary.to_dict())
        return summary

    # ----------------------------------------------------------------- node --

    def _run_node(self, node: ObjectiveNode) -> None:
        node.status = RUNNING
        task = self.graph.task_for(node)
        gauntlet = Gauntlet(task, repo_dir=".", test_sources=self._protected_sources())
        self.bus.emit(GRAPH_NODE_STARTED, node=node.id, goal=node.goal, deps=node.deps,
                      base=self.state_commit[:10])

        dead_ends: list[str] = []
        failure = ""
        for i in range(node.max_attempts):
            node.attempts = i + 1
            label = f"{node.id}-a{i}"
            docs_text = ""
            if self.docs is not None and failure:
                # a red verdict that looks like an import/attr/kwarg drift gets the
                # current upstream docs for the pinned version attached, via Bright Data
                docs_text = self.docs.hint_for_failure(failure) or ""
            ctx = Context(
                task=task.statement,
                repo_map=self.repo_map,
                failure=failure,  # gauntlet output: held-out details already withheld
                diff_so_far=self.git.diff(self.base_commit, self.state_commit)
                if self.state_commit != self.base_commit else "",
                dead_ends=dead_ends,
                docs=docs_text,
                # sub-agents share no history with this process; the branch label is
                # restated in full so the child's work is attributable to this node
                hint=f"You are working branch {label} of objective node {node.id!r}.",
            )
            cands = self.agents.generate(ctx.render(), n=1, start=i)
            cand = cands[0]
            self.bus.emit(GRAPH_NODE_ATTEMPT, node=node.id, attempt=i + 1, model=cand.model,
                          intent=cand.intent, empty=cand.empty)
            if cand.empty:
                # a reply with no patch -- including "the task is complete" prose --
                # consumes an attempt and changes nothing. Claims are not moves.
                dead_ends.append(f"{cand.intent or 'no patch produced'} -> produced no diff")
                continue

            sb = self.provider.fork(self.state_commit, label=label)
            try:
                res = gauntlet.run(sb, cand.patch, base_commit=self.state_commit)
                self.receipts.record_result(f"{node.id}-a{i}", res)
                if res.green:
                    # the ONLY path to fulfilled: the verifier said green.
                    # A worktree sandbox has a local checkout we can commit; a
                    # harness sandbox's workdir is remote, so its restorable state
                    # is the snapshot reference instead (found by review).
                    wd = getattr(sb, "workdir", None)
                    if wd is not None and Path(wd).exists():
                        sha = self.git.commit_node(
                            f"[graph {node.id}] attempt {i + 1} green · {cand.intent}",
                            cwd=wd,
                        )
                    else:
                        sha = sb.snapshot()
                    self._advance(node, sha)
                    return
            finally:
                sb.destroy()

            failure = res.last_failure or res.reason
            dead_ends.append(Node.child_of(
                Node.root(commit=self.state_commit, image=self.state_commit),
                commit=self.state_commit, image="", patch=cand.patch,
                intent=cand.intent, result=res,
            ).one_line())

        # attempts exhausted -> the engine takes the node whole
        self._escalate(node, task)

    def _escalate(self, node: ObjectiveNode, task: TaskSpec) -> None:
        """Hand the node to the tree search: restorable states, per-branch sandboxes,
        multi-provider fan-out on stall, the same gauntlet deciding. The node is
        fulfilled only if the search itself reaches green."""
        from .loop import SearchRun  # late import; loop pulls in the world

        node.escalated = True
        sub_run_id = f"{self.run_id}-{node.id}"
        node.sub_run_id = sub_run_id
        self.bus.emit(GRAPH_NODE_ESCALATED, node=node.id, sub_run=sub_run_id,
                      attempts=node.attempts)

        # the search forks from the graph's CURRENT state, not the original base
        git("checkout", "-B", self.git.trunk, self.state_commit, cwd=self.repo)
        run = SearchRun(
            task=task,
            repo=self.repo,
            provider=self.provider,
            subagents=self.agents,
            run_id=sub_run_id,
            scheduler=Scheduler(Budget(
                max_nodes=self.escalation_budget.max_nodes,
                max_seconds=self.escalation_budget.max_seconds,
                max_usd=self.escalation_budget.max_usd,
            )),
            bus=self.bus,
            parallel=self.parallel,
        )
        result = run.run()
        if result.green:
            self._advance(node, result.winner.commit)
        else:
            node.status = FAILED
            self.bus.emit(GRAPH_NODE_FAILED, node=node.id, sub_run=sub_run_id,
                          reason=result.stopped_because, best_score=result.winner.score)

    def _advance(self, node: ObjectiveNode, sha: str) -> None:
        node.status = FULFILLED
        node.commit = sha
        self.state_commit = sha
        git("checkout", "-B", self.git.trunk, sha, cwd=self.repo)
        self.bus.emit(GRAPH_NODE_FULFILLED, node=node.id, commit=sha[:10],
                      escalated=node.escalated, attempts=node.attempts)

    # -------------------------------------------------------------- helpers --

    def _protected_sources(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for pat in self.graph.protected_paths:
            base = self.repo / pat.rstrip("/")
            if base.is_dir():
                for p in base.rglob("*"):
                    if p.is_file() and p.suffix in (".py", ".js", ".ts", ".go", ".rs"):
                        out[str(p.relative_to(self.repo))] = p.read_text(errors="replace")
            elif base.is_file():
                out[str(base.relative_to(self.repo))] = base.read_text(errors="replace")
        return out

    def squashed(self) -> str:
        """The one diff the whole graph produced, for the gate."""
        return self.git.squashed_diff(self.base_commit, self.state_commit)
