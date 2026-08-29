# Demo runbook

Two artefacts: a three-minute video for the submission, and a live run for the
judging table. They share the same beats. Both are built so that the most impressive
moment does not depend on the model doing anything clever.

---

## Before you start

```bash
make demo && make test          # 22 tests green in ~10s, no network
npx @truefoundry/trueforge@latest   # leave running
make serve                      # leave running
make console                    # leave running, full screen
```

Terminal at 16pt or larger. Two windows: the console, and one shell for the
no-model commands. Kill notifications.

**Rehearse the fallback once.** If TrueForge, the model API or the venue Wi-Fi is
down, `ratchet verify` still tells the entire story with no network at all. Section
5 is that path.

---

## The three-minute video

**0:00 — the claim (20s).**
> Give an agent a repo and a test command and it will eventually notice that the
> cheapest way to make tests pass is to change what "pass" means. This one can't.
> It has no way to declare itself finished. A verifier it doesn't control decides,
> and every step is a git commit that can be rolled back.

Show the spine idle on screen while you say it.

**0:20 — one honest loop (40s).**
Start the run. The agent reads, proposes a patch, the gate rail lights up left to
right, `hidden` goes green, the spine grows a tooth. Narrate one sentence: *the
agent didn't decide that worked — those eight gates did.*

**1:00 — the rollback (30s).**
The second attempt regresses a pass-to-pass test. Rail shows `p2p FAIL`, the spine
grows a red stub, the log says `rolled back to <sha>`. Say the line that matters:
> Stopped isn't a prompt asking nicely. It's a `git reset --hard` the model can't
> argue with. And the failure is now its next observation.

**1:30 — the cheat, caught before it runs (35s).**
Feed the prepared cheating patch (`demo-repo/patches/cheat.diff`) through
`propose_patch`, or run it from the shell. `integrity FAIL` fires first,
`DISQUALIFIED`, and nothing else in the gauntlet even executes.
> It hardcoded the three test cases and marked a held-out test as flaky. Static
> analysis of the diff, before a line of it ran.

Then the one that lands harder — the canary:
```bash
ratchet verify --task tasks/canary-impossible/task.yaml --repo demo-repo \
               --diff demo-repo/patches/canary_hack.diff
```
> No skip markers. No test edits. Zero static findings. It just returns a different
> answer the second time it's asked. We catch it because that task is impossible —
> the two assertions contradict each other. Any green result on it is a confession.

**2:05 — stall, fan-out, arbitration (35s).**
Three rejections in a row. The console prints `[stall]`, the agent calls `fan_out`,
three sub-agent threads appear inline in the stream with their own thread ids, and
the scoreboard fills in. Point at the `gap` column:
> `cand-b` passes everything it can see and nothing it can't. It scores below
> `cand-c`, which is honestly mediocre. The verifier picks. Nobody votes.

**2:40 — the gate (20s).**
Everything green. The agent calls `open_pull_request`. The approval bar takes the
whole screen. Hover, do not press, for a beat.
> This is the only action in the system that leaves the machine, and it's the
> harness holding it, not our prompt. Deny it and the agent gets the denial as an
> observation and keeps working.

Press approve. PR opens. End on the PR page with the Qodo review comment visible.

---

## Live at the table

Have all three of these ready to run in under ten seconds each, because judges
interrupt:

1. **"Show me it can't cheat."**
   `ratchet verify --task tasks/demo-001-slugify/task.yaml --repo demo-repo --diff demo-repo/patches/cheat.diff`
   Disqualified, with the two findings named and located.

2. **"What if the cheat is subtle?"**
   The canary command above. Zero static findings, still disqualified.

3. **"What is the harness actually doing?"**
   `.ratchet/<run>.bus.jsonl` — every `thread.created`, `tool.approval_required`,
   `sandbox.created` and compaction event, mirrored straight off TrueForge's SSE
   stream. Then show `agent/agent.json`: the sandbox block, the sub-agent block,
   `require_approval_for_tools: ["open_pull_request"]`.

4. **"Is the scraper real?"**
   Break it on purpose: edit `section:` for a source in `src/ratchet/scrapers.yaml`
   to a heading that no longer exists, run a lookup, and show the `docs.heal` event,
   the rewritten YAML, and the new `history` entry with the reason. `git diff` is the
   proof.

## Questions you will be asked

**"What stops the agent editing the verifier itself?"** The verifier is not in the
graded worktree. It runs in the orchestrator process, and `src/ratchet/gauntlet/` is
in `protected_paths`. The agent has no shell in the graded tree at all — only
`propose_patch`.

**"Isn't this just running tests in a loop?"** The held-out split and the gap
penalty. A test runner tells you whether the visible tests pass. This tells you
whether the fix generalises, and it prices overfitting *below* honest partial
progress. That difference is the project.

**"Why not let the model self-critique?"** We do, and then we ignore it. Self-report
is not evidence. The canary exists precisely because a model that is cheating will
also report that it is not.

**"What is TrueForge doing versus you?"** Everything except the definition of
progress: the loop, context and compaction, MCP dispatch and OAuth, the sandbox,
sub-agent threads, session resume, and the approval interrupt. We deleted a lot of
code by not writing it. The README has the table.
