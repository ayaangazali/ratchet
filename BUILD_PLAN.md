# Ratchet — the build plan

What is decided, what is built, what is left, and the order to do it in. Written to
be handed to Claude Code and executed.

---

## 0. The pitch, and why it isn't another wrapper

Ratchet is a coding agent harness where the agent never decides it's done — the tests
do. Every step is a git commit plus a sandbox snapshot, and every candidate patch
clears a verifier gauntlet (build, cheat check, fail-to-pass, pass-to-pass, types,
lint, diff hygiene) before it sticks. Because each step is a restorable node, the run
is a **tree search over repo states**, with the verifier's score as the value function
and a scheduler deciding where to spend the next unit of compute. Stalled branches
fork in parallel, dead ends are pruned, and the winning path exits as one clean
squashed diff at a human approval gate.

Three claims, each demonstrable in under thirty seconds:

1. **The verifier is the loop condition, not the model's opinion.** Termination is
   `result.green`, never a `<done>` token. Partial credit is a scalar so the search
   can hill-climb instead of flipping a boolean.
2. **Forking is cheap because we snapshot the sandbox, not just the repo.** A branch
   inherits its parent's dependencies and warm cache. Everyone else re-runs
   `pip install` per attempt; we explore ten nodes while they explore two.
3. **The harness carries the weight.** Sub-agents, sandboxes, approvals, session
   persistence, multi-provider routing — all TrueForge. We built the search and the
   verifier. There is no container orchestration and no provider SDK in this repo.

---

## 1. What is already built

`make dev && make demo && make test` → the whole suite, under a minute, no Docker, no network.

| piece | state |
|---|---|
| the seven-stage gauntlet with the exact score formula | **done**, `verifier/gauntlet.py` |
| the fifteen lines of bash (test reset, markers, exit code outside them) | **done**, `verifier/eval_script.py` |
| cheat detector: ~20 rules, severity-graded, hard gate on critical | **done**, `verifier/cheat.py` |
| test parsers: pytest, jest, vitest, go, cargo, with anti-spoof guards | **done**, `verifier/parsers.py` |
| held-out test split, pooled into `f2p_ratio`, `delta` reported | **done** |
| Node + Tree: restorable states, atomic persistence, rendering | **done**, `node.py` |
| scheduler: score + novelty − depth + untried, budgets, stall rule | **done**, `scheduler.py` |
| the search loop with parallel fan-out and pruning | **done**, `loop.py` |
| negative-sibling injection | **done**, `context.py` |
| git state: commit per node, park, restore, squash | **done**, `gitstate.py` |
| sandbox interface + worktree fallback + `bench-snapshot` | **done**, `sandbox.py` |
| three sub-agent roles with per-role model routing | **done**, `subagents.py` |
| approval gate with a file fallback | **done**, `gate.py` |
| signed hash-chained receipts + `ratchet audit` | **done**, `receipts.py` |
| red-team battery: 10 attacks, 2 controls | **done**, `redteam.py` |
| linear-vs-search eval suite with error bars | **done**, `evals/` |
| the TUI: tree, stage rail, counters, budget, approval bar | **done**, `tui/` |
| CLI: run, tree, rewind, diff, verify, ship, replay, bench, redteam, audit, evals | **done** |
| offline everything: `--scripted` run, fixture, replay | **done** |
| **harness sandbox provider wired to a real snapshot backend** | **left**, T2 |
| **live model calls through TrueForge** | **left**, T3 (offline path proves the loop) |
| **one live approval pause and denial** | **left**, T4 |
| **docs oracle against a real page + a live repair** | **left**, T6 |

---

## 2. The loop, as built

