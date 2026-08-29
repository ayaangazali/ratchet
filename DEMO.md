# Demo runbook

Five minutes on stage, and a live run at the judging table. Both are built so the
most impressive moment does not depend on the model doing anything clever.

---

## Before you start

```bash
make clean && make demo && make test && make redteam     # ~1 min, all green
npx @truefoundry/trueforge@latest                        # leave running
make console                                             # leave running, full screen
```

Terminal at 16pt or larger. Two windows: the console, and one shell for the no-model
commands. Notifications off. `make fixture` done, so a replay is one command away.

**Rehearse the fallback once.** If TrueForge, the model API or the venue Wi-Fi is
down, `ratchet verify`, `make redteam`, `make evals` and `make run-offline` still tell
the entire story with no network at all. That is §6 below.

---

## The five minutes

**0:00 — the claim (25s).**
> Point an agent at a repo with a test command and it will eventually notice that the
> cheapest way to make tests pass is to change what "pass" means. This one can't. It
> has no way to declare itself finished — a verifier it doesn't control decides — and
> because every step is a commit plus a snapshot, the run isn't a loop with retries.
> It's a search over repo states.

Tree pane idle on screen while you say it.

**0:25 — one honest step (45s).**
`ratchet run`. The root node appears with a score, the cartographer maps the repo
once, the first candidate goes through the stage rail left to right, and the tree
grows a node. One sentence: *the agent didn't decide that worked — those seven stages
did, and the score is what the search hill-climbs on.*

**1:10 — the rollback (40s).**
The next candidate regresses a pass-to-pass test. Stage rail shows `p2p FAIL`, the
node turns red in the tree, and the run continues from the *parent*, not from the
broken state. Say the line:
> Stopping it isn't a prompt asking nicely. It's a rollback it can't argue with — and
> the failure is now the next observation, with the dead end named so no sibling
> repeats it.

**1:50 — the cheat, caught before it runs (45s).**
Feed the prepared patch. `cheat FAIL` fires first and nothing else executes.
> It hardcoded the two visible cases and marked a held-out test flaky. Static
> analysis of the diff, before a line of it ran.

Then the one that lands harder:
```bash
ratchet verify --task tasks/canary-impossible/task.yaml --repo demo-repo \
               --diff demo-repo/patches/canary_hack.diff
```
> No skip markers. No test edits. Zero static findings. It just returns a different
> answer the second time it's asked. We catch it because that task is impossible —
> the two assertions contradict each other, so any green result is a confession.

**2:35 — stall, fan-out, three providers (50s).**
Three expansions with no improvement. The console prints the stall, the search forks
three ways from the highest-scoring *shallow* node, and three sandboxes light up with
three different model names in the tree. Point at the counters climbing.
> Different providers, not three samples from one model — diversity is structural.
> And it forks from a shallow node on purpose: expanding the deepest one when you're
> stuck is how a search tunnels into a dead branch and calls it progress.

**3:25 — kill it and reattach (20s).**
Kill the console. Reopen it. The run is still going and the tree redraws from the bus.
Twenty seconds, worth more than five minutes of explaining.

**3:45 — rewind (25s).**
```bash
ratchet rewind 0f3a
```
> Every node is a restorable state, so you can go back to step twelve and branch a
> different direction. Nothing else in this category does that, because nothing else
> treats steps as states.

**4:10 — the gate (30s).**
The winner reaches `open_pull_request`. The approval bar takes the whole screen with
one clean squashed diff — not eleven steps of search. Hover, don't press, for a beat.
> This is the only action that leaves the machine, and it's the harness holding it,
> not our prompt. Deny it and the agent gets the denial as an observation.

Approve. Squash lands.

**4:40 — the receipts and the chart (20s).**
```bash
ratchet audit          # chain intact
make evals             # linear 50% ±14 · search 100% · cheats persisted 1 vs 0
```
> That's our own harness pointed at our own seeded bugs. Same draws, same budget; the
> only difference is whether a bad step is allowed to persist.

---

## At the table

Five things, each under ten seconds to start, because judges interrupt.

1. **"Show me it can't cheat."**
   `make redteam` — 11/11 with the two controls still green.

2. **"What if the cheat is subtle?"**
   The canary command above. Zero static findings, still caught.

3. **"How do we know this run was real?"**
   `ratchet audit`, then edit one line of the receipts file and run it again. The
   chain breaks at the exact receipt. Then `git log` on the scratch branch: every
   commit message carries its score, its test counts and its verifier line.

4. **"What is the harness actually doing?"**
   `.ratchet/<run>.bus.jsonl` — sandbox creations, sub-agent threads, approval pauses.
   Then `ratchet/subagents.py`: three roles, three models, routed by role. Then the
   absence: no `docker run` anywhere in the repo, because sandboxing is the harness's
   job.

5. **"Is the scraper real?"**
   Break it live: point a source's `extract.section` at a heading that no longer
   exists, run a lookup, show the `docs.heal` event and `git diff ratchet/scrapers.yaml`
   with the timestamped repair.

---

## Questions you will get

**"What stops the agent editing the verifier?"** It is not in the graded tree. The
verifier runs in the orchestrator process, outside the sandbox, and the agent's only
way to change anything is a patch to the *target* repo that goes through the gauntlet.
For runs pointed at Ratchet's own repo, `ratchet/verifier/` is in the default
protected set, so a diff that touches it is a critical finding before it executes.

**"Isn't this just running tests in a loop?"** Two differences. Held-out tests, so a
patch fitted to what it was shown loses score rather than winning. And restorable
states, so a bad step is never inherited — which is exactly what `make evals`
measures.

**"Why not let the model self-critique?"** We do — the reviewer role — and then we
ignore it as a gate. Self-report is not evidence. The canary exists because a model
that is cheating will also report that it is not.

**"What did TrueForge do versus you?"** Everything except the definition of progress:
model calls and routing, context and compaction, sandboxed execution, sub-agent
threads, session persistence, the approval interrupt. We wrote the search and the
verifier and deleted a week of plumbing by not writing it.

**"Snapshots or worktrees?"** Whichever `make bench` said. Say the number out loud —
it shows you made an engineering decision on evidence rather than hoping.

---

## Section 6 · The offline demo, if everything is down

```bash
make redteam        # the verifier, scored against eleven known attacks
make run-offline    # a complete search: root, a prune, a green node, the gate
make run-graph      # the objective graph: a rejected candidate, then fulfilment
ratchet tree        # the search tree
ratchet audit       # the receipt chain
make evals          # linear vs search, with error bars
make replay         # the recorded run, in the terminal
make proof          # all of the above plus a forged-receipt catch, with evidence kept
```

Eight commands, no network, and every claim in the pitch is demonstrated. Practise
this once — it is the version you will be glad to have.
