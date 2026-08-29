"""The search loop.

Not a linear agent loop with retries: a tree search over repo states, with the
verifier's score as the value function and the scheduler deciding where to spend the
next unit of compute.

    root = Node(commit, snapshot, score=verifier.run())
    while budget and nothing green:
        node    = scheduler.select(frontier)
        ctx     = context.assemble(repo_map, failure, diff_so_far, dead_ends)
        patches = generators.step(ctx, n=node.fanout)
        for patch in patches:
            child = sandbox.fork(node.image)      # warm cache inherited
            result = gauntlet.run(child)
            prune(child) if result.regressed else tree.add(child)
    winner = best(frontier) -> squash -> approval gate

Three things in here are easy to get wrong and worth calling out:

* **Forking from the node, not from HEAD.** A child inherits its parent's state, so
  the search explores a tree rather than repeatedly re-deriving one path.
* **Fan-out happens from the highest-scoring *shallow* node**, not the deepest one.
  Expanding the deepest node when you are stuck is how a search tunnels into a dead
  branch and calls it progress.
* **Pruned work is parked, never destroyed.** A dead end is still a node you can
  rewind to, and its one-line summary goes into its siblings' next prompt.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import context as ctx_mod
from .bus import Bus
from .gate import Gate
from .gitstate import GitState, commit_message
from .models import GauntletResult, TaskSpec
from .node import Node, Tree
from .receipts import ReceiptBook
from .scheduler import Budget, Scheduler
from .subagents import Candidate, Subagents
from .verifier.gauntlet import Gauntlet


@dataclass
class RunResult:
    winner: Node
    tree: Tree
    green: bool
    stopped_because: str
    budget: Budget


class SearchRun:
    def __init__(
        self,
        *,
        task: TaskSpec,
        repo: Path,
        provider,
        subagents: Subagents,
        run_id: str,
        scheduler: Scheduler | None = None,
        bus: Bus | None = None,
        docs=None,
        parallel: bool = True,
    ) -> None:
        self.task = task
        self.repo = Path(repo).resolve()
        self.provider = provider
        self.agents = subagents
        self.run_id = run_id
        self.scheduler = scheduler or Scheduler()
        self.bus = bus or Bus(self.repo / ".ratchet" / f"{run_id}.bus.jsonl")
        self.docs = docs
        self.parallel = parallel

        self.git = GitState.start(self.repo, run_id)
        self.tree = Tree(self.repo / ".ratchet" / f"{run_id}.tree.json")
        self.receipts = ReceiptBook(self.repo / ".ratchet" / f"{run_id}.receipts.jsonl")
        self.gate = Gate(self.repo, bus=self.bus)
        self.gauntlet = Gauntlet(task, repo_dir=".", test_sources=self._read_protected())
        self.step = 0
        self.repo_map = ""
        self.base_commit = self.git.head()

    # ------------------------------------------------------------------ setup --

    def _read_protected(self) -> dict[str, str]:
        """The graded tests' contents, for the special-casing rule only. This never
        goes near a prompt -- see the invariant in CLAUDE.md."""
        out: dict[str, str] = {}
        for pat in self.task.protected_paths:
            base = self.repo / pat.rstrip("/")
            if base.is_dir():
                for p in base.rglob("*"):
                    if p.is_file() and p.suffix in (".py", ".js", ".ts", ".go", ".rs"):
                        out[str(p.relative_to(self.repo))] = p.read_text(errors="replace")
            elif base.is_file():
                out[str(base.relative_to(self.repo))] = base.read_text(errors="replace")
        return out

    def _tree_listing(self) -> str:
        # shared with the objective graph; the held-out exclusion lives in one place
        return ctx_mod.tree_listing(self.repo, self.task.f2p_hidden)

    # ------------------------------------------------------------------- root --

    def establish_root(self) -> Node:
        base_image = self.provider.base_image()
        sb = self.provider.fork(base_image, label="root")
        self.bus.emit("sandbox.created", label="root", provider=self.provider.name)
        try:
            result = self.gauntlet.run(sb, "", base_commit=self.base_commit, apply_patch=False)
            image = sb.snapshot()
        finally:
            sb.destroy()
        root = Node.root(commit=self.base_commit, image=image, result=result)
        root.intent = "baseline"
        self.tree.add(root)
        self.receipts.record_result("root", result)
        self.bus.emit("node.added", **_node_event(root, result))
        return root

    # ------------------------------------------------------------------- loop --

    def run(self) -> RunResult:
        self.bus.emit(
            "run.started",
            run_id=self.run_id,
            task=self.task.task_id,
            provider=self.provider.name,
            snapshots=getattr(self.provider, "supports_snapshots", False),
            trunk=self.git.trunk,
            budget=self.scheduler.budget.to_dict(),
        )
        root = self.establish_root()
        if root.green:
            return self._finish(root, "the task was already green at the root")

        self.repo_map = self.agents.map_repo(self._tree_listing(), self.task.statement)
        self.bus.emit("repo.mapped", lines=len(self.repo_map.splitlines()))

        stopped = ""
        fruitless = 0  # consecutive expansions that added no child at all
        while True:
            if not self.scheduler.budget.ok():
                stopped = self.scheduler.budget.exhausted_reason()
                break
            green = next((n for n in self.tree.frontier() if n.green), None)
            if green:
                return self._finish(green, "verifier returned green")

            node = self.scheduler.select(self.tree)
            if node is None:
                stopped = "no live nodes left to expand"
                break

            fanout = 1
            hint = ""
            if self.scheduler.stalled:
                target = self.scheduler.stall_target(self.tree) or node
                node, fanout = target, 3
                self.scheduler.note_stall()
                self.bus.emit("stall", node=node.id, fanout=fanout, depth=node.depth)
                hint = (
                    "Previous attempts from this state have not improved the score. Do not vary the last "
                    "idea; choose a materially different hypothesis about the cause."
                )

            children = self.expand(node, fanout=fanout, hint=hint)
            # Empty candidates spend no node budget, so a generator that has gone
            # dry (an exhausted script, a model returning prose with no diff) used
            # to spin this loop flat out until the wall clock -- millions of bus
            # events, zero progress. Nothing usable five times in a row is an
            # answer: stop and say so.
            fruitless = 0 if children else fruitless + 1
            if fruitless >= 5:
                stopped = "the generator produced no usable candidate five expansions in a row"
                break
            self.scheduler.observe(self.tree)

        return self._finish(self.tree.best(), stopped or "budget exhausted")

    # ----------------------------------------------------------------- expand --

    def expand(self, node: Node, *, fanout: int = 1, hint: str = "") -> list[Node]:
        node.untried = False
        self.step += 1
        diff_so_far = self.git.diff(self.base_commit, node.commit) if node.commit != self.base_commit else ""
        docs_text = ""
        if self.docs is not None and node.last_failure:
            docs_text = self.docs.hint_for_failure(node.last_failure) or ""

        ctx = ctx_mod.assemble(
            task=self.task.statement,
            node=node,
            tree=self.tree,
            repo_map=self.repo_map,
            diff_so_far=diff_so_far,
            docs=docs_text,
            hint=hint,
        )
        self.bus.emit("expand", node=node.id, fanout=fanout, depth=node.depth, dead_ends=len(ctx.dead_ends))

        candidates = self.agents.generate(ctx.render(), n=fanout)
        for c in candidates:
            self.scheduler.budget.spend(usd=c.cost_usd)

        if self.parallel and len(candidates) > 1:
            with ThreadPoolExecutor(max_workers=min(4, len(candidates))) as pool:
                results = list(pool.map(lambda ic: self._evaluate(node, ic[0], ic[1]), enumerate(candidates)))
        else:
            results = [self._evaluate(node, i, c) for i, c in enumerate(candidates)]

        return [r for r in results if r is not None]

    def _evaluate(self, parent: Node, index: int, cand: Candidate) -> Node | None:
        """Fork, apply, grade, then commit or prune. One candidate, one sandbox."""
        label = f"{parent.id}-{index}-{int(time.time() * 1000) % 100000}"
        if cand.empty:
            self.bus.emit("candidate.empty", parent=parent.id, model=cand.model)
            return None

        sb = self.provider.fork(parent.image, label=label)
        self.bus.emit("sandbox.created", label=label, provider=self.provider.name, parent=parent.id)
        self.bus.emit("verify.started", label=label, parent=parent.id, intent=cand.intent, model=cand.model)
        try:
            result = self.gauntlet.run(sb, cand.patch, base_commit=self.base_commit)
            for name, st in result.stages.items():
                self.bus.emit("stage.result", label=label, stage=name, passed=st.passed,
                              detail=st.detail, skipped=st.skipped)

            if result.regressed:
                sha = self._commit_in(sb, parent, cand, result, park=True)
                self.git.park(f"{parent.id}-{index}", sha)
                node = Node.child_of(parent, commit=sha, image=parent.image, patch=cand.patch,
                                     intent=cand.intent, result=result, model=cand.model,
                                     tokens=cand.tokens, cost_usd=cand.cost_usd)
                self.tree.add(node)
                self.tree.prune(node, result.reason)
                self.receipts.record_result(node.id, result)
                self.bus.emit("node.pruned", **_node_event(node, result))
                return node

            image = sb.snapshot()
            sha = self._commit_in(sb, parent, cand, result)
            node = Node.child_of(parent, commit=sha, image=image, patch=cand.patch, intent=cand.intent,
                                 result=result, model=cand.model, tokens=cand.tokens, cost_usd=cand.cost_usd)
            self.tree.add(node)
            self.receipts.record_result(node.id, result)
            self.scheduler.budget.spend(nodes=1)
            self.bus.emit("node.added", **_node_event(node, result))
            return node
        finally:
            if not getattr(self.provider, "supports_snapshots", False):
                # worktree fallback: the state lives in the commit, so the checkout
                # can go. With real snapshots the sandbox is the state and is kept.
                sb.destroy()

    def _commit_in(self, sandbox, parent: Node, cand: Candidate, result: GauntletResult, *, park: bool = False) -> str:
        f2p = result.stages.get("f2p")
        p2p = result.stages.get("p2p")
        tests_line = f"f2p {f2p.detail if f2p else '-'} · p2p {p2p.detail if p2p else '-'}"
        verifier_line = ", ".join(
            filter(
                None,
                [
                    "types ok" if (result.stages.get("types") or _ok()).passed else "types FAIL",
                    "lint ok" if (result.stages.get("lint") or _ok()).passed else "lint FAIL",
                    "cheat-check clean" if not result.findings else f"cheat-check {result.findings[0].rule}",
                    None if not park else f"PRUNED: {result.reason}",
                ],
            )
        )
        msg = commit_message(
            node_id=parent.id,
            step=self.step,
            intent=cand.intent,
            before=parent.score,
            after=result.score,
            verifier_line=verifier_line,
            tests_line=tests_line,
        )
        return self.git.commit_node(msg, cwd=getattr(sandbox, "workdir", None))

    # ----------------------------------------------------------------- finish --

    def _finish(self, winner: Node, why: str) -> RunResult:
        # seal the chain so a truncated tail is detectable; idempotence guarded
        try:
            self.receipts.seal(f"winner={winner.id} green={winner.green} reason={why}")
        except RuntimeError:
            pass  # already sealed (finish can be reached twice on rewind flows)
        self.bus.emit(
            "run.done",
            winner=winner.id,
            green=winner.green,
            score=winner.score,
            reason=why,
            nodes=len(self.tree),
            budget=self.scheduler.budget.to_dict(),
        )
        self.git.return_home()
        return RunResult(winner=winner, tree=self.tree, green=winner.green,
                         stopped_because=why, budget=self.scheduler.budget)

    # -------------------------------------------------------------- shipping --

    def squashed(self, winner: Node) -> str:
        return self.git.squashed_diff(self.base_commit, winner.commit)

    def request_ship(self, winner: Node) -> tuple:
        """The last node in the state machine. Nothing leaves without a human."""
        diff = self.squashed(winner)
        path = self.tree.path_to(winner)
        req = self.gate.request(
            action="open_pull_request",
            summary=f"{self.task.task_id}: {winner.intent or 'fix'}",
            diff=diff,
            stats={
                "nodes_explored": len(self.tree),
                "path_length": len(path),
                "score": winner.score,
                "green": winner.green,
                "cost_usd": round(sum(n.cost_usd for n in self.tree), 4),
            },
        )
        return req, self.gate.wait(req)


def _ok():
    from .models import StageResult

    return StageResult(name="-", passed=True, skipped=True)


def _node_event(node: Node, result: GauntletResult) -> dict:
    return {
        "id": node.id,
        "parent": node.parent_id,
        "score": node.score,
        "green": node.green,
        "outcome": result.outcome.value,
        "intent": node.intent,
        "model": node.model,
        "depth": node.depth,
        "delta": result.delta,
        "findings": [f.rule for f in result.findings],
        "reason": result.reason,
    }
