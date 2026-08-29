---
description: Open a reviewed pull request the way this repo requires
argument-hint: [short description of the change]
---

Ship the current work as a pull request: $ARGUMENTS

This repository's whole thesis is that unverified agent output should not merge, so
nothing here merges unreviewed either — and the competition's code-quality track is
judged on exactly this trail.

1. Confirm the local gate is green: `make test && make lint && make redteam`.
   If `make redteam` is not 10/10 with zero false positives, stop and fix that first.
2. Branch if you are on `main`: `git switch -c feat/<slug>`.
3. Commit with a message that says what changed and why, not what files moved.
4. Push and open the PR with `gh pr create`. In the body, state what a reviewer
   should look at hardest — usually: can any path now set ACCEPTED outside the
   decision function, and can any held-out test name reach a string the agent reads.
5. Wait for Qodo's review. If it does not appear within a couple of minutes, comment
   `/agentic_review` (hosted v2) or `/review` (v1/OSS).
6. **Deal with what it finds before merging.** Use the "Apply this suggestion"
   button for the ones you agree with, so the fix is attributable in the commit
   trail. Reply in the thread to the ones you disagree with, saying why.
7. After merge, add a row to the `## Qodo Code Review Evidence` table in
   `README.md`: PR link, what it changed, what Qodo found, how it was resolved.
