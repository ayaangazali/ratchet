# Ratchet — the build plan

Everything decided, everything to build, in the order to build it. Written to be
handed to Claude Code and executed.

---

## 0. The one-liner, and why it wins

> A coding agent that cannot decide it is done. Every step is a git commit, every
> patch runs a verifier gauntlet, anything that regresses is rolled back to the last
> green commit and fed back as the next observation, and the only action that leaves
> the machine waits for a human.

The brief asks for an agent that can **reach your tools**, **run its own code
safely**, and **be stopped before it does damage**. Most submissions will do the
third with a prompt. This does it with a `git reset --hard` the model cannot argue
with. That is the whole pitch and every design decision below serves it.

---

## 1. Confirmed facts about the event

Verified from the hackathon pages on 29 Aug 2026:

- Online submission deadline: **Aug 30, 8:00 PM London** (= 12:00 PDT Aug 30).
  The in-person agenda you were given says submissions at **18:00 local**. These are
  not the same clock — **confirm which one binds you** and work to the earlier.
- Team size 1–4. Repo must be public. Deliverables: repo, README with setup, ~3 min
  demo video, a write-up on how it uses TrueForge, and a
  `## Qodo Code Review Evidence` section in the README linking merged PRs.
- Every submission is considered for all tracks; a team wins at most one.
- **Best Code Quality is judged on the Qodo review trail every submission carries.**
  Direct pushes to `main` do not count. This is the cheapest prize to lose by
  accident and the cheapest to secure: branch, PR, review, apply, merge, log it.
- The official online track list is Best Use of TrueForge / Best Code Quality / Best
  UI plus a blog prize; the in-person sheet you have adds a Bright Data track. Build
  for both lists — the Bright Data work is load-bearing here anyway, not bolted on.

### Tool facts worth knowing before you start

**TrueForge** — `github.com/truefoundry/trueforge`, MIT, TypeScript. `npx
@truefoundry/trueforge@latest` gives you local mode on `:8790` with SQLite and no
login; Node ≥ 22.14 required. No TrueFoundry cloud account needed.

- **Transport is SSE, and frames carry no `event:` name.** Dispatch on
  `json.loads(data)["type"]`. The SSE `id` is a monotonic per-turn sequence number;
  reconnect with `?after_sequence_number=N`. A finished turn's stream is collected
  ~5 minutes later and returns **412** after that — hydrate from
  `/turns/{id}/events` (durable) then attach.
- **Approvals are a pause/resume, not a callback.** Declared per MCP server via
  `require_approval_for_tools` (`@write`, `@destructive`, `@all`, or literal tool
  names). The turn ends with `state.required_actions`; you resume by creating a
  *new* turn whose input items are `user.tool_approval`. **Approval items and user
  messages may not be mixed in one turn.**
- **Sub-agents** are the built-in `create_sub_agent` tool (`{name, input}`).
  Multiple calls in one assistant message run under `Promise.all`; the orchestrator
  runs up to **5** in parallel. Results return as a single `tool.response` carrying
  only the sub-agent's final message. Sub-agents **share the parent's sandbox** and
  cannot see the parent conversation — restate everything.
- **The sandbox** exposes exactly one tool, `exec`, and persists across turns within
  a session. Pre-installed: Python 3.13, git, curl, jq, ripgrep. `pip install`
  persists. Local sandbox egress is allowlisted to PyPI + GitHub only.
- **There is no built-in web search.** It comes from MCP connectors; the shipped
  catalog includes `bright-data`, `exa`, `tavily`, `github`, `deepwiki` and others.
- Context: `compaction_threshold_tokens` (default 50000) — the docs show a different
  key shape than the shipped schema; trust the schema. `iteration_limit` default 100,
  server execution timeout 600s.
- **No API-key auth in local mode.** Fine on localhost; a trap if you add OIDC.

**Qodo** — hosted GitHub App via `app.qodo.ai/signin` → Integrations → GitHub, ~5
minutes. The free open-source plan needs 200+ stars, so a fresh hackathon repo does
**not** qualify — use the 14-day trial. Hosted v2 responds to `/agentic_review`,
`/agentic_describe`, `/ask`, `/config`; v1/OSS speaks `/review`, `/improve`,
`/implement`. Run `/config` in a PR comment once to find out which you have. Commit
`.pr_agent.toml` and `best_practices.md`. Land at least one commit from the *Apply
this suggestion* button so Qodo's contribution is visible in the commit trail.

