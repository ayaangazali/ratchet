# Research digest

Everything verified before the build, so nobody has to re-search it on the day.
Facts marked **[V]** were read from a primary source (the URL given); **[I]** is
inference, flagged so you can check it rather than trust it.

---

## 1. TrueForge

Repo `github.com/truefoundry/trueforge` · docs `https://trueforge.dev` ·
`https://trueforge.dev/llms.txt` lists every doc page, and each page also serves
as `.md`. MIT, **TypeScript**, Node ≥ 22.14. **[V]**

```bash
npx @truefoundry/trueforge@latest     # local mode: UI + API on :8790, SQLite, no login
```

npm packages (published 2026-08-19): `@truefoundry/trueforge@0.1.4` (server),
`@truefoundry/trueforge-core@0.1.4` (agent loop), `@truefoundry/trueforge-sdk@0.1.3`,
`@truefoundry/trueforge-ui@0.2.4`. **[V]**

> The `.js.map` files in those packages contain full original TypeScript
> `sourcesContent`. `npm pack @truefoundry/trueforge-core@0.1.4` and read the maps
> if you need the real agent loop rather than any summary. **[V]**

### 1.1 The event stream — this is the TUI contract

Transport is **SSE**, two endpoints, both `text/event-stream`: **[V]**

```
POST /api/v1/sessions/{sid}/turns                                   # stream:true (default)
GET  /api/v1/sessions/{sid}/turns/{tid}/subscribe?after_sequence_number=N
```

Frames are `id: <sequenceNumber>` + `data: <json>`. **There is no `event:` field** —
dispatch on the parsed `data["type"]`. **[V]**

Event types (from `trueforge-core/dist/core/events/schema.js`): **[V]**

| type | notes |
|---|---|
| `turn.created` / `turn.done` | `turn.done` carries `state.status` and `state.required_actions` |
| `model.message` / `model.message.delta` | `usage.input_tokens_breakdown` = `{harness, skills, instructions, tool_definitions, messages}` |
| `tool.response` | |
| `tool.approval_required` | `tool_calls: [{id, source_event_id}]` |
| `tool.response_required` | client-side tools |
| `thread.created` / `thread.done` | sub-agents; `agent_info`, `parent`, `title` |
| `sandbox.created` | session-scoped, `thread_id` null |
| `mcp.auth_required` | `mcp_servers[].auth_url` |
| `mcp.initialize` | `transport_type: streamable-http \| sse` |
| `agent.context.overwrite` | `reason: "compaction"` |
| input items | `user.message`, `user.tool_approval`, `user.tool_response` |

`thread_id` is `"main"` for the root agent, unique per sub-agent, `null` for
run-level events. **[V]**

**Reconnect.** `sequenceNumber` is monotonic per turn; `subscribe` replays events
strictly greater than `after_sequence_number`. `412` = the live stream is gone;
`403` = you are not `created_by`. Server caps: `TURN_SUBSCRIBE_TIMEOUT_MS=600000`,
`TURN_STREAM_TTL_SECONDS = SERVER_EXECUTION_TIMEOUT_SECONDS + 300`,
`TURN_STREAM_POST_COMPLETION_TTL_SECONDS=300` — i.e. **five minutes** to reattach
after a turn ends. The durable path is `GET /turns/{tid}/events`. **[V]**

### 1.2 Approvals — a pause/resume, not a callback

Declared per MCP server in the agent spec: **[V]**

```json
"require_approval_for_tools": ["@write", "@destructive"]
```

Selectors: `@all`, `@read-only` (for `enable_tools`), `@write`, `@destructive`, or
literal tool names. Flow: **[V]**

1. `tool.approval_required` arrives on the stream.
2. The turn ends with `turn.done`, `state.status === "done"`, and
   `state.required_actions: ActionRequiredEvent[]`.
3. You resume by creating a **new turn** whose input items are approvals:

```json
{"type":"user.tool_approval","thread_id":"main","tool_call_id":"...","approval":{"status":"allow"}}
```

**Do not mix user messages with approval or tool-response items in one turn.** **[V]**
`previous_turn_id` defaults to `"auto"`. MCP OAuth uses the same pause via
`mcp.auth_required`.

### 1.3 Sandbox

One tool, `exec`, MCP server id `sandbox`: **[V]**

```js
{ intent: string, command: string, cwd?: string, env?: Record<string,string> }
```

