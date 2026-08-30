---
description: Open a reviewed pull request the way this repo requires
argument-hint: [short description of the change]
---

Ship the current work as a pull request: $ARGUMENTS

This repo's thesis is that unverified agent output shouldn't merge, so nothing here
merges unreviewed either — and the competition's code-quality track is judged on
exactly this trail.

1. Confirm the local gate: `make test && make lint && make redteam`. If the red team
   does not catch the whole battery with zero false positives, stop and fix that first.
2. Branch if you are on `main`: `git switch -c feat/<slug>`.
3. Commit with a message that says what changed and why, not what files moved.
4. `gh pr create`. In the body, name what a reviewer should look at hardest — usually:
   can any path set `green` outside `verifier/gauntlet.py`, and can a held-out test
   name reach a string the agent reads.
5. Wait for Qodo. If it does not appear within a couple of minutes, comment
   `/agentic_review` (hosted v2) or `/review` (v1/OSS).
6. **Deal with what it finds before merging.** Use "Apply this suggestion" for the
   ones you agree with, so the fix is attributable in the commit trail; reply in the
   thread to the ones you don't, saying why.
7. After merge, add a row to the Qodo table in `README.md`: PR link, what it changed,
   what Qodo found, how it was resolved.
