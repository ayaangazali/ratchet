# Handoff — start here

You are picking up a repository that already works. Nothing below is greenfield.
Read this, run §1, then work `TASKS.md` in order.

`README.md` has the argument, `CLAUDE.md` the rules you must not break, `RESEARCH.md`
every tool fact anyone looked up (so you do not need to search the web again),
`DEMO.md` the runbook, `SUBMISSION.md` the checklist.

---

## 1. Prove it works before you change anything

```bash
make dev          # editable install + dev deps (python 3.11+)
make demo         # seeds demo-repo/: a broken slugify, three prepared patches
make test         # 41 tests, ~40s, no docker, no network
make redteam      # 10 known cheating patterns fired at the verifier — expect 10/10
make run-offline  # a complete search with no model: root, a prune, a green node
```

`make redteam` is the fastest way to understand the whole project. It must print:

```
caught 10/10 known reward-hacking patterns
false positives on the honest fix: 0
verifier holds: every known attack blocked, the real fix still accepted.
```

If it does not, stop and fix that first — it is the project's only real claim. If it
complains that the repo is not at its baseline, run `make clean && make demo`.

---

## 2. The shape, in one screen

```
scheduler.select ──► loop.expand ──► subagents.generate (n providers)
                                   └► sandbox.fork(parent.image)
                                      └► verifier.gauntlet ──► commit  |  prune+park
                                                                 │
                                                     tree.best ──┴──► gate ──► squash
```

| file | owns |
|---|---|
| `ratchet/loop.py` | the search loop |
| `ratchet/node.py` | Node and Tree — restorable states, persistence, rendering |
| `ratchet/scheduler.py` | selection score, novelty, budgets, the stall rule |
| `ratchet/sandbox.py` | harness provider, worktree fallback, snapshot benchmark |
| `ratchet/gitstate.py` | commit per step, park, restore, squash |
| `ratchet/context.py` | repo map + failure + diff so far + **dead ends** |
| `ratchet/subagents.py` | cartographer, generators, reviewer; model routing |
| `ratchet/gate.py` | the approval gate |
| `ratchet/verifier/gauntlet.py` | the seven stages and the score — **the product** |
| `ratchet/verifier/cheat.py` | the cheat detector |
| `ratchet/verifier/eval_script.py` | the fifteen lines of bash that make it work |
| `ratchet/receipts.py` | hash-chained, signed results |
| `ratchet/redteam.py` | an eval of the verifier itself |
| `ratchet/evals/` | linear vs search, on our own seeded bugs |
| `ratchet/tui/` | the console, driven entirely off the JSONL bus |

---

## 3. What to do, in order

`TASKS.md` has acceptance criteria per task and lanes per person. The short version:

1. **T1 · accounts** — TrueForge running, Qodo installed on the repo, Bright Data key
   in `.env`. Thirty minutes, and everything depends on it.
2. **T2 · the snapshot decision** — `make bench`. Under ~5s round trip: wire
   `HarnessProvider` and run the tree search on real snapshots. Over it: stay on
   worktrees and stop touching it. **Timebox this to 45 minutes.**
3. **T3 · live models** — `make run` against a real TrueForge session; the offline
   path already proves the loop, so this is about the backend and the prompts.
4. **T4 · the approval gate, live** — one real pause and one real denial.
5. **T5 · console polish** — `make fixture && make console`; needs no model.
6. **T6 · docs oracle live** — one real source, then break it on purpose.
7. **T7 · freeze, demo, submit** — `DEMO.md`, beat by beat.

Every one of these is independently demoable, so stopping after any of them still
leaves a submission.

---

## 4. Things that will bite you

Read `CLAUDE.md` §Invariants first. Beyond those:

- **Do not write container orchestration.** Sandboxes come from the harness; the
  fallback is git worktrees. `docker run` in this repo throws away the argument.
- **TrueForge SSE frames have no `event:` name.** Dispatch on `data["type"]`.
- **A finished turn's stream dies after ~5 minutes** (412). Hydrate from
  `/turns/{id}/events`, then attach with `after_sequence_number`.
- **Approval items may not be mixed with a user message** in one turn.
- **Sub-agents share the parent's sandbox and see none of the conversation.**
  Restate the task and the branch label in full.
- **Held-out test names must never reach a prompt.** Grep for `f2p_hidden` in every
  path that builds context before you freeze.
- **A stale demo repo makes the verifier look broken.** `redteam` now refuses to run
  when the target tests already pass at HEAD; if you see that error, reseed.
- **Qodo's free OSS plan needs 200+ stars.** Use the 14-day trial and confirm a
  review lands on PR #1 in hour one, not at 17:00.
- **`ratchet verify` is the demo's insurance.** No model, no key, no network. Never
  let a change break it.

---

## 5. House style

Python 3.11+, `from __future__ import annotations`, dataclasses at boundaries, ruff
and mypy clean before a PR. Pure functions in `verifier/` — no I/O, no subprocess, no
network — which is why the tests run anywhere. Subprocess calls take argv lists,
never `shell=True`. Comments explain *why*, especially where the code looks paranoid;
most of the paranoia is load-bearing and a future reader will delete it otherwise.
Every new verification rule ships with two tests and a red-team entry.

When a change makes a red patch pass, ask whether you fixed the patch or weakened the
verifier. Weakening the verifier to get to green is exactly the behaviour this
project exists to catch, and it is no less wrong when a human does it.
