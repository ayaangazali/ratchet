# CLAUDE.md — project rules for Ratchet

Read this before writing code here. It is the contract, not a summary.

---

## What this is

A coding agent harness where the agent **never decides it's done** — the tests do.
Every step is a git commit plus a sandbox snapshot; every candidate patch clears a
verifier gauntlet before it sticks; because each step is restorable, a run is a
**tree search over repo states** with the verifier's score as the value function.
Stalled branches fork in parallel, dead ends are pruned, and the winning path exits
as one squashed diff at a human approval gate.

TrueForge owns sub-agents, sandboxes, approvals, session persistence and model
routing. We own the search and the verifier. That division is the point of the
project and of the track we are entering.

---

## Invariants — do not break these, ever

1. **Only the gauntlet declares success.** `green` is set in exactly one place,
   `verifier/gauntlet.py`. If you want a `force_green`, you have misread the project.
2. **There is no `done` tool.** Never add one. The agent's belief about its own work
   is not an input to anything.
3. **Protected paths are reverted before grading**, every run, with no flag to skip.
4. **The exit code is echoed outside the parsed region.** Anything between the
   markers is agent-influenced and therefore untrusted.
5. **Held-out test names never appear in anything the agent can read.** Check
   `context.assemble`, `GauntletResult.to_observation`, and every bus event that
   could reach a prompt. A leak silently destroys the entire signal.
6. **Pruned work is parked before it is dropped**, at `refs/ratchet/pruned/<node>`.
   A dead end is still a node you can rewind to.
7. **Nothing irreversible happens without the gate.** `open_pull_request` and any
   push go through `gate.py`. Add an irreversible action, add it there in the same
   commit.
8. **Every graded node is receipted.** `receipts.record_result` runs on the accepted
   path and the pruned path alike. A node that skips it is a hole in the chain.
9. **`make redteam` stays at 10/10 with zero false positives**, and it is in CI. If
   your change breaks it, your change is the problem — unless the attack was
   mis-specified, in which case fix the attack and say so in the PR.
10. **We do not orchestrate containers.** No `docker run`, no `subprocess` container
    management, no provider SDK. Sandboxes come from the harness; the worktree
    fallback is git, not Docker. Writing container orchestration here throws away the
    argument for building on a harness at all.

## Conventions

- Python 3.11+, `from __future__ import annotations`, dataclasses at boundaries,
  `ruff` and `mypy` clean before a PR.
- `verifier/` holds pure functions — `parsers`, `grade`, `cheat` take data and return
  data, no I/O, no subprocess, no network. That is why the tests run anywhere.
- Subprocess calls take argv lists. Never `shell=True`, never an agent-influenced
  string interpolated into a shell.
- Comments explain *why*, especially where the code looks paranoid. Most of the
  paranoia is load-bearing and a future reader will delete it otherwise.
- Every new verification rule ships with two tests — a patch that trips it and a
  patch that must not — plus an entry in the red-team battery.

## Commands

```bash
make dev              # editable install + dev deps
make demo             # seed demo-repo/ with the broken slugify and three patches
make test             # the whole suite; no docker, no network
make lint             # ruff + mypy
make redteam          # score the verifier against known cheating patterns
make evals            # linear vs search on our own seeded bugs
make bench            # time a sandbox fork round trip (the pre-noon decision)
make fixture          # a recorded run, so the console builds with no model
make console          # the TUI
make audit            # verify the latest run's receipt chain

ratchet verify --task tasks/demo-001-slugify/task.yaml --repo demo-repo \
               --diff demo-repo/patches/cheat.diff     # the gauntlet, no agent
ratchet run --repo demo-repo --scripted demo-repo/patches/scripted.json
```

---

## Sandboxes — the harness's job, not ours

`sandbox.py` is an interface with two implementations:

- **`HarnessProvider`** — execution and snapshots come from the sandbox provider
  TrueForge is configured with. A child inherits its parent's installed dependencies
  and warm build cache. This is the path that makes forking cheap.
- **`WorktreeProvider`** — the documented fallback: one git worktree per node off a
  prebuilt base, every attempt sharing a pre-warmed virtualenv. No snapshots, same
  search, same verifier, same demo.

