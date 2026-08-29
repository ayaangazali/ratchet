# Tasks

Ordered so that stopping after any one of them still leaves a working submission.
One pull request each, reviewed by Qodo before merge. Tick as you go, and add the
merged PR link to the table in `README.md` the moment it merges — retrofitting that
table at 17:55 is how teams lose the code-quality track.

Each task lists **acceptance** — the observable thing that has to be true. If you
cannot demonstrate the acceptance line, the task is not done, regardless of how
much code exists.

---

## T1 · Accounts and keys  ·  30 min  ·  blocks everything

- [ ] `npx @truefoundry/trueforge@latest` serving on `http://localhost:8790`
- [ ] A model provider added in TrueForge → Settings → Model providers
- [ ] Qodo GitHub App installed on this repo (`app.qodo.ai/signin` → Integrations →
      SaaS → GitHub). Use the 14-day trial; the free OSS plan needs 200+ stars
- [ ] `/config` commented on a PR to learn whether your install speaks v1
      (`/review`, `/improve`) or v2 (`/agentic_review`)
- [ ] `cp .env.example .env` and fill `BRIGHTDATA_API_KEY`
- [ ] Repo public, first PR open

**Acceptance:** a Qodo review comment appears on PR #1 without anyone asking for it,
and `curl localhost:8790/api/v1/models` returns your model list.

---

## T2 · Live loop against TrueForge  ·  90 min  ·  P0

The MCP server, the client and the orchestrator are written. This task is about
making a real session actually run.

- [ ] `make serve` and confirm `http://127.0.0.1:8931/mcp` responds
- [ ] `make run` — creates a session from `agent/agent.json` and drives the task
- [ ] Fix whatever the agent-spec schema rejects. `RESEARCH.md §1.5` has the shipped
      shape; if the custom MCP `url` field is refused, register the server under
      Settings → Connectors and reference it by `name` instead
- [ ] Confirm `task_brief` → `repo_read` → `propose_patch` round-trips, and that a
      red verdict comes back to the model as its next observation

**Acceptance:** one honest patch accepted end to end, with `.ratchet/<run>.bus.jsonl`
containing `agent.text`, `agent.tool`, `attempt.submitted`, `gate.result` and
`verdict` events. Run `ratchet audit --receipts .ratchet/<run>.receipts.jsonl` and
get "chain intact".

---

## T3 · The approval gate, live  ·  45 min  ·  P0

- [ ] Drive the agent to a fully green state so it calls `open_pull_request`
- [ ] Confirm TrueForge emits `tool.approval_required` and the turn ends with
      `state.required_actions`
- [ ] Approve from the console (`a`) and confirm the turn resumes and the PR opens
- [ ] Deny once, and confirm the agent receives the denial reason as an observation
      and keeps working rather than crashing

**Acceptance:** both paths demonstrated. The denial path matters more than the
approval path — it is the difference between a gate and a speed bump.

---

## T4 · Docker grading backend  ·  45 min  ·  P0

- [ ] `make image`
- [ ] `RATCHET_BACKEND=docker make test` passes
- [ ] Confirm `--network=none` really is applied during grading (add a task whose
      test tries to open a socket and watch it fail)

**Acceptance:** the same 27 tests pass through the container path, and the console
header shows `docker` rather than `local`.

---

## T5 · Console polish  ·  90 min  ·  P1, Best UI

Needs no model at all — it renders off the bus file.

- [ ] `make fixture && make console`
- [ ] Check every state renders: accepted, rejected, disqualified, stall, fan-out,
      arbitration scoreboard, docs repair, approval bar
- [ ] Resize to a narrow terminal and to a projector-sized one
- [ ] Make sure the approval bar is legible from across a room — this is the widget
      judges will actually remember
- [ ] Add the receipt chain head to the header, so "this run is verifiable" is visible

**Acceptance:** a stranger can watch a replay and answer, unprompted, what the agent
is doing, what it is waiting on, and what it did.

---

## T6 · Docs oracle against a real page  ·  60 min  ·  P1, Bright Data

- [ ] Pick one dependency this repo actually uses; add its source to
      `src/ratchet/scrapers.yaml` with `url`, `extract.section` and an `expect` block
- [ ] Run a lookup and confirm real markdown comes back through the CLI or the Web
      Unlocker path
- [ ] **Break it on purpose**: change `extract.section` to a heading that no longer
      exists, run the lookup again, and watch the oracle relocate the section, rewrite
      the YAML and append to `history`
- [ ] Commit the resulting diff — that diff *is* the Bright Data evidence
- [ ] Optional: create a Scraper Studio collector and set `collector_id` so the
      escalation path calls `bdata scraper heal`

**Acceptance:** `git diff` on `scrapers.yaml` shows the repair, with a timestamp and
the reason, and the bus contains a `docs.heal` event.

---

## T7 · Freeze, demo, submit  ·  75 min  ·  P0

- [ ] **16:45: feature freeze.** No new features, whatever is unfinished
- [ ] Run the full demo three times start to finish (`DEMO.md`)
- [ ] Record the ~3 minute video following the beats in `DEMO.md`
- [ ] Fill `SUBMISSION.md` and paste the TrueForge write-up into the README
- [ ] Fill the Qodo evidence table with real merged PR links
- [ ] Submit. Then write the blog post from `BLOG.md`

**Acceptance:** submitted with fifteen minutes to spare.

---

## Stretch, in value order

- [ ] **S1 · Dogfood.** Seed a bug in `parse.py`, point Ratchet at its own repo, let
      it open the PR, let Qodo review the agent's patch. Makes "the harness kept
      building itself" literal. Highest storytelling value of anything left.
- [ ] **S2 · Normalised majority vote** across candidates: `ast.parse` → strip
      docstrings → `ast.unparse` → vote on the canonical diff. About twenty lines,
      and it makes `arbitrate` cite agreement as well as score.
- [ ] **S3 · Reproduction test first.** Have the agent write a failing test, and have
      the verifier confirm it fails *before* the patch. High-precision signal.
- [ ] **S4 · `ratchet replay --speed 4` into the TUI** rather than stdout, so a dead
      live run can be replayed at the judging table.
- [ ] **S5 · Second real task** on a different repo, to show the harness is not
      wired to one demo.
- [ ] **S6 · Impossible-task escape hatch.** Give the agent a `flag_as_impossible`
      tool. Published work shows this cuts cheating from 54% to 9%, and the canary
      then measures whether it uses it. A genuinely novel thing to demo.

---

## Definition of done for any PR here

1. `make test` green, `make lint` clean.
2. `make redteam` still prints 10/10 and zero false positives.
3. A Qodo review on the PR, with its findings either fixed or answered in a comment.
4. No new tool that can end a run, and no new path that can set ACCEPTED outside
   `gauntlet/score.py::decide`.
5. If it touched the verifier, a test with a patch that trips the new rule *and* a
   patch that must not.