```python
root = Node(commit, image=sandbox.snapshot(), score=gauntlet.run())
while budget.ok() and not any(n.green for n in tree.frontier()):
    node = scheduler.select(tree)                 # score + 0.3·novelty − 0.05·depth + 0.1·untried
    if scheduler.stalled:                         # 3 expansions with no improvement
        node, fanout = scheduler.stall_target(tree), 3   # highest-scoring SHALLOW node
    ctx = context.assemble(repo_map, node.last_failure, diff_so_far, dead_ends)
    for patch in subagents.generate(ctx, n=fanout):      # n>1 → n different providers
        child_env = provider.fork(node.image)            # warm cache inherited
        result = gauntlet.run(child_env, patch)
        prune_and_park(child) if result.regressed else tree.add(child)
    scheduler.observe(tree)
gate.request(git.squash(root, tree.best()))
```

Three details that are easy to get wrong and are worth defending in review:

- **Fork from the node, not from HEAD**, or you have a loop wearing a tree costume.
- **Fan out from a shallow node when stuck.** Expanding the deepest node when you are
  stuck is how a search tunnels into a dead branch and calls it progress.
- **Pruned work is parked, never destroyed** (`refs/ratchet/pruned/<node>`), and its
  one-line summary goes into its siblings' next prompt.

---

## 3. The gauntlet

| # | stage | fails how | weight |
|---|-------|-----------|--------|
| 1 | build / install | non-zero exit | hard gate, score 0 |
| 2 | cheat check (static, on the diff) | any critical pattern | hard gate, score 0 |
| 3 | fail-to-pass | target tests still failing | 0.5 |
| 4 | pass-to-pass | a previously-green test is now red | hard gate — regression |
| 5 | types | new type errors | 0.2 |
| 6 | lint | new violations only | 0.1 |
| 7 | diff hygiene | unrelated files, size blowup | 0.2 |

```
score = 0.5·f2p_ratio + 0.2·types_clean + 0.1·lint_clean + 0.2·diff_hygiene
green = f2p_ratio == 1.0 and p2p_intact and cheat_clean and build_ok
```

Cheat rules currently shipping: `protected_path`, `test_deleted`,
`test_file_emptied`, `skip_marker`, `assertion_removed`, `assertion_weakened`,
`assertion_downgraded`, `hard_exit`, `always_equal`, `report_hook_tamper`,
`runtime_test_write`, `mocked_in_source`, `config_loosened`, `env_bypass`,
`monkeypatch_assert`, `special_casing`, `broad_except_pass`, `mass_refactor`,
`log_spoofed`, `canary_passed`.

`runtime_test_write` exists because our own red team found it: reverting test files
before the run does nothing if the *source* rewrites them at import time, after the
revert. That is the value of shipping an eval of your own verifier.

---

## 4. The day, hour by hour

Workshops end at 11:00; submission at 18:00. Seven hours, four lanes.

| time | work | lane |
|------|------|------|
| pre-event | 3 target repos picked, base images built, parsers verified on each | all |
| 11:00–11:15 | **`make bench` — the snapshot decision** | B |
| 11:00–13:00 | verifier hardening on a real repo; second task file | A |
| 11:00–13:00 | wire `HarnessProvider` **or** prebuild the fallback base + warm venv | B |
| 13:00–15:00 | live models through the harness; prompt tuning until diffs parse | C |
| 13:00–15:00 | docs oracle against a real page; break it on purpose | A |
| 15:00–16:00 | the approval gate live, both paths; multi-provider fan-out on screen | C |
| 15:00–17:00 | the console | D |
| 16:00–16:45 | self-eval run on the real repos; record the numbers | A |
| 16:45–17:15 | **record the backup demo video** | D |
| 17:15–17:45 | Qodo pass on our own PRs, README, blog draft | all |
| **17:45** | **code freeze. Nothing merges after this.** | — |

**Rules.** Headless first, always — the TUI wraps a working loop, and if it is
half-done at 17:30 you ship headless and still demo. Merge a PR every ninety minutes
so the Qodo trail is real rather than retrofitted. The snapshot decision is timeboxed
to 45 minutes total; the fallback is a first-class path, not an apology.

---

## 5. Sponsor mapping