**Decide between them with `ratchet bench-snapshot`, and decide early.** Under ~5s
round trip, run the full tree search on snapshots. Over it, take the fallback and
stop touching it — the search is the product, snapshotting is an optimisation of it.

---

## Bright Data — the pipeline lives in the repo, not beside it

The docs oracle keeps the agent honest about the outside world: when a failure looks
like an import error, a missing attribute or an unexpected keyword argument, the
current upstream documentation for the **exact version in the lockfile** is attached
to the next prompt instead of letting the model guess from memory.

**Configuration lives in `ratchet/scrapers.yaml` and is version controlled.** Never
scrape with a one-off shell command; add a source there so the change is reviewable
and the repair history is auditable.

```bash
npx -p @brightdata/cli brightdata login
npx -p @brightdata/cli brightdata add mcp --agent claude-code --global
npx -p @brightdata/cli brightdata skill add scraper-studio
```

- **Fetch order is CLI → Web Unlocker REST.** `brightdata scrape <url> -f markdown`
  first; on failure `POST https://api.brightdata.com/request` with
  `{"zone": …, "url": …, "format": "raw", "data_format": "markdown"}`.
- **Extract by heading, not by CSS selector.** Headings survive redesigns; class
  names do not. Cheapest resilience available.
- **Every fetch is validated** against the source's `expect` block. An unvalidated
  fetch is a guess, not a fetch.
- **Repair is a diff.** On validation failure the oracle relocates the section by
  heading similarity, rewrites `scrapers.yaml` and appends to that source's `history`
  with a timestamp and a reason. Commit it — that diff is the evidence.
- **Escalate to Bright Data's own self-healing** when a source has a `collector_id`:
  `bdata scraper heal <id> "<what broke>"`. Keep `auto_approve: false`; its approval
  gate showing a `diff_summary` is a feature, and it is the same gate the PR uses.

---

## Qodo — every change goes through a reviewed pull request

The submission requires the review trail, and the project's thesis is that unverified
agent output should not merge. Direct pushes to `main` contradict the pitch.

1. Branch: `git switch -c feat/<thing>`.
2. Open a PR. Qodo reviews automatically; if not, comment `/agentic_review` (hosted
   v2) or `/review` (v1/OSS). Run `/config` once to find out which you have.
3. **Deal with what it finds before merging.** Use *Apply this suggestion* for the
   ones you agree with so the fix is attributable in the commit trail.
4. `.pr_agent.toml` and `best_practices.md` are committed on purpose: a reviewer
   configured deliberately beats one running on defaults.
5. Add the merged PR to the README table as you go, not at 17:55.

---

## TrueForge — what the harness owns

| concern | owner |
|---|---|
| model calls, retries, multi-provider routing | TrueForge |
| context management and compaction | TrueForge |
| MCP connections, OAuth, tool dispatch | TrueForge |
| sandboxed execution and snapshots | TrueForge |
| sub-agent threads and parallelism | TrueForge |
| session persistence, resume after reconnect | TrueForge |
| the approval interrupt | TrueForge |
| **what counts as progress** | **Ratchet** |

Wire facts that cost hours if you learn them the hard way:

- SSE frames carry **no `event:` name**. Dispatch on `json.loads(data)["type"]`.
- The SSE `id` is a monotonic per-turn sequence number; reconnect with
  `?after_sequence_number=N`. A finished turn's stream is collected minutes later and
  then returns **412** — hydrate from `/turns/{id}/events` first, then attach.
- Approvals are **not** a callback. The turn ends with `state.required_actions` and
  you resume with a *new* turn whose items are `user.tool_approval`. **Never mix
  approval items with a user message.**
- Sub-agents share the parent's sandbox and see none of the conversation. Restate
  the task and the branch label in full.
- Compaction key is `compaction_threshold_tokens`, not the shape in the docs.
- Local sandbox egress is allowlisted to PyPI and GitHub only.

---

## When you are stuck

Read the failing stage. Run `ratchet verify` on the patch by hand with no model in
the loop. Add a test that reproduces it. Only then change the verifier.

Changing the verifier to make a patch pass is exactly the behaviour this project
exists to catch, and it is no less wrong when a human does it.