**Bright Data** — free tier is 5,000 requests/month, no card. CLI:
`npx -p @brightdata/cli brightdata login`, then `brightdata add mcp --agent
claude-code --global` and `brightdata skill add scraper-studio`. Scraper Studio has a
**first-party self-healing feature** with an approval gate (`bdata scraper heal <id>
"<what broke>"`, showing `preview_result` and `diff_summary`) — use it rather than
claiming to have invented self-repair. For docs and changelogs the right surface is
Web Unlocker with `data_format: "markdown"` (`POST https://api.brightdata.com/request`),
plus SERP to find the page. There is no official `CLAUDE.md` template; the official
mechanism is their skills repo, and putting the config in `scrapers.yaml` +
`CLAUDE.md` is the version-controlled answer the track asks for.

---

## 2. Architecture, and the one decision that matters

The tempting design is: let the agent run shell in a sandbox, and check its work
afterwards. **Do not.** If the agent has a shell in the graded tree, the verifier is
advisory and every anti-tamper control becomes a suggestion.

The design here inverts that:

```
TrueForge  (owns the loop, context, MCP dispatch, sandbox, sub-agents, approvals)
    │  tool calls                                    ▲ tool.approval_required
    ▼                                                │
Ratchet MCP server  — the only door into the graded tree
    ├── repo_read / repo_grep / repo_tree   unrestricted reads
    ├── dry_run                             grade without consequence
    ├── propose_patch  ──►  THE PAWL  ──►  commit (green) | rollback (red)
    ├── docs_lookup    ──►  Bright Data, pinned to the lockfile
    ├── fan_out / arbitrate                 parallel branches, scored not voted
    └── open_pull_request                   ← human-gated, the only egress
```

Consequences, all good:

- The agent still gets the TrueForge sandbox for scratch work — reproduce the bug,
  prototype, run throwaway scripts — and none of it is graded, so it is free to be
  messy there. That is a *better* story than denying it a shell.
- The graded worktree is on the host, so the pawl can revert test files, run with
  `--network=none`, and commit or roll back atomically.
- Everything the agent can do is an MCP tool, which means TrueForge's approval
  machinery, sub-agent threading and context management all apply for free.
- Candidate branches key off an explicit `branch` argument the parent assigns, so
  fan-out needs no undocumented thread metadata.

**There is no `done` tool.** This is the design, stated as an absence.

---

## 3. Feature list

### P0 — without these there is no submission (build first, in this order)

| # | Feature | Notes |
|---|---------|-------|
| 1 | `TaskSpec` with `f2p_visible` / `f2p_hidden` / `p2p` / `protected_paths` | the held-out split is the differentiator; get it right first |
| 2 | Eval script: apply → `git checkout base -- tests/` → re-apply pristine tests → markers → exit code outside the markers | ~15 lines of bash, the highest-leverage code in the repo |
| 3 | Log parser + `suite_ran` sentinel + exit-code cross-check | empty log must never grade as a pass |
| 4 | Grader with the SWE-bench skip asymmetry | skipped F2P ≠ pass; skipped P2P ≠ regression; missing = failure |
| 5 | `patchlint` cheat detector | path lint, deleted tests, skip markers, `sys.exit(0)`, always-`__eq__`, weakened assertions, special-casing via literal overlap |
| 6 | Verifier score with the `visible − hidden` gap penalty | makes it a verifier, not a test runner |
| 7 | Git ledger: commit per accepted step, park rejected at `refs/ratchet/rejected/*`, `reset --hard` to last green | rollback + audit trail + time travel, free |
| 8 | Ratchet MCP server over streamable-http | the whole tool surface |
| 9 | TrueForge session + SSE pump + observation feedback | the loop |
| 10 | Approval gate on `open_pull_request` | `require_approval_for_tools`, resumed with `user.tool_approval` |
| 11 | Demo repo + honest/cheat/canary patches + `ratchet verify` CLI | the demo must work with no model and no network |
| 12 | Qodo installed, `.pr_agent.toml` committed, first PR reviewed | do this at hour 1, not hour 7 |

### P1 — these win tracks (build second)