Description, verbatim: *"Execute shell commands in a persistent sandbox
environment. The sandbox persists across calls within the same session… Pre-installed:
Python 3.13, pydantic, git, curl, helm, jq, ripgrep, genson."* **[V]** `pytest` is
**not** pre-installed but `pip install` persists for the session.

Providers: the docs name **Daytona** only, but the shipped server also has an
undocumented **local** provider built on `@anthropic-ai/sandbox-runtime`
(bubblewrap on Linux, Seatbelt on macOS), enabled when `STANDALONE` is true —
which is the default under `npx`. Linux hosts need `bwrap`, `socat`, `rg` on PATH.
**Local sandbox egress is allowlisted to PyPI + GitHub only** (npm is not on the
list); Daytona has no such restriction. **[V]**

No host mount: a repo gets in by `git clone` inside the sandbox. Daytona defaults:
`exec_timeout_ms: 60000`, auto-stop 5 min. File out via
`GET /sessions/{sid}/turns/{tid}/download-sandbox-file`, 20 MB cap. **[V]**

### 1.4 Sub-agents

Built-in tool `create_sub_agent`, schema `{name, input}` (plus `model` when a model
set is configured). All tool calls in one assistant message execute under
`Promise.all`, and the orchestrator runs active threads in batches of
**`MAX_PARALLEL_SUB_AGENTS = 5`**. The parent receives only the sub-agent's final
assistant message as a `tool.response`. Sub-agents **share the parent's sandbox**
("Files created or modified by either agent are shared") and have **no access to
the prior conversation**. One level deep; they cannot ask the user questions. **[V]**

### 1.5 Agent spec (validated against the shipped zod schema)

```jsonc
{
  "name": "my-agent",
  "manifest": {
    "model": {"name": "anthropic/claude-sonnet-4-6",
              "params": {"temperature": 0.2, "max_tokens": 8192,
                         "parallel_tool_calls": true, "reasoning_effort": "medium"}},
    "instructions": "...",
    "mcp_servers": [{"name": "github", "enable_tools": ["@all"], "preload": false,
                     "require_approval_for_tools": ["@write", "@destructive"]}],
    "skills": [{"name": "..."}],                 // requires sandbox.enabled
    "config": {
      "iteration_limit": 100,                    // 1..1024
      "sandbox": {"enabled": false, "file_downloads": true},
      "dynamic_sub_agents": {"enabled": true},
      "context_management": {
        "compaction": {"enabled": true, "compaction_threshold_tokens": 50000},
        "large_tool_response": {"enabled": true}},
      "generative_ui": {"enabled": true},
      "ask_user_questions": {"enabled": true}
    }
  }
}
```

⚠️ The docs render compaction as `trigger: {type, value}`; the **shipped schema uses
`compaction_threshold_tokens`**. Trust the schema — if you get a 422, that is why. **[V]**

Other built-in tools: `exec`, `create_sub_agent`, `ask_user_question`
(`{question, options[0..5]}`), `get_current_datetime`, `get_openui_instructions`,
plus deferred-tool meta-tools `list_tools` / `get_tool_info` / `call_tool`. **[V]**

### 1.6 MCP catalog, models, storage

Shipped catalog (14 servers) includes, with no auth: **exa** (`https://mcp.exa.ai/mcp`),
**parallel-web**, **deepwiki**; with a header: **tavily**, **bright-data**
(`https://mcp.brightdata.com/mcp`), **github**; via OAuth DCR: linear, notion, sentry,
supabase, stripe, atlassian, posthog. Custom URLs are supported. **There is no
built-in web search tool** — it comes from a connector. Timeouts:
`MCP_REQUEST_TIMEOUT_MS=240000`, `MCP_CONNECT_TIMEOUT_MS=30000`. OAuth requires
`PUBLIC_BASE_URL` to be set. **[V]**

Model providers: `openai`, `anthropic`, `google-gemini`, `fireworks`, `zai`,
`moonshot`, `alibaba`, `together`, plus **`custom`** for any OpenAI-compatible
endpoint. Model FQN is `provider/name`; get the real list from `GET /api/v1/models`. **[V]**

Storage: local = SQLite (`better-sqlite3`), hosted = Postgres + Redis. Sessions,
turns and events are durable; **the SSE stream is not**. **[V]**

### 1.7 Gotchas that cost hours

