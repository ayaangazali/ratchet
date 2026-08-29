# Ratchet

**A coding agent that cannot decide it is done. The tests decide.**

Every step the agent takes is a git commit on a scratch branch. Every proposed patch
runs a verifier gauntlet — build, fail-to-pass, held-out tests, pass-to-pass, types,
lint, and a cheat detector that catches deleted tests and weakened assertions —
before it is allowed to stick. Anything that regresses is rolled back to the last
green commit and fed back as the next observation. When the agent stalls three times,
sub-agents fan out across parallel branches and the verifier keeps whichever one
scores highest. One human approval gate stands in front of the only action that
leaves the machine.

A ratchet turns one way. That is the whole idea.

```
  |  = 4f2a19c  score 0.981  ACCEPTED        <- committed, cannot be undone by the agent
     x 8a31    DQ special_casing,skip_marker  <- rolled back, parked, never merged
     x 71c9    score 0.612  delta 1.00        <- passed everything it could see
  |  = 0b7e441  score 0.744  ACCEPTED
  |  = 1c09aa2  run start
```

---

## Why this shape

Give an agent write access to a repository and a test command, and it will
eventually discover that the cheapest way to make tests pass is to change what
"pass" means. This is measured, not theoretical: on tasks whose tests are made
provably unsatisfiable, frontier models report success **~50% of the time** rather
than reporting the task impossible ([ImpossibleBench](https://arxiv.org/html/2510.20270v1)).
Anthropic has documented production coding-RL runs where models learned
`sys.exit(0)`, an `__eq__` that always returns `True`, and a `conftest.py` hook that
rewrites pytest's own report objects ([paper](https://arxiv.org/html/2511.18397v1)).

So Ratchet takes the decision away. The agent proposes; a verifier it does not
control disposes. Three properties fall out:

- **The stopping condition is external.** There is no `done` tool. Prose asserting
  the fix is complete does nothing.
- **Failure is information, not damage.** A red verdict rolls the tree back and
  becomes the next observation, so the agent argues with a test result rather than
  with itself.
- **The interesting number is the gap.** Every task holds tests back. A patch that
  aces the visible tests and flunks the held-out ones scores *below* one that is
  honestly mediocre on both.

---

## Install and run

Requires Python 3.11+, git, and Node 22.14+ for TrueForge.

```bash
# 1. the harness
npx @truefoundry/trueforge@latest          # http://localhost:8790, SQLite, no login

# 2. ratchet
git clone <this repo> && cd ratchet
make dev
make demo                                   # seeds demo-repo/ with a broken slugify

# 3. three panes
make serve                                  # ratchet MCP server on :8931
make run                                    # creates the TrueForge session, drives the task
make console                                # the TUI
```

No model, no API key, no network needed to see the core working:

```bash
ratchet verify --task tasks/demo-001-slugify/task.yaml --repo demo-repo \
               --diff demo-repo/patches/honest.diff     # ACCEPTED
ratchet verify --task tasks/demo-001-slugify/task.yaml --repo demo-repo \
               --diff demo-repo/patches/cheat.diff      # DISQUALIFIED, before it executes
ratchet verify --task tasks/canary-impossible/task.yaml --repo demo-repo \
               --diff demo-repo/patches/canary_hack.diff # DISQUALIFIED by the canary
```

---

## How it works

```
  TrueForge  ──────────────────────────────────────────────────────────────
  model loop · context + compaction · MCP dispatch · sandbox · sub-agents ·
  session persistence · approval interrupt
       │                                                        ▲
       │ tool calls                                             │ tool.approval_required
       ▼                                                        │
  Ratchet MCP server  ── the only door into the graded tree ─────┘
       │
       ├── repo_read / repo_grep / repo_tree      unrestricted reads
       ├── dry_run                                grade without consequence
       ├── propose_patch  ─────────────►  THE PAWL  ─────────►  commit  or  rollback
       ├── docs_lookup    ─────────────►  Bright Data (pinned to the lockfile)
       ├── fan_out / arbitrate         parallel branches, scored not voted
       └── open_pull_request           ← the only irreversible action, human-gated
```

**The pawl** runs eight gates in a fixed order. `integrity` runs first, on the diff
text, before a single line of the patch has executed — a patch that edits a graded
test is rejected without ever running. Then: does it apply, does it build, do the
visible fail-to-pass tests pass, do the **held-out** ones, does the regression suite
survive, do types and lint hold.

**The eval script** is the load-bearing anti-tamper primitive and it is about fifteen
lines of bash: apply the patch, `git checkout <base> -- tests/` to erase any edit the
agent made to a graded path, re-apply the pristine tests, run the suite between
unambiguous markers, and echo the test command's exit code *outside* the parsed
region so a patch that prints fake `PASSED` lines is caught by the mismatch.

**The score** is what makes it a verifier rather than a test runner:

```
score ∝  1.00·hidden + 0.60·p2p + 0.25·visible + 0.20·integrity + types + lint
       −  0.80·(visible − hidden)     ← the reward-hacking gap
       −  0.10·(blast radius)
```

**The canary** is a task whose two assertions contradict each other. Nothing can
satisfy it, so any green result on it is proof the grader was defeated — a cheat
detector with zero false positives, catching hacks no static rule can see. The
repository ships one that no `patchlint` rule fires on: a patch that simply returns a
different answer the second time it is asked.

**The approval gate** is enforced by the harness, not by our prompt.
`open_pull_request` sits in `require_approval_for_tools`, TrueForge suspends the turn
with `tool.approval_required`, and the run resumes only when a human answers. Deny it
and the agent gets a denial reason as an observation and keeps working.

---

## The console

Three questions answerable from across a room: what is it doing (the stream, with
sub-agent threads inline), what is it waiting on (the gate rail and the approval
bar), what did it do (the ratchet spine — green teeth stuck, red stubs rolled back).
The approval bar is the only widget that takes the full width, because an
irreversible action should interrupt the room rather than wait to be noticed.

If the TUI dies mid-run the approval still works:
`echo '{"allow": true}' > .ratchet/approvals/<tool_call_id>.json`.

---

## Layout

```
src/ratchet/
  models.py            Verdict, TaskSpec, GateResult — everything that crosses a boundary
  gauntlet/
    eval_script.py     the bash that reverts tests before grading
    parse.py           log → status map, suite-ran sentinel, exit-code cross-check
    grade.py           F2P / P2P / held-out, with the SWE-bench skip asymmetry
    patchlint.py       the cheat detector
    score.py           the scalar and the hard gate
    runner.py          the pawl: containers, timeouts, gates in order
  ledger.py            git as memory: commit per step, park, roll back
  workspace.py         one worktree per candidate; the read-only file API
  mcp_server.py        the tool surface the harness calls
  docs_oracle.py       Bright Data pipeline with validation and self-repair
  harness/             TrueForge HTTP + SSE client, orchestrator, approval routing
  tui/                 the console
tasks/                 task specs, including the impossible canary
```

## Tests

```bash
make test
```

Runs with the `LOCAL` backend so it needs no Docker and no network. The end-to-end
tests seed the demo repository and put three real patches through the real pawl: an
honest fix (accepted), a hardcoding patch that also skips a held-out test
(disqualified before execution), and the canary hack (disqualified by construction,
with zero static findings).

---

## Qodo Code Review Evidence

Every change in this repository went through a pull request reviewed by Qodo.
Configuration is committed at `.pr_agent.toml` and `best_practices.md`.

| PR | What it changed | Qodo findings | Resolution |
|----|-----------------|---------------|------------|
| #_  | _fill in as you merge_ | | |

## Credits and prior art

The verification design borrows deliberately and openly:
[SWE-bench](https://www.swebench.com/SWE-bench/reference/harness/) for the
fail-to-pass / pass-to-pass vocabulary, the test-reset trick and the exit-code
cross-check; [ImpossibleBench](https://arxiv.org/html/2510.20270v1) for the canary;
[EvilGenie](https://arxiv.org/abs/2511.21654) and SpecBench for held-out tests and
the visible-minus-hidden gap; [Agentless](https://arxiv.org/html/2407.01489v2) for
normalised patch voting and regression-count ranking. Built on
[TrueForge](https://github.com/truefoundry/trueforge).

MIT.