| # | Feature | Track |
|---|---------|-------|
| 13 | The TUI: stream + gate rail + ratchet spine + scoreboard + full-width approval bar | Best UI |
| 14 | Stall detection → `fan_out` → `create_sub_agent` → `arbitrate` | Best Use of TrueForge |
| 15 | Docs oracle: lockfile-pinned fetch, schema validation, heading-based repair, `scrapers.yaml` in git | Bright Data |
| 16 | Failure-triggered docs: import/attribute/kwarg errors auto-attach current docs to the observation | Bright Data — proves the data is *used*, not decorative |
| 17 | The canary task | the demo moment nobody else has |
| 18 | Self-repair demo: break `scrapers.yaml` on purpose, show the `docs.heal` event and the resulting git diff | Bright Data judging criterion #5 |
| 19 | Sub-agent threads rendered inline in the TUI with their own thread ids | Best UI + Best Use of TrueForge in one widget |
| 20 | Qodo *Apply this suggestion* commit landed and linked in the README table | Best Code Quality |

### P2 — only if you are ahead (and each is a genuine upgrade, not filler)

| # | Feature | Why |
|---|---------|-----|
| 21 | Dogfood run: point Ratchet at its own repo with a seeded bug; the PR it opens is reviewed by Qodo | "the harness kept building itself" stops being a tagline |
| 22 | AST-normalised majority vote across candidates (strip docstrings, `ast.unparse`, vote on canonical diffs) | Agentless's ranking signal, ~20 lines |
| 23 | Reproduction-test generation: agent writes a failing test first, verifier confirms it fails pre-patch | high-precision signal, and it demos beautifully |
| 24 | Per-candidate container (not just worktree) with a hard memory cap | stronger isolation story |
| 25 | `ratchet replay <run>` — re-render a finished run from the bus file | judges love a replay; also saves you if the live run dies |
| 26 | LLM integrity judge as a *score term only*, never a gate | rubric axis from the agentic-rubrics work |
| 27 | Coverage-greedy P2P subset selection for speed | only if the suite is slow enough to matter |

### Explicitly not building

Learned trajectory verifiers, MCTS, a custom model, a web UI, auth, multi-repo
support, a plugin system. Every one of these is a day of work that no judge will see.

---

## 4. Schedule (the 09:00–18:00 in-person day)

Times assume two people. Solo: cut P2 entirely and start the video at 15:30.

| Time | Work | Done means |
|------|------|-----------|
| 09:00–10:00 | Breakfast, accounts: TrueForge running locally, Qodo app installed on the repo, Bright Data key in `.env`, repo created **public** with the first empty PR open | three green checkmarks, `npx @truefoundry/trueforge` serving on :8790 |
| 10:00–11:00 | Workshops. One person listens for approval + sub-agent details; the other starts P0 #1–#4 | `parse`/`grade` unit tests passing |
| 11:00–12:00 | P0 #2–#7: eval script, patchlint, score, ledger | `ratchet verify` disqualifies the cheat patch with no model in the loop |
| 12:00–13:00 | P0 #8–#10: MCP server, TrueForge session, first real agent loop | one honest patch accepted end to end |
| 13:00–14:00 | P0 #11–#12 + **first Qodo review dealt with**; merge PR #1 | README has a real row in the Qodo table |
| 14:00–15:00 | Pizza; P1 #13 the TUI (this is a whole person's afternoon — assign it now) | gate rail and spine render off the bus file |
| 15:00–16:00 | P1 #14 fan-out + #17 canary | stall → three sub-agents → scoreboard, on screen |
| 16:00–16:45 | P1 #15/#16/#18 docs oracle and the break-it-on-purpose demo | `docs.heal` event + `scrapers.yaml` diff |
| 16:45–17:15 | **Freeze.** No new features. Run the demo three times start to finish | it works three times |
| 17:15–17:45 | Record the video, write the README write-up, fill the Qodo table | video uploaded |
| 17:45–18:00 | Submit. Then, and only then, write the blog post | submitted with 15 minutes spare |

**Hard rules.** Feature freeze at 16:45 regardless of what is unfinished. The video
gets recorded even if a feature is missing — an unrecorded demo scores zero. Merge a
PR every 90 minutes so the Qodo trail is real rather than retrofitted.

---

## 5. Judging map — what to point at, per track

**Best Use of TrueForge** ("the harness is doing the work rather than sitting under a
thin wrapper"). Show, in this order: `agent/agent.json` (MCP servers with per-tool
approval policy, sandbox on, sub-agents on, compaction configured) → a live
`tool.approval_required` pause → three sub-agent threads with distinct `thread_id`s
in the stream → the bus file proving every one of those events came off TrueForge's
SSE stream rather than being simulated. Then the argument: *we deleted the loop, the
context manager, the OAuth dance and the approval interrupt from our codebase, and
spent the day on the only thing the harness cannot know — what counts as progress.*