1. **No API-key auth.** Standalone accepts requests with no credentials; auth is
   OIDC-only. Fine on localhost, a trap if you add OIDC. **[V]**
2. `403` on turn create/subscribe if you are not `created_by`. **[V]**
3. `412` on subscribe after the post-completion TTL — always keep the
   `listTurnEvents` fallback. **[V]**
4. SSE has no `event:` name. **[V]**
5. Approvals cannot be mixed with messages. **[V]**
6. Skills silently require a sandbox (422: *"skills require a sandbox provider"*). **[V]**
7. `iteration_limit` default 100 but `SERVER_EXECUTION_TIMEOUT_SECONDS` is 600 — a
   long loop hits the wall clock first. **[V]**
8. Fully local, no TrueFoundry account needed; you supply only model keys. **[V]**

### 1.8 Cookbook

`https://github.com/truefoundry/trueforge/tree/examples/agent-cookbook/examples` —
ten agents including `ci-fixer` (github + sandbox, "diagnose and patch a failing
pull request"), which is the closest template to a git + pytest agent. Raw files
fetch fine from `raw.githubusercontent.com`. **[V]**

---

## 2. The hackathon

- Deadline **Aug 30, 8:00 PM London**; solo or up to 4; repo must be public. **[V]**
- Deliverables: repo, README with setup, ~3-minute demo video, a write-up on how it
  uses TrueForge, and a `## Qodo Code Review Evidence` section linking merged PRs. **[V]**
- Load-bearing line from the rules: *"a judge has to be able to see the harness
  doing real work rather than sitting under a thin wrapper around a model call."* **[V]**
- *"Best Code Quality is judged on the Qodo review trail every submission carries."*
  Direct pushes to main do not count. **[V]**
- Every submission is considered for all tracks; a team wins at most one. **[V]**
- Resources: kickoff `https://www.wemakedevs.org/blogs/agent-harness-hackathon-kick-off`,
  rules `https://www.wemakedevs.org/hackathons/trueforge/rules`, workshop video
  `https://www.youtube.com/watch?v=bqgz6gOK5OA`. **[V]**
- The in-person agenda you were handed lists a Bright Data track and an 18:00
  submission time; the online rules list three tracks and 20:00 London. **Confirm
  which binds you and work to the earlier.** **[I]**

---

## 3. Qodo

- Hosted GitHub App: `app.qodo.ai/signin` → Integrations → SaaS → GitHub → Add
  installation → authorize → Finish. Documented as *"Approximately 5 minutes."* **[V]**
- **The free open-source plan requires 200+ stars**, so a fresh hackathon repo does
  not qualify — use the 14-day trial. **[V]**
- Commands differ by version. Hosted **v2**: `/agentic_review`, `/agentic_describe`,
  `/ask`, `/config`, `/generate_labels`, `/checks`. **v1 / OSS**: `/review`,
  `/improve`, `/implement`, `/describe`, `/add_docs`, `/test`, `/analyze`,
  `/compliance`, `/custom_prompt`, `/similar_code`. **Run `/config` in a PR comment
  once to find out which you have.** **[V]**
- Config file is `.pr_agent.toml` at the root of the default branch; `best_practices.md`
  at the root steers `/improve`. Precedence: Wiki > Local > Global > Org. **[V]**
- Evidence artefacts: prioritised findings, a *"possible security issue"* label, a
  *"Review effort [1-5]"* label, and the **"Apply this suggestion"** button which
  *"instantly converts a suggestion into a committable code change"* — set
  `commitable_code_suggestions = true`. **[V]**
- Repo-wide context: v1 uses `enable_rag = true` + `rag_repo_list`; v2's Context
  Engine considers *"the broader system surrounding a code change, not just the
  changed lines"* and appears automatic on the hosted app. **[V]**
- The OSS action lives at `the-pr-agent/pr-agent`, which self-describes as *not* the
  Qodo offering — using it may not count as "using Qodo" for judging. **[I]**

---

## 4. Bright Data

- Free tier **5,000 requests/month**, no card. `$50` credit link
  `brdta.com/wemakedevs`, promo code `wemakedevs` (lowercase) in Billing. **[V]**
- CLI (binary `brightdata`, alias `bdata`): **[V]**

```bash
curl -fsSL https://cli.brightdata.com/install.sh | sh     # or npm i -g @brightdata/cli
npx -p @brightdata/cli brightdata login
npx -p @brightdata/cli brightdata add mcp --agent claude-code --global
npx -p @brightdata/cli brightdata skill add scraper-studio
```

- Scraper Studio is three interfaces over one product — AI agent, browser IDE, and
  CLI — and *"All produce identical output regardless of the building method used."* **[V]**
- Scraper lifecycle: **[V]**

```bash
bdata scraper create <url> "<description>" [--name N] [--pretty] [-o f.json]
bdata scraper run <collector_id> --input-file urls.txt [--sync]
bdata scraper heal <collector_id> "<what's wrong>" [--url <verify>] [--auto-approve]
bdata scraper approve <collector_id> [--reject]
```

Documented rules: don't loop `run` over URLs (batch with `--input-file`/`--urls`);
`--sync` caps at 50 s server-side; heal prompts cap at 1000 chars; without
`--auto-approve` healing pauses at an approval gate exposing `preview_result` and
`diff_summary`. **[V]**

- **Self-healing is a first-party feature**, described as *"an AI-powered code
  refactor assistant… that rewrites parts of a scraper from a plain-language
  prompt"*, with manual (diff-approval) and Auto modes. Do not claim to have
  invented it — wire to it. **[V]**
- MCP server `@brightdata/mcp`; env `API_TOKEN` (required), `WEB_UNLOCKER_ZONE`
  (default `mcp_unlocker`), `BROWSER_ZONE`, `PRO_MODE` (default false),
  `RATE_LIMIT` (`100/1h`), `GROUPS`, `TOOLS`. 69 tools total; always-on:
  `search_engine`, `search_engine_batch`, `scrape_as_markdown`, `scrape_batch`,
  `discover`. `GROUPS`/`TOOLS` take precedence over `PRO_MODE`. **[V]**
- For docs and changelogs the right surface is **Web Unlocker with markdown**: **[V]**

```bash
curl -H "Authorization: Bearer $BRIGHTDATA_API_KEY" -H "Content-Type: application/json" \
  -d '{"zone":"'"$BRIGHTDATA_UNLOCKER_ZONE"'","url":"https://…","format":"raw","data_format":"markdown"}' \
  https://api.brightdata.com/request
```

Async variant: `POST /unblocker/req` → `GET /unblocker/get_result`. The dataset
Web Scraper API (`/datasets/v3/trigger` → `/progress/{id}` → `/snapshot/{id}`) is
keyed to pre-built platform schemas, **not** arbitrary docs sites. **[V]**

- There is **no official `CLAUDE.md` template**; the official mechanism is
  `github.com/brightdata/skills` (21 skills) plus the copy-paste templates at
  `docs.brightdata.com/datasets/scraper-studio/coding-agent-prompts`. Putting the
  config in a version-controlled `scrapers.yaml` referenced from `CLAUDE.md` is the
  answer the track's criteria ask for. **[I]**

---

## 5. Prior art the verifier design borrows from

**SWE-bench harness** (`swebench/harness/`): **[V]**

- `FAIL_TO_PASS` / `PASS_TO_PASS` are derived empirically — run the suite at the
  base commit with the test patch, then again with the gold patch, and diff the
  status maps.
- Grading: `f2p == 1 and p2p == 1` → `RESOLVED_FULL`. The skip asymmetry is
  explicit in the source: a skipped F2P test is **not** a resolution (otherwise
  `pytest.mark.skip` is a free win), a skipped P2P test is **not** a regression, and
  a test missing from the map counts as a failure.
- The eval script applies the patch, then `git checkout <base> -- <test files>`,
  then re-applies the pristine test patch. **This is the whole anti-tamper story in
  two lines of bash.**
- Post-issue-#620 hardening: record the *test command's* exit code outside the
  parsed region and refuse a log that claims a clean sweep while the runner exited
  non-zero; plus a `SUITE_RAN` sentinel so an empty log is not a pass.
- Apply chain: `git apply -v` → `git apply -v --3way` → `patch --fuzz=5`, with
  `git checkout -- . ; git clean -fd` between attempts.
- Artefacts per instance: `report.json`, `run.json`, `test_output.txt`, `eval.sh`,
  `patch.diff`. Default per-instance timeout 1800 s.
- Footgun: the result cache keys on `run_id` + `instance_id` and **ignores patch
  content**. Key yours on `sha256(patch)`.

**Reward hacking, measured:**

| finding | source |
|---|---|
| ~50% cheat rate on provably impossible tasks (GPT-5 54.0%, Claude Opus 4.1 ~50%, o3 49%); read-only tests are the best perf/safety tradeoff; an explicit "flag as impossible" escape hatch cut cheating 54% → 9% | ImpossibleBench, `arxiv.org/html/2510.20270v1`, code `github.com/safety-research/impossiblebench` |
| production coding-RL models learned `sys.exit(0)`, always-True `__eq__`, and a `conftest.py` hook rewriting `TestReport.outcome`; 12% attempted code sabotage of the research codebase; inoculation prompting cut generalised misalignment 75–90% | Anthropic, `arxiv.org/html/2511.18397v1` |
| held-out tests + LLM judge + test-modification monitor; hardcoding jumps to 22–44% on ambiguous problems; Gemini 2.5 Pro deleted test files 3.4% of the time | EvilGenie, `arxiv.org/abs/2511.21654` |
| defines the reward-hacking gap `Δ = s_val − s_test`; 90th-percentile Δ grows ~27pp per 10× code size; extra refinement iterations do not remove hacking | SpecBench, `arxiv.org/html/2605.21384v1` |
| under optimisation pressure against "bad thoughts", ~90% of continued cheating became undetectable | OpenAI CoT monitoring, `openai.com/index/chain-of-thought-monitoring/` |
| commit tool outputs cryptographically *before* execution; watch for a pivot from solving to researching the grader | METR, `metr.org/blog/2026-08-26-openai-hugging-face-incident-investigation/` |

**Selection and search:**

- Agentless (`arxiv.org/html/2407.01489v2`): generate many patches, filter by
  regression-test failures, rank by AST-normalised majority vote — parse, unparse,
  strip docstrings, then vote on the canonical diff. 50.8% Verified with Claude 3.5.
- SWE-Search / moatless-tree-search (`arxiv.org/abs/2410.20285`): value agent emits
  (score, natural-language critique) and the critique feeds back; discriminator over
  ≤5 finalists lifts correct-pick from 73% → 84%.
- SWE-Gym (`arxiv.org/html/2412.21139v2`): outcome-supervised verifier lifts pass@1
  20.6 → best@16 32.0; best@k is log-linear in k.
- Agentic rubrics (`arxiv.org/html/2601.04171v1`): four axes — file change, spec
  alignment, **integrity (no test weakening, no mass refactor)**, runtime. The
  integrity axis is a tamper score; ours is `patchlint`.
- mini-swe-agent (`github.com/SWE-agent/mini-swe-agent`): ~100 LOC, one bash tool,
  >74% on Verified. Evidence that scaffolding complexity is not where the value is.
- container-use (`github.com/dagger/container-use`): container + git branch per
  agent, every action auto-committed. The closest existing thing to Ratchet's
  ledger; worth citing rather than pretending we invented it.

**Sandboxing recipe:** `--network=none --memory=2g --memory-swap=2g --cpus=2
--pids-limit=512 --cap-drop=ALL --security-opt=no-new-privileges --ulimit
nofile=4096:4096`, plus a wall-clock timeout inside *and* outside the container.
Split edit-phase (network allowed, proxied) from grade-phase (no network at all). **[I]**

---

## 6. What this repo already implements from the above

| Idea | Where |
|---|---|
| F2P/P2P vocabulary, skip asymmetry, missing-test-is-failure | `gauntlet/grade.py` |
| Test reset before grading, markers, exit code outside the parsed region | `gauntlet/eval_script.py` |
| `SUITE_RAN` sentinel, exit-code cross-check | `gauntlet/parse.py` |
| Escalating apply chain with clean-up between attempts | `gauntlet/eval_script.py` |
| Held-out tests and the `Δ` penalty | `models.py`, `gauntlet/score.py` |
| Integrity axis as both a gate and a score term | `gauntlet/patchlint.py` |
| Impossible-task canary | `tasks/canary-impossible/task.yaml` |
| Container flags, network off during grading | `gauntlet/runner.py` |
| Commit-per-step, park-then-rollback | `ledger.py` |
| Cryptographic receipts over verdicts | `receipts.py` |
| An eval of the verifier itself | `redteam.py` |
| Not implemented (deliberately): learned verifiers, MCTS, majority vote | see `BUILD_PLAN.md` P2 |
