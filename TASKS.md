# Tasks

Ordered so that stopping after any one still leaves a working submission. Lanes map
to the four roles in the build spec: **A** verifier · **B** sandbox and git ·
**C** loop and scheduler · **D** TUI and demo.

One pull request per task, reviewed by Qodo before merge. That is a competition
requirement, not a nicety. Add the merged PR to the README table the moment it lands.

Each task lists **acceptance** — the observable thing that has to be true. If you
cannot demonstrate it, the task is not done, however much code exists.

---

## T1 · Accounts and keys · 30 min · everyone · blocks everything

- [ ] `npx @truefoundry/trueforge@latest` serving on `http://localhost:8790`
- [ ] A model provider added in TrueForge → Settings → Model providers (at least two,
      so fan-out is genuinely multi-provider)
- [ ] Qodo GitHub App installed (`app.qodo.ai/signin` → Integrations → SaaS → GitHub).
      Use the 14-day trial; the free OSS plan needs 200+ stars
- [ ] `/config` commented on PR #1 to learn whether you have v1 or v2 command names
- [ ] `cp .env.example .env`, fill `BRIGHTDATA_API_KEY`
- [ ] Repo public, PR #1 open

**Acceptance:** a Qodo review appears on PR #1 unprompted, and
`curl localhost:8790/api/v1/models` lists your models.

---

## T2 · The snapshot decision · 45 min HARD TIMEBOX · B · 11:15

```bash
make bench
```

- [ ] Read the round trip. **Under ~5s** → wire `HarnessProvider` in
      `ratchet/harness/sandboxes.py` against the sandbox provider TrueForge is
      configured with, and run the tree search on real snapshots.
- [ ] **Over ~5s, or it fights you** → stop. Stay on `WorktreeProvider`, pre-build the
      base image, put a warm venv at `demo-repo/.ratchet/venv`, and move on. You lose
      the warm-cache flex; you keep the search, the verifier and the demo.
- [ ] Either way, record the number in the README.

**Acceptance:** `make bench` prints a verdict line, and `ratchet run` uses whichever
provider it chose. Do not spend a second hour on this.

---

## T3 · Live models through the harness · 90 min · C

The loop already runs end to end offline (`make run-offline`), so this is about the
backend, the prompts and the routing — not the search.

- [ ] `make run` against a live TrueForge session
- [ ] Confirm the three roles use their configured models, and that a fan-out uses
      **different providers** rather than three samples from one
- [ ] Tune the generator prompt until patches come back as clean unified diffs with
      an `intent:` line — that is the only contract `subagents._extract_patch` needs
- [ ] Watch one real prune and confirm the failure text reaches the next prompt

**Acceptance:** a real run reaches green, `ratchet tree` shows at least one pruned
node, and `ratchet audit` says the chain is intact.

---

## T4 · The approval gate, live · 45 min · C

- [ ] Drive a run to green so it reaches `request_ship`
- [ ] Approve from the console (`a`) and confirm the squash happens
- [ ] **Deny once**, and confirm the run reports the denial and nothing was pushed
- [ ] Confirm the file fallback works with the console closed:
      `echo '{"allow": true}' > demo-repo/.ratchet/approvals/<id>.json`

**Acceptance:** both paths demonstrated. The denial matters more than the approval —
it is the difference between a gate and a speed bump.

---

## T5 · Console polish · 90 min · D · needs no model

```bash
make fixture && make console
```

- [ ] Every state renders: root, kept node, pruned node, integrity violation, stall,
      fan-out across three providers, approval bar, budget line
- [ ] Legible on a projector and in a narrow terminal
- [ ] The ambient counters (sub-agents, sandboxes live, approvals) are always visible
- [ ] Add the receipt-chain head to the header, so "this run is verifiable" is on
      screen rather than in a README

**Acceptance:** a stranger watching a replay can say, unprompted, what the agent is
doing, what it is waiting on, and what it did.

---

## T6 · Docs oracle against a real page · 60 min · A

- [ ] Add one dependency this repo actually uses to `ratchet/scrapers.yaml`, with
      `url`, `extract.section` and an `expect` block
- [ ] Run a lookup and confirm real markdown comes back (CLI first, Web Unlocker
      fallback)
- [ ] **Break it on purpose**: point `extract.section` at a heading that no longer
      exists, run again, watch the oracle relocate the section, rewrite the YAML and
      append to `history`
- [ ] Commit that diff — it *is* the Bright Data evidence
- [ ] Optional: create a Scraper Studio collector and set `collector_id`, so the
      escalation path calls `bdata scraper heal` with its own approval gate

**Acceptance:** `git diff ratchet/scrapers.yaml` shows the repair with a timestamp
and a reason, and the bus contains a `docs.heal` event.

---

## T7 · Freeze, demo, submit · 75 min · D + everyone

- [ ] **16:45 feature freeze.** No exceptions, whatever is unfinished
- [ ] Run the demo three times start to finish (`DEMO.md`)
- [ ] Record the ~5 minute video; have the backup recording ready
- [ ] Fill `SUBMISSION.md`, paste the TrueForge write-up into the README
- [ ] Fill the Qodo table with real merged PR links
- [ ] Submit, then write the blog post from `BLOG.md`

**Acceptance:** submitted with fifteen minutes to spare.

---

## Stretch, in value order

- [ ] **S1 · Dogfood.** Seed a bug in `ratchet/verifier/parsers.py`, point Ratchet at
      its own repo, let it open the PR, let Qodo review the agent's patch. Highest
      storytelling value of anything left: *"we changed the scheduler at 15:00 — here
      is the regression our own harness caught."*
- [ ] **S2 · Deterministic replay.** Seed every model call and sandbox id so
      `ratchet replay <node>` re-runs bit-exact.
- [ ] **S3 · Normalised majority vote** across siblings: `ast.parse` → strip
      docstrings → `ast.unparse` → vote on the canonical diff. About twenty lines, and
      it lets `arbitrate` cite agreement as well as score.
- [ ] **S4 · A second real repo** with a different framework, to show the harness is
      not wired to one demo. `verifier/parsers.py` already speaks jest, vitest, go and
      cargo — this is mostly a task file.
- [ ] **S5 · Impossible-task escape hatch.** Give the generator a way to say "this
      task is unsatisfiable". Published work puts the cheating rate drop at 54% → 9%,
      and the canary then measures whether it actually uses it.

---

## Definition of done for any PR

1. `make test` green, `make lint` clean.
2. `make redteam` still catches the whole battery with zero false positives.
3. A Qodo review, with each finding either fixed or answered in the thread.
4. No new way to end a run, and no new path that can set `green` outside
   `verifier/gauntlet.py`.
5. If it touched the verifier: a test with a patch that trips the new rule, a patch
   that must not, and an entry in the red-team battery.
