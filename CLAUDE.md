# CLAUDE.md — project rules for Ratchet

Read this before writing code in this repository. It is the contract, not a summary.

---

## What this project is

Ratchet is a coding-agent harness in which the agent **cannot decide it is done**.
A verifier decides. Every step the agent takes is a git commit on a scratch branch;
every proposed patch runs a gauntlet (integrity → apply → build → fail-to-pass →
held-out → pass-to-pass → types → lint) before it is allowed to stick; anything that
regresses is rolled back to the last green commit and handed back as the next
observation; three consecutive stalls trigger a fan-out across parallel branches,
scored by the same verifier.

It runs on **TrueForge**, which owns the agent loop, MCP tool dispatch, the sandbox,
sub-agent threads, session persistence and the approval interrupt. Ratchet owns one
thing only: **what counts as progress**.

---

## Invariants — do not break these, ever

1. **Only the pawl produces a green verdict.** No code path may set
   `Decision.ACCEPTED` outside `gauntlet/score.py::decide`. If you find yourself
   wanting a `force_accept`, you have misunderstood the project.
2. **There is no `done` tool.** Never add one. The agent's belief about its own
   success is not an input to anything.
3. **Protected paths are reverted before grading.** `eval_script.build_eval_script`
   does this with `git checkout <base> -- <protected>`. Do not add a flag that skips it.
4. **The exit code is echoed outside the parsed region.** Anything inside the
   `>>>>> Start/End Test Output` markers is agent-influenced and therefore untrusted.
5. **Held-out tests never appear in any string the agent can read.** Check
   `task_brief`, every `Verdict.to_observation()` path, and every bus event that
   reaches a prompt. `f2p_hidden` names leaking into an observation silently destroys
   the entire signal.
6. **Grading runs with `--network=none`.** If a gate needs the network, it is not a
   grading gate.
7. **Rejected work is parked before rollback** at `refs/ratchet/rejected/<attempt>`.
   Never `reset --hard` without parking first.
8. **`open_pull_request` is the only tool that leaves the machine**, and it stays in
   `require_approval_for_tools`. If you add another irreversible tool, add it there in
   the same commit.
9. **Every verdict is receipted.** `receipts.record` runs on the accept path and the
   reject path alike. A verdict that skips the receipt book is a hole in the chain,
   and the chain is the only reason anyone should believe the run history.
10. **`make redteam` stays at 10/10 with zero false positives.** It is a test of the
   verifier, and it is in CI. If your change breaks it, your change is the problem —
   unless you can show the attack was mis-specified, in which case fix the attack and
   say so in the PR.

## Conventions

- Python 3.11+, `from __future__ import annotations`, dataclasses over dicts at
  boundaries, `ruff` + `mypy` clean before you open a PR.
- Pure functions in `gauntlet/` (`parse`, `grade`, `patchlint`, `score`) take data
  and return data. No I/O, no subprocess, no network. That is what makes them
  testable and it is why the demo can run with the Wi-Fi off.
- Subprocess calls use argv lists, never `shell=True`, never f-stringed user input.
- Comments explain *why*, especially where the code looks paranoid. Most of the
  paranoia is load-bearing and a future reader will delete it otherwise.
- New verification rules need a test with a real patch that trips them and a real
  patch that does not.

## Commands

```bash
make dev            # editable install + dev deps
make test           # pytest; runs with LOCAL backend, no docker, no network
make lint           # ruff + mypy
make demo           # seed demo-repo/ with the broken slugify and three patches
make serve          # ratchet MCP server on :8931
make console        # the TUI
ratchet verify --task tasks/demo-001-slugify/task.yaml --repo demo-repo \
               --diff demo-repo/patches/cheat.diff      # grade a patch, no model
```

---

## Bright Data — the data pipeline lives in the repo, not beside it

The docs oracle keeps the agent honest about the outside world: when a test fails
with an import error, a missing attribute or an unexpected keyword argument, the
orchestrator attaches the **current** upstream documentation for the **exact version
pinned in the lockfile** to the failure observation, instead of letting the model
guess from memory.

**Configuration lives in `src/ratchet/scrapers.yaml` and is version controlled.**
Never scrape with a one-off command in a shell; add or edit a source there so the
change is reviewable and the repair history is auditable.

Setup:

```bash
npx -p @brightdata/cli brightdata login          # or: export BRIGHTDATA_API_KEY=...
npx -p @brightdata/cli brightdata add mcp --agent claude-code --global
npx -p @brightdata/cli brightdata skill add scraper-studio
```

