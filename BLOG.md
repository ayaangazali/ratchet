# The agent doesn't get a vote

*Draft outline. Write it the same evening — it is much easier while the day still hurts.*

---

**1. The moment (200 words).**
Open with the demo everyone remembers: a patch that passes every test you can see,
gets rejected anyway, and the reason. Not "our agent is safe" — a specific patch, a
specific verdict, a specific number.

**2. The thing nobody designs for (400 words).**
Give an agent a repo and a test command and it will eventually notice that the
cheapest way to make tests pass is to change what "pass" means. Cite it properly:
ImpossibleBench's ~50% cheat rate on provably unsatisfiable tasks; Anthropic's
production RL runs where models learned `sys.exit(0)`, an always-True `__eq__` and a
conftest hook that rewrites pytest's report objects. Make clear this is measurement,
not speculation, and that a prompt saying "please don't cheat" is not a control.

**3. The inversion (400 words).**
The tempting design — let the agent run, check afterwards — makes the verifier
advisory. Ours makes it the loop condition. Show the loop and point at the absence:
there is no `done` tool, and termination is `result.green`.

**3b. And then the thing that falls out of it (400 words).**
Once every step is a *restorable* state — a commit plus a snapshot — retrying stops
being the only move. You can fork from any node, prune a branch without losing it, and
come back to step twelve tomorrow. The loop becomes a search, and the verifier's score
becomes the value function. Include the tree render; it explains itself.

**4. Fifteen lines of bash (500 words).**
The highest-leverage code in the project. Apply, `git checkout base -- tests/`,
re-apply pristine tests, run between markers, echo the exit code *outside* the parsed
region. Explain each line by the attack it kills. Credit SWE-bench, which figured
most of this out first, and its issue #620.

**5. The number that makes it a verifier (400 words).**
Held-out tests and the `visible − hidden` penalty. A patch that aces what it can see
and flunks what it cannot scores *below* one that is honestly mediocre. Show the
scoreboard from a real fan-out and let the `gap` column do the arguing.

**6. The canary (300 words).**
A task whose two assertions contradict each other. Zero false positives by
construction. Show the patch that defeats it and trips no static rule — it just
returns a different answer the second time it is asked — and be honest that this is
the class of hack static analysis will never catch.

**6b. Does the search actually beat a loop? (350 words).**
The eval nobody runs on their own submission. Same bugs, same draws, same budget; the
only difference is whether a bad step persists. Linear 50% ±14 versus search 100%, and
seven cheating patches that stuck under linear versus zero. Define "stuck" honestly:
still in the trial's final state — linear has no rollback so every applied cheat
persists, and under search the zero is structural, because nothing is inherited unless
the verifier passed it. Be explicit that the generator is simulated and that this
measures machinery, not model quality — the honesty is what makes the number worth
anything.

**7. We tested our own defences (300 words).**
`make redteam`, the scorecard, and the attack it surfaced that we had not thought of:
source that rewrites a test file at import time, after the revert. Say plainly that
the red team found a real hole and that the fix is a rule you can read.

**8. What the harness did (300 words).**
The honest accounting: the loop, context and compaction, MCP dispatch and OAuth, the
sandbox, sub-agent threads, session resume, the approval interrupt. Roughly a week of
work we did not do, and the day we spent instead on the one thing it cannot know.

**9. What broke (300 words).**
Write this one properly. The apply chain deleting its own patch file via
`git clean -fd`. The demo's "fixed" implementation that failed its own regression
test — caught by the verifier, which was the point. Whatever goes wrong on the day.

**10. Close (150 words).**
Stopped isn't a prompt asking nicely. It's a rollback the model can't argue with.

---

**Publishing notes:** repo link, a screenshot of the console mid-fan-out, and the
red-team scorecard as a code block. Lead with the scorecard on social — it is the
one artefact that is legible without context.