**TrueForge.** Three distinct sub-agent roles, not one: the cartographer (cheap
model, maps the repo once at startup so no later prompt pays to re-read the tree),
the generators (strong models, one per branch during fan-out, deliberately on
*different providers* so diversity is structural rather than temperature noise), and
the reviewer (runs over each candidate; advisory, because a model that can veto is a
model that can be argued with). Sandboxes give per-branch isolation so candidates
execute simultaneously without colliding. Approvals are the final node in the state
machine — no push, no PR, no destructive git op without a human seeing one squashed
diff. Session persistence is what makes `rewind` and reconnect real.

> The anti-pattern that loses this track is shelling out to Docker. If you are
> writing subprocess orchestration, stop: that is the harness's job, and doing it
> yourself throws away the argument. `grep -r "docker run" ratchet/` returns nothing,
> and that is a deliberate design property.

**Qodo.** Required for the code-quality track and thematically free: we ship a tool
whose thesis is that unverified agent output shouldn't merge, so every PR here goes
through Qodo and is dealt with before merge. `.pr_agent.toml` carries real review
instructions — leakage of held-out test names, paths that could set `green` outside
the gauntlet, weakened sandbox flags — not defaults.

**Bright Data.** The agent writes against APIs that changed six months ago. The docs
oracle pins versions from the lockfile, fetches through the CLI with a Web Unlocker
fallback, validates every fetch against an `expect` block, and repairs itself by
relocating sections by heading when a site restructures — writing the fix back into
`scrapers.yaml` with a timestamp and a reason, so the repair is a reviewable diff.
Sources with a `collector_id` escalate to Bright Data's own self-healing, whose
approval gate is wired to the same human gate as the pull request.

**OpenAI.** Cartographer and reviewer roles, plus one of the fan-out providers. Show
the cost-per-solve line from the budget bar; judges like a number.

---

## 6. Risks and cut lines

| risk | mitigation |
|---|---|
| snapshot forking slow or flaky | decided by `make bench` before noon; worktree fallback already built and tested |
| test parsing is per-framework and fiddly | five parsers already ship with tests; verify each target repo **before** the day, add no sixth on the day |
| the tree UI eats the afternoon | it renders off the bus and there is a fixture; headless works without it |
| no repo ready at demo time | `make demo` seeds one in two seconds, and it is in CI |
| budget runaway during the demo | hard caps on nodes, wall clock and dollars, visible in the TUI |
| a stale demo repo makes the verifier look broken | `redteam` refuses to run when the target tests already pass at HEAD |
| the model refuses to cheat, so the DQ never fires live | never rely on it — both cheating patches are pre-built and fed deliberately |
| venue Wi-Fi dies | six offline commands demonstrate every claim (`DEMO.md` §6) |

**If behind at 16:00, cut in this order:** deterministic replay → novelty bonus →
negative-sibling injection → the TUI (fall back to `ratchet tree` and stdout).
**Never cut the verifier.** Without the gauntlet this is a wrapper and the pitch
collapses.

---

## 7. The flex

Ship the harness with its own eval suite and run it on itself. `make evals` holds the
generator fixed and varies only the machinery — same bugs, same draws, same call
budget — and reports pass rate with error bars plus the number of cheating patches
that persisted:

```
overall   linear 50% ±14   ·   search 100% ±0
cheating patches that persisted   linear 1   ·   search 0
```

Then, in the demo: *"we changed the scheduler at 15:00 — here's the regression our own
harness caught."* No hackathon team shows a controlled experiment on its own
submission, and it proves the verifier is real because you pointed it at yourselves.

---

## 8. Handing it to Claude Code

`HANDOFF.md` is the briefing, `TASKS.md` the ordered backlog with acceptance criteria
and lanes, `CLAUDE.md` the contract, `RESEARCH.md` every verified tool fact and URL so
nobody searches twice. Four slash commands live in `.claude/commands/`:
`/verify`, `/harden`, `/ship`, `/demo`.

Start by running §1 of `HANDOFF.md`. If `make redteam` does not print 10/10, that is
the only thing that matters until it does.
