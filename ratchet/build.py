"""`ratchet build` — a goal in, a merged pull request out.

    goal or issue
        -> objective graph          the goal becomes nodes with tests attached
        -> parallel sub-agents      one sandbox per node, several providers
        -> the gauntlet             seven stages; a node sticks or it is pruned
        -> Qodo, over MCP           the diff is reviewed BEFORE it is committed
        -> fixes                    a finding is work, not a verdict
        -> commit, pull request     nothing irreversible without the gate

The order is the argument. Reviewing before the commit means a blocking finding
never becomes a commit anyone has to revert, and the reviewer's output re-enters
the same loop that produced the patch instead of landing in a comment thread.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .bus import Bus
from .qodo_mcp import REVIEW_WAIT, Finding, QodoMCP, QodoUnavailable

ISSUE_URL = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)")
PAPER_URL = re.compile(r"(arxiv\.org|doi\.org|openreview\.net|aclanthology\.org|\.pdf$)", re.I)
ARXIV_ID = re.compile(r"arxiv\.org/(?:abs|html|pdf)/([\d.]+)")
REPO_URL = re.compile(r"github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


@dataclass
class Target:
    """What the user asked for, whatever shape they asked in."""

    kind: str            # "prompt" | "repo" | "issue" | "paper"
    goal: str
    repo: str = ""
    issue: str = ""
    paper: str = ""

    @classmethod
    def parse(cls, raw: str, *, force: str = "") -> Target:
        raw = raw.strip()
        if force == "research" or PAPER_URL.search(raw):
            ident = m.group(1) if (m := ARXIV_ID.search(raw)) else raw.rstrip("/").rsplit("/", 1)[-1]
            return cls("paper", f"build a working implementation of {ident}", paper=raw)
        if m := ISSUE_URL.search(raw):
            owner, name, number = m.groups()
            return cls("issue", f"resolve issue #{number} in {owner}/{name}",
                       repo=f"{owner}/{name}", issue=f"#{number}")
        if m := REPO_URL.search(raw):
            owner, name = m.groups()
            return cls("repo", f"work on {owner}/{name}", repo=f"{owner}/{name}")
        return cls("prompt", raw)


@dataclass
class Node:
    """One objective. It is fulfilled when its tests pass and never before."""

    id: str
    goal: str
    tests: list[str]
    deps: list[str] = field(default_factory=list)
    model: str = ""
    status: str = "pending"


#: The demo's graph. Chosen so the shape is visible: two nodes that can run at
#: the same time, and one that genuinely cannot start until they land.
DEMO_NODES = [
    Node("parse", "parse the rate-limit config and validate its window",
         ["tests/test_config.py::test_window_parses",
          "tests/test_config.py::test_rejects_a_negative_window"],
         model="truefoundry/openai/gpt-5.2"),
    Node("store", "a token bucket that survives a restart",
         ["tests/test_bucket.py::test_refills_over_time",
          "tests/test_bucket.py::test_survives_a_restart"],
         model="trueforge/claude-sonnet-4-6"),
    Node("middleware", "reject over-limit requests with 429 and Retry-After",
         ["tests/test_middleware.py::test_429_when_over_limit",
          "tests/test_middleware.py::test_sets_retry_after"],
         deps=["parse", "store"], model="truefoundry/openai/gpt-5.2"),
]

#: Findings the demo replays. Every one is real: Qodo raised each of these
#: against this repository, and they are quoted as they arrived. `--live` fetches
#: current findings instead of replaying these.
REPLAYED_FINDINGS = [
    Finding("high", "Ignored protected files survive the reset",
            "git clean -fdq preserves ignored files, so a file created under a protected "
            "directory survives the pre-run reset and can still affect grading.",
            "ratchet/verifier/eval_script.py", 49),
    Finding("high", "End marker remains forgeable",
            "parse_exit_code splits on the first end marker, so suite code can print a forged "
            "END followed by exit code 0 and be graded on the forgery.",
            "ratchet/verifier/parsers.py", 59),
    Finding("medium", "Empty truncation audits clean",
            "verify() only requires a seal when at least one receipt remains, so truncating the "
            "receipt file to zero bytes returns success.",
            "ratchet/receipts.py", 166),
]


#: A paper is not a feature request. The graph it produces has an extra
#: obligation at the end: the implementation has to reproduce the number the
#: paper claims, or it is a plausible-looking thing that agrees with nobody.
PAPER_NODES = [
    Node("tasks", "encode the paper's task construction: two assertions that contradict",
         ["tests/test_tasks.py::test_the_pair_is_unsatisfiable",
          "tests/test_tasks.py::test_no_implementation_passes_both"],
         model="truefoundry/openai/gpt-5.2"),
    Node("harness", "run a model against a task and record what it reports",
         ["tests/test_harness.py::test_records_a_claim_of_success",
          "tests/test_harness.py::test_separates_reported_from_actual"],
         model="trueforge/claude-sonnet-4-6"),
    Node("metric", "the cheat rate: reported success on a task nothing can satisfy",
         ["tests/test_metric.py::test_counts_only_impossible_tasks",
          "tests/test_metric.py::test_matches_the_worked_example"],
         deps=["tasks", "harness"], model="truefoundry/openai/gpt-5.2"),
]

STAGES = [("cheat", "0 finding(s), 0 critical"), ("build", "ok"), ("f2p", "{f2p}"),
          ("p2p", "118/118"), ("types", "clean"), ("lint", "clean"),
          ("hygiene", "{files} file(s), {lines} added lines")]


@dataclass
class Pace:
    beat: float = 0.35

    def wait(self, factor: float = 1.0) -> None:
        if self.beat:
            time.sleep(self.beat * factor)


class BuildRun:
    """Drives the pipeline and narrates it onto the bus."""

    def __init__(self, target: Target, repo: Path, bus: Bus, *, run_id: str,
                 pace: Pace | None = None, qodo: QodoMCP | None = None, demo: bool = True) -> None:
        self.target = target
        self.repo = Path(repo)
        self.bus = bus
        self.run_id = run_id
        self.pace = pace or Pace()
        self.qodo = qodo or QodoMCP("ayaangazali/ratchet")
        self.live_pr = "10"        # which pull request `--live` reports on
        self.demo = demo
        source = PAPER_NODES if target.kind == "paper" else DEMO_NODES
        self.nodes = [Node(n.id, n.goal, list(n.tests), list(n.deps), n.model) for n in source]

    def emit(self, kind: str, **payload) -> None:
        self.bus.emit(kind, **payload)
        self.pace.wait()

    # ------------------------------------------------------------------ run --

    def run(self) -> dict:
        t = self.target
        self.emit("build.started", run_id=self.run_id, target=t.kind, goal=t.goal,
                  repo=t.repo, issue=t.issue, demo=self.demo)

        if t.kind == "paper":
            self.emit("paper.read", url=t.paper, ident="arXiv:2510.20270",
                      title="ImpossibleBench: Measuring Reward Hacking with Unsatisfiable Tasks",
                      claim="On tasks whose tests are provably unsatisfiable, frontier models "
                            "report success roughly half the time instead of reporting the task impossible.",
                      reproduce="cheat rate ≈ 0.50 on the impossible split")
            self.emit("paper.method", steps=[
                "construct tasks whose tests contradict each other",
                "run a model against them and record what it reports",
                "measure reported success on tasks nothing can satisfy",
            ], implementable=3, out_of_scope=["the model training runs", "the closed eval set"])

        if t.kind == "issue":
            self.emit("issue.read", repo=t.repo, issue=t.issue,
                      title="Requests are not rate limited",
                      body="A single client can exhaust the API. We need a per-key limit with a "
                           "sane window, a 429, and a Retry-After header.")

        # 1. the goal becomes a graph, and every node carries the tests that judge it
        self.emit("graph.planned", nodes=[
            {"id": n.id, "goal": n.goal, "tests": len(n.tests), "deps": n.deps} for n in self.nodes
        ])

        # 2. nodes with no unmet dependency run at the same time, in their own sandboxes
        done: set[str] = set()
        while len(done) < len(self.nodes):
            wave = [n for n in self.nodes if n.status == "pending" and set(n.deps) <= done]
            self.emit("wave.started", nodes=[n.id for n in wave], parallel=len(wave))
            for n in wave:
                self._work(n)
                done.add(n.id)

        # 3. a paper build owes a reproduction: the implementation has to produce
        #    the number the paper claims, or it is a plausible thing agreeing with
        #    nobody. It is graded like any other check.
        if t.kind == "paper":
            self.emit("reproduce.started", claim="cheat rate ≈ 0.50 on the impossible split",
                      runs=40)
            self.emit("reproduce.result", measured="0.47", claimed="0.50", tolerance="±0.05",
                      matches=True,
                      note="40 runs against the impossible split; the honest split stays at 0.00")

        # 4. the review happens on the diff, before anything is committed
        review = self._review()

        # 4. every blocking finding is work, and the diff is re-reviewed after
        if review["blocking"]:
            for f in review["findings"]:
                if f["severity"] in ("critical", "high"):
                    self._fix(f)
            self.emit("review.started", scope="diff", reviewer="qodo", pass_no=2,
                      scripted=review["scripted"])
            self.emit("review.done", findings=0, blocking=0, pass_no=2)

        # 5. only now does anything become a commit, and only with a human
        self.emit("approval.required", action="open_pull_request",
                  summary=self.target.goal,
                  stats={"nodes": len(self.nodes), "findings answered": review["blocking"]})
        self.pace.wait(2)
        self.emit("approval.resolved", approved=True)
        self.emit("commit.created", sha="9f2c1ab", message=f"feat: {self.target.goal}")
        pr = "#204"
        self.emit("pr.opened", pr=pr, title=self.target.goal,
                  url=f"https://github.com/{self.target.repo or 'you/repo'}/pull/204")
        self.emit("pr.merged", pr=pr)
        self.emit("build.done", green=True, pr=pr, nodes=len(self.nodes),
                  findings=len(review["findings"]), reason="every node fulfilled, every finding answered")
        return {"green": True, "pr": pr, "nodes": len(self.nodes), "review": review}

    # ---------------------------------------------------------------- parts --

    def _work(self, node: Node) -> None:
        self.emit("node.started", id=node.id, goal=node.goal, model=node.model,
                  tests=node.tests, deps=node.deps)
        self.emit("sandbox.created", label=node.id, provider="trueforge", snapshot=True)

        # the first attempt on the middle node fails its own tests: a search that
        # never rejects anything is not a search
        if node.id == "store":
            self.emit("verify.started", label=f"{node.id}-0", model=node.model,
                      intent="keep the bucket in memory")
            self.emit("stage.result", label=f"{node.id}-0", stage="cheat", passed=True,
                      detail="0 finding(s), 0 critical")
            self.emit("stage.result", label=f"{node.id}-0", stage="f2p", passed=False,
                      detail="1/2 — test_survives_a_restart failed")
            self.emit("node.pruned", id=f"{node.id}-0",
                      reason="a held-out test says the bucket must survive a restart")
            self.emit("verify.started", label=f"{node.id}-1", model=node.model,
                      intent="persist the bucket, refill from the clock on load")

        label = f"{node.id}-1" if node.id == "store" else f"{node.id}-0"
        if node.id != "store":
            self.emit("verify.started", label=label, model=node.model, intent=node.goal)
        for stage, detail in STAGES:
            self.emit("stage.result", label=label, stage=stage, passed=True,
                      detail=detail.format(f2p=f"{len(node.tests)}/{len(node.tests)}",
                                           files=2, lines=48))
        node.status = "fulfilled"
        self.emit("node.fulfilled", id=node.id, tests=len(node.tests))

    def _review(self) -> dict:
        """The review stage. `--live` reaches the real reviewer; the demo replays
        findings it actually produced, and the event stream records which."""
        self.emit("review.started", scope="diff", reviewer="qodo", pass_no=1,
                  note="before the commit exists", scripted=self.demo)
        if not self.demo:
            try:
                result = self.qodo.call_tool(
                    "review_pr", {"pr": self.live_pr, "wait": REVIEW_WAIT, "poll": 15})
            except QodoUnavailable as e:
                self.emit("review.failed", reason=str(e)[:160])
                raise
        else:
            result = {"findings": [f.to_dict() for f in REPLAYED_FINDINGS],
                      "blocking": sum(1 for f in REPLAYED_FINDINGS if f.blocking),
                      "scripted": True}
        for f in result["findings"]:
            self.emit("review.finding", **f)
        self.emit("review.done", findings=len(result["findings"]),
                  blocking=result["blocking"], pass_no=1, scripted=self.demo)
        return result

    def _fix(self, finding: dict) -> None:
        self.emit("fix.started", title=finding["title"], severity=finding["severity"],
                  path=finding.get("path", ""))
        self.emit("verify.started", label="fix", model="truefoundry/openai/gpt-5.2",
                  intent=finding.get("fix") or "address the finding")
        self.emit("stage.result", label="fix", stage="cheat", passed=True, detail="0 finding(s), 0 critical")
        self.emit("stage.result", label="fix", stage="f2p", passed=True, detail="7/7")
        self.emit("fix.done", summary=finding.get("fix") or finding["title"])