Rules of the pipeline:

- **Fetch order is CLI → Web Unlocker REST.** `brightdata scrape <url> -f markdown`
  first; on failure, `POST https://api.brightdata.com/request` with
  `{"zone": "$BRIGHTDATA_UNLOCKER_ZONE", "url": ..., "format": "raw", "data_format": "markdown"}`.
  Two surfaces, one config.
- **Extract by heading, not by CSS selector.** Headings survive redesigns; class
  names do not. This is the single cheapest source of scraper resilience available.
- **Every fetch is validated** against the source's `expect` block: `min_chars`,
  `must_contain`, and the blocked-page markers in `must_not_contain`. An unvalidated
  fetch is not a fetch, it is a guess.
- **Repair is a diff.** When validation fails, the oracle relocates the section by
  heading similarity, writes the new selector back into `scrapers.yaml`, and appends
  to that source's `history` with a timestamp and the reason. Commit that file.
- **Escalate to Bright Data's own self-healing** when a source has a `collector_id`:
  `bdata scraper heal <id> "<what broke>"`. Leave `auto_approve: false` unless you
  want repairs landing unreviewed — the approval gate showing a `diff_summary` is a
  feature, and it is the same gate the pull request uses.
- **Cache** to `.ratchet/docs-cache/`, keyed on `sha256(url + extractor)`, 24h TTL.
  Never commit the cache.

To prove the pipeline repairs itself, point a source at a page whose structure has
changed (or edit `scrapers.yaml` to name a section that no longer exists) and run a
lookup. You should see a `docs.heal` event, a rewritten `section:` in the YAML, and a
new `history` entry. That is the demo.

---

## Qodo — every change goes through a reviewed pull request

The submission requires a Qodo review trail, and this project's entire thesis is
that unverified agent output should not merge. Direct pushes to `main` contradict
the pitch.

1. Branch. `git switch -c feat/<thing>`.
2. Open a PR. The Qodo GitHub App reviews it automatically; if it does not, comment
   `/agentic_review` (hosted v2) or `/review` (v1/OSS).
3. **Deal with what it finds before merging.** Use the *Apply this suggestion*
   button for at least the ones you agree with, so the fix is attributable in the
   commit trail.
4. Configuration lives in `.pr_agent.toml` and `best_practices.md` at the repo root.
   Both are committed on purpose: a reviewer that is configured deliberately beats a
   reviewer that is running on defaults.
5. Record merged PR links under `## Qodo Code Review Evidence` in `README.md` as you
   go. Doing it at 17:55 is how teams lose that prize.

Run `/config` in a PR comment once at the start to confirm whether the installation
speaks v1 or v2 command names.

---

## TrueForge — what the harness owns

Do not reimplement any of this in Ratchet:

| Concern | Owner |
|---|---|
| model calls, retries, streaming | TrueForge |
| context management and compaction | TrueForge |
| MCP connections, OAuth, tool dispatch | TrueForge |
| sandboxed execution | TrueForge |
| sub-agent threads and parallelism | TrueForge (`create_sub_agent`, 5 at a time) |
| session persistence, resume after reconnect | TrueForge |
| the approval interrupt | TrueForge (`tool.approval_required`) |
| **what counts as progress** | **Ratchet** |

Wire facts that cost hours if you learn them the hard way:

- SSE frames carry **no `event:` name**. Dispatch on `json.loads(data)["type"]`.
- The SSE `id` is a monotonic per-turn sequence number. Persist it; reconnect with
  `?after_sequence_number=N`.
- A finished turn's live stream is garbage-collected minutes later; reconnecting then
  returns **412**. Hydrate from `/turns/{id}/events` first, then attach.
- Approvals are **not** a callback. The turn ends with `state.required_actions`, and
  you resume by starting a *new* turn whose input items are `user.tool_approval`.
  **Never mix approval items with a user message in one turn.**
- `thread_id` is `"main"` for the root agent, a unique id per sub-agent, `null` for
  run-level events.
- Local mode: SQLite, no auth, `npx @truefoundry/trueforge@latest`, Node >= 22.14.

---

## When you are stuck

Prefer, in order: read the failing test output; run `ratchet verify` on the patch by
hand with no model in the loop; add a unit test that reproduces the failure in
`tests/`; only then change the verifier. Changing the verifier to make a patch pass
is exactly the behaviour this project exists to catch, and it is just as wrong when
a human does it.
