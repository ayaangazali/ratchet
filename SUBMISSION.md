# Submission checklist

Fill this in as you go. The parts that are text go straight into the submission form
or the README, so writing them here first means you are not composing prose at 17:50.

## Required by the rules

- [ ] Public repository — URL: `________`
- [ ] README with setup steps that a stranger can follow
- [ ] Demo video, roughly three minutes — URL: `________`
- [ ] Write-up: what the agent does and how it uses TrueForge (draft below)
- [ ] `## Qodo Code Review Evidence` section in the README with merged PR links
- [ ] Blog post, if entering that prize — URL: `________`

## The write-up (paste into the README and the form)

> **What it does.** Ratchet is a coding agent that cannot decide it is done. It is
> given a defect and a repository it cannot write to directly. It reads freely,
> experiments in a sandbox, and submits patches to a verifier it does not control.
> Every accepted patch is a git commit; every rejected one is rolled back to the
> last green commit and handed back as the agent's next observation. When it stalls,
> sub-agents fan out across parallel branches and the verifier keeps the highest
> scoring one. The only action that leaves the machine — opening a pull request —
> waits for a human.
>
> **How it uses TrueForge.** The harness runs everything except the definition of
> progress. It owns the agent loop and the model calls, context management and
> compaction, MCP tool dispatch including OAuth, the sandbox the agent experiments
> in, the sub-agent threads that fan out on a stall, session persistence across
> reconnects, and the approval interrupt that holds the pull request. Ratchet
> registers one custom MCP server carrying the tools that touch the graded tree, and
> declares `open_pull_request` in `require_approval_for_tools` so the harness — not
> our prompt — is what stops it. The console is a client of TrueForge's SSE stream:
> every sub-agent thread, sandbox creation and approval pause you see on screen is
> a harness event, mirrored onto a local bus. We wrote no agent loop, no context
> manager, no OAuth dance and no approval machinery, and spent the day on the only
> question the harness cannot answer for us.
>
> **Why it matters.** Give an agent a repository and a test command and it will
> eventually find that the cheapest way to make tests pass is to change what "pass"
> means. That is measured, not hypothetical. So we took the decision away, and then
> tested our own defences: `make redteam` fires ten published reward-hacking patterns
> at the verifier and reports how many got through, alongside a control patch that
> must not be caught.

## Numbers to quote

- `make redteam` → caught `__/10`, false positives `__`
- tests: `__` passing, runtime `__`, no network required
- verdict receipts: hash-chained and signed; `ratchet audit` verifies a run
- lines of Python: `__`

## Qodo evidence table (copy into the README)

| PR | What it changed | Qodo findings | Resolution |
|----|-----------------|---------------|------------|
| #  |                 |               |            |

## Last checks before you hit submit

- [ ] Clone the repo into a fresh directory and run `make dev && make demo && make test`
- [ ] Every secret is in `.env`, and `.env` is git-ignored
- [ ] The video plays without your machine
- [ ] Someone who did not write it can explain the architecture from the README