**Best Code Quality.** The Qodo table in the README with merged PR links, at least
one commit authored by *Apply this suggestion*, `.pr_agent.toml` with real
`extra_instructions` (not defaults), `best_practices.md`, CI running ruff + mypy +
pytest on every PR, and 22 tests that run with no network. Say the line: *we ship a
tool whose thesis is that unverified agent output shouldn't merge, so nothing merges
here unreviewed either.*

**Best UI** ("an interface a stranger could pick up and drive; shows what the agent
is doing, what it is waiting on, what it did, and asks before the irreversible step
rather than after"). Map it literally: the stream, the gate rail, the spine, and an
approval bar that takes the full width and blocks. Mention the crash path — an
approval can still be granted with `echo '{"allow":true}' > .ratchet/approvals/<id>.json`.

**Bright Data.** Config in `scrapers.yaml`, in git, referenced from `CLAUDE.md`;
lockfile-pinned versions so the fetch is specific rather than generic; validation on
every fetch; break the extractor live and show the repair as a diff plus a `history`
entry; escalation to `bdata scraper heal` for collector-backed sources with the
approval gate wired to the same human gate as the PR. Close with the criterion they
published: the data is fresh, structured, and actually used — it lands in the
failure observation the agent reads next.

**Blog post.** Title: *"The agent doesn't get a vote."* Structure: the reward-hacking
evidence (ImpossibleBench ~50%, Anthropic's production RL findings) → the design
inversion (verifier outside the agent) → the fifteen lines of bash that do most of
the work → the canary and why it has zero false positives → what TrueForge gave us
for free → what broke on the day. Publish the same evening; the writing is easier
while it still hurts.

---

## 6. Risk register

| Risk | Likelihood | Mitigation, already built |
|---|---|---|
| Venue Wi-Fi or model API dies | high | `ratchet verify` demos the entire verification story offline; `Backend.LOCAL` needs no Docker |
| Docker Desktop not working on the demo laptop | medium | automatic fallback to `Backend.LOCAL`, labelled as less safe in the UI |
| TrueForge rejects our custom MCP server URL in the agent spec | medium | register it in Settings → Connectors and reference by name instead; keep both shapes in `agent/agent.json` comments |
| SSE stream drops mid-demo | medium | hydrate from `/turns/{id}/events`, then re-attach with `after_sequence_number` |
| The model refuses to cheat, so the DQ demo never fires naturally | **high** | never rely on it: `patches/cheat.diff` and `patches/canary_hack.diff` are pre-built and fed deliberately |
| Held-out test names leak into a prompt | medium | invariant #5 in `CLAUDE.md`; grep for `f2p_hidden` in every observation path before freeze |
| Qodo trial not active / OSS plan rejected (needs 200+ stars) | medium | use the 14-day trial, set it up in hour 1, verify a review appears on PR #1 |
| Fan-out eats the whole time budget | medium | cap at 3 candidates, `MAX_PARALLEL_SUB_AGENTS` is 5 anyway; arbitrate on a timer |
| Run takes longer than the video | high | record the beats separately and cut; the spine makes non-linear editing legible |

---

## 7. Handing this to Claude Code

The repository already contains the working core. Verify first, then extend:

```bash
make dev && make demo && make test     # 22 tests, ~10s, no network
```

Suggested task order, one PR each so the Qodo trail builds itself:

1. **Wire the live loop.** `make serve`, `make run`, confirm `task_brief` and
   `propose_patch` round-trip against a real TrueForge session. Fix whatever the
   agent-spec schema rejects; keep `agent/agent.json` in sync.
2. **Docker task image.** `make image`, then flip `RATCHET_BACKEND=docker` and make
   the end-to-end tests pass through the container path.
3. **TUI polish.** Run `make console` against a recorded bus file first; the widgets
   are already driven entirely by the bus, so this needs no model.
4. **Docs oracle against a real page.** Pick one dependency you actually use, set its
   source in `scrapers.yaml`, run a lookup, then break the `section:` on purpose and
   watch it repair. Commit the resulting diff — it is the Bright Data evidence.
5. **Dogfood (P2 #21).** Seed a bug in `parse.py`, point Ratchet at its own repo, let
   it open the PR, and let Qodo review the agent's own patch.

Every one of these is independently demoable, so stopping after any of them still
leaves a submission.
