# Handoff — start here

You are picking up a repository that already works. Nothing below is a greenfield
task. Read this file, run the three commands in §1, and then work §3 in order.

**What this is:** Ratchet, a coding-agent harness in which the agent cannot decide
it is done. A verifier decides. Built on TrueForge for the Agent Harness Hackathon.
The full argument is in `README.md`, the plan is in `BUILD_PLAN.md`, the rules you
must not break are in `CLAUDE.md`, and every fact anyone looked up is in
`RESEARCH.md` so you do not need to search the web again.

---

## 1. Prove it works before you change anything

```bash
make dev          # editable install + dev deps  (python 3.11+)
make demo         # seeds demo-repo/ with a broken slugify and three patches
make test         # 27 tests, ~15s, no docker, no network
make redteam      # fires 10 known cheating patches at the verifier; expect 10/10
```

`make redteam` is the fastest way to understand the whole project. It prints:

```
caught 10/10 known reward-hacking patterns
false positives on the honest fix: 0
verifier holds: every known attack blocked, the real fix still accepted.
```

If that line does not print, stop and fix that before anything else. It is the
project's only real claim.

---

## 2. The shape, in one screen

```
TrueForge (owns the loop, context, MCP dispatch, sandbox, sub-agents, approvals)
    │  tool calls                                    ▲ tool.approval_required
    ▼                                                │
Ratchet MCP server ── the only door into the graded tree
    ├── repo_read / repo_grep / repo_tree     unrestricted reads
    ├── dry_run                               grade without consequence
    ├── propose_patch ──► THE PAWL ──► commit (green) | rollback (red)
    ├── docs_lookup   ──► Bright Data, pinned to the lockfile
    ├── fan_out / arbitrate                   parallel branches, scored not voted
    └── open_pull_request                     ← human-gated, the only egress
```

There is deliberately no `done` tool. Do not add one.

| file | what it owns |
|---|---|
| `src/ratchet/gauntlet/eval_script.py` | the bash that reverts tests before grading — the highest-leverage code here |
| `src/ratchet/gauntlet/parse.py` | log → status map, suite-ran sentinel, exit-code cross-check |
| `src/ratchet/gauntlet/grade.py` | F2P / P2P / held-out with the skip asymmetry |
| `src/ratchet/gauntlet/patchlint.py` | the cheat detector |
| `src/ratchet/gauntlet/score.py` | the scalar and the hard decision |
| `src/ratchet/gauntlet/runner.py` | the pawl: containers, timeouts, gates in order |
| `src/ratchet/ledger.py` | commit per step, park rejected, roll back |
| `src/ratchet/receipts.py` | hash-chained, signed verdict receipts |
| `src/ratchet/mcp_server.py` | the tool surface the harness calls |
| `src/ratchet/docs_oracle.py` | Bright Data pipeline with validation and self-repair |
| `src/ratchet/harness/` | TrueForge HTTP + SSE client, orchestrator, approval routing |
| `src/ratchet/tui/` | the console, driven entirely off the JSONL bus |
| `src/ratchet/redteam.py` | an eval of the verifier itself |

---

## 3. What to do, in order

Work `TASKS.md`. It has acceptance criteria per task and is ordered so that
stopping after any one of them still leaves a submission. One pull request per
task, reviewed by Qodo before merge — that is a hard requirement of the
competition, not a nicety.

The short version:

1. **T1 · accounts** — TrueForge running, Qodo installed on the repo, Bright Data
   key in `.env`. Thirty minutes, and everything else depends on it.
2. **T2 · live loop** — `make serve` + `make run` against a real TrueForge session.
   Expect the agent spec to need adjusting; `RESEARCH.md §1.5` has the shipped schema.
3. **T3 · approval gate** — one live pause and resume on `open_pull_request`.
4. **T4 · docker backend** — `make image`, then `RATCHET_BACKEND=docker make test`.
5. **T5 · console polish** — `make fixture` then `make console`; needs no model.
6. **T6 · docs oracle live** — one real source, then break it on purpose.
7. **T7 · demo + video + submission** — `DEMO.md` is the runbook, beat by beat.

---

## 4. Things that will bite you

Read `CLAUDE.md` §Invariants first; those are non-negotiable. Beyond them:

- **TrueForge SSE frames have no `event:` name.** Dispatch on `data["type"]`.
- **A finished turn's stream dies after ~5 minutes** (412). Hydrate from
  `/turns/{id}/events`, then attach with `after_sequence_number`.
- **Approval items may not be mixed with a user message** in one turn.
- **Sub-agents share the parent's sandbox and see none of the conversation.**
  Restate the task and the branch label in full in each `create_sub_agent` input.
- **Compaction key is `compaction_threshold_tokens`**, not the shape in the docs.
- **Local sandbox egress is PyPI + GitHub only.** npm is not on the allowlist.
- **Qodo's free OSS plan needs 200+ stars.** Use the trial; verify a review lands
  on PR #1 in hour one, not at 17:00.
- **`ratchet verify` is the demo's insurance.** It needs no model, no key and no
  network. Never let a change break it.

---

## 5. House style

Python 3.11+, `from __future__ import annotations`, dataclasses at boundaries,
`ruff` and `mypy` clean before a PR. Pure functions in `gauntlet/` — no I/O, no
subprocess, no network — which is why the tests run anywhere. Subprocess calls take
argv lists, never `shell=True`. Comments explain *why*, especially where the code
looks paranoid; most of the paranoia is load-bearing and a future reader will
delete it otherwise. Every new verification rule ships with two tests: a patch that
trips it and a patch that must not.

When a change makes a red patch pass, ask whether you fixed the patch or weakened
the verifier. Weakening the verifier to make something go green is exactly the
behaviour this project exists to catch, and it is no less wrong when a human does it.
