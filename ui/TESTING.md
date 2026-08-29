# UI test log — 2026-08-29

Method: every scenario driven through the real browser (Playwright), against the real
gateway (`ui/gateway.py`) spawning real `ratchet run --scripted` subprocesses. After each
scenario the console log and the network tab were read, and the verifier's decision was
judged on its merits before marking pass/fail. No mocked pipeline data anywhere; the Qodo
panel is live GitHub data.

## Scenarios

| # | scenario | expected | observed | judgement |
|---|----------|----------|----------|-----------|
| 1 | honest fix (chip) | 1 attempt, green, gate, approve → result | green score 1.00, real diff in gate, receipts "chain intact", approved | **pass** — verifier went green because the hidden suite actually passed |
| 2 | explore first | bad attempt graded, then green | attempt 1 `regressed 0.58 · ✂ 3 previously-passing test(s) now fail`, attempt 2 green | **pass** — regression detection is the right reason to reject dropping separators |
| 3 | cheating patch | cheat caught, pruned, then green | pruned `cheated 0.00` with findings `protected_path, skip_marker, special_casing`; reason names the graded-test edit | **pass** — all three cheat vectors in the patch were the ones flagged |
| 4 | canary hack | special-casing flagged | `progress 0.58` with `special_casing` finding; not green (hidden tests still fail); honest attempt wins after | **pass, with note** — the finding is non-critical by design, and the gauntlet still refused green, which is the invariant that matters |
| 5 | exhaust budget | run ends, no green | 3 attempts regressed, scripted responses exhausted, core loop span; watchdog killed at 60s → honest "No green · run killed", gate "never reached" | **pass** — no fabricated success; core spin bug logged below |
| 6 | deny at the gate | decision recorded, nothing ships | result: verdict Green, gate "denied — denied in ui" | **pass** — green verdict and ship decision are correctly independent |
| 7 | free-typed prompt | default scenario runs | typed prompt → green → gate | **pass** |
| 8 | cap nodes budget | `--budget` reaches the CLI | result shows `3 nodes explored · 1/6 budget` | **pass** — UI budget flowed through create → CLI flag → budget line |
| 9 | Qodo data integrity | panel counts == GitHub | PR #9 panel "6 bugs · 4 rule violations" == `gh api` body `🐞 Bugs (6) 📘 Rule violations (4)`; PR #12 4/1 matches too | **pass** — live `qodo-code-review[bot]` comments, zero invented numbers |
| 10 | reload mid-run | stream replays, rows rebuild | fresh EventSource replayed the bus from offset 0; full pipeline + gate reappeared | **pass** |

## Console / network evidence

- Console: 0 errors across all scenarios. Only warnings are the two React Router v7
  future-flag notices (upstream, benign).
- Network per run: `POST /api/create` 200 → `GET /api/stream/<id>` 200 (SSE) →
  `POST /api/approve/<id>` 200 → `GET /api/result/<id>` 200, plus `GET /api/qodo` 200.
  Requests appear twice in dev because of React StrictMode double-mount; single in prod build.
- `npm run build` (tsc + vite) clean; `ruff check ui/gateway.py ui/make_scenarios.py` clean.

## Bugs found and fixed during the loop

1. **Every node graded `infra` ("no evidence the test suite executed").** The task's
   `test_cmd` invokes bare `python`; the gateway spawned the CLI without the venv on
   `PATH`, so the suite never ran. Fix: `RUN_ENV` prepends the venv bin.
2. **Runaway bus file (60.5 MB, 856k events in ~1 min).** When the scripted responses run
   out, the scheduler loops `expand → candidate.empty → stall` with no sleep and no
   dead-end accounting. Gateway-side mitigation: 60s watchdog kills the process and
   synthesizes an honest no-green result. **The core bug is in the ratchet scheduler and
   deserves its own fix + test** — an empty candidate should count as a dead end.
3. **Gateway re-read the whole bus every 150ms** — replaced with incremental byte reads.

## Scenario 11 — live Qodo round-trip (2026-08-29, second pass)

Question: is Qodo's AI actually reviewing alongside us, or are we just displaying
history? Proven live, with timestamps:

1. `gh pr comment 12 --body "/review"` posted at **23:04:44Z**.
2. `qodo-code-review[bot]` acknowledged ("Qodo is busy working") at **23:04:50Z** — 6s.
3. The bot **edited its review comment in place** — `created_at 21:26:30Z`,
   `updated_at 23:06:45Z` — a fresh review pass ~2 min after the trigger, with new
   category set (`requirement gaps`, `ux issues`) absent from the earlier pass.
4. Gateway fetch at 23:09:03Z picked it up (`at` now uses `updated_at` for this reason).
5. Browser console (the proof the integration runs in the UI):
   `[qodo] live feed: 13 PRs, 20 bot comments, fetched 2026-08-29T23:09:03.078Z`
   `[qodo] newest review: PR #12 at 2026-08-29T23:06:49Z`
   — and note the feed grew from 11 PRs/15 comments to 13/20 during the session:
   Qodo was reviewing other agents' fresh PRs while we watched.

**Qodo Command CLI is discontinued upstream** (verified 2026-08-29, post-login: the
server replies "Qodo Command has been discontinued. You can still get automated code
reviews by connecting your Git provider"). The hosted PR bot is Qodo's only living
review surface, so the integration targets it directly.

## Scenario 12 — commanding Qodo from inside the console

Each Qodo panel row has a ↻ button → `POST /api/qodo/rereview` → `gh pr comment
<n> --body "/review"`. Browser-tested on PR #12:

- console: `[qodo] fresh review commanded on PR #12:
  https://github.com/ayaangazali/ratchet/pull/12#issuecomment-5465460574`
- GitHub: `/review` comment at 23:19:45Z, bot ack "Qodo is busy working" at
  **23:19:52Z** — 7 seconds from UI click to Qodo responding.
- The gateway drops `qodo_cache.json` on trigger so the panel's next fetch shows the
  fresh pass. **pass**

## Scenario 13 — /qodo status page

`#/qodo` (linked from the topbar) lists every PR with Qodo's latest headline counts;
expanding a PR parses the bot's newest review comment into individual findings — title,
tags, description, and the **"what qodo told the agent"** block (Qodo embeds a
ready-to-use agent prompt per finding; the page surfaces it verbatim). Buttons:
re-review now (the `/review` trigger) and open on github.

Browser-verified on PR #12: 10 unique findings parsed, all 10 with agent prompts,
headline `8 bugs · 2 rule violations` matching the 23:22Z review pass. Console:
`[qodo] status page: 13 PRs loaded`, `[qodo] PR #12: 10 findings parsed`, 0 errors.
Parser fixes found while testing: the review body repeats finding summaries in a later
section (dedupe by title, first wins) and repeats `<code>N (x)</code>` counts deeper in
the body (headline = first match per category). Terminal twin: the `/qodo` skill at
`~/.claude/skills/qodo/SKILL.md` reports the same status via `gh`. **pass**

## Known limitations

- Qodo reviews shown are the bot's real findings on this repo's PRs — the "external
  reviewer alongside the agent" story. Per-run Qodo review of the winning diff would need
  a PR per demo run (quota) or a local pr-agent install (LLM key); deliberately skipped.
- `ui/qodo_cache.json` is committed as an offline fallback so the panel renders on venue
  Wi-Fi; it refreshes automatically every 10 min when GitHub is reachable.
