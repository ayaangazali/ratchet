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

> **What it does.** Ratchet is a coding agent harness in which the agent never
> decides it is done — the tests do. Every step it takes becomes a git commit plus a
> sandbox snapshot, and every candidate patch must clear a verifier gauntlet — build,
> cheat check, fail-to-pass, pass-to-pass, types, lint, diff hygiene — before it is
> allowed to stick. Because each step is a restorable node, a run is not a linear loop
> with retries: it is a tree search over repo states, with the verifier's score as the
> value function and a scheduler deciding where to spend the next unit of compute.
> Stalled branches fork in parallel across sandboxes, dead ends are pruned and fed
> back as one-line warnings to their siblings, and the winning path exits as a single
> clean squashed diff sitting at a human approval gate.
>
> **How it uses TrueForge.** The harness runs everything except the definition of
> progress: model calls and multi-provider routing, context management and compaction,
> sandboxed execution and the snapshots that make forking cheap, the sub-agent threads
> that fan out on a stall, session persistence that makes rewind and reconnect real,
> and the approval interrupt that holds the pull request. Three roles run on three
> different providers on purpose — a cheap cartographer that maps the repo once, the
> generators that write patches, and a reviewer — so branch diversity is structural
> rather than a sampling artefact. There is no container orchestration and no provider
> SDK anywhere in the repository; `grep -r "docker run"` returns nothing, which is a
> deliberate design property rather than an omission. We wrote the search and the
> verifier and deleted a week of plumbing by not writing it.
>
> **Why it matters.** Give an agent a repository and a test command and it will
> eventually find that the cheapest way to make tests pass is to change what "pass"
> means. That is measured, not hypothetical. So we took the decision away — and then
> tested our own defences. `make redteam` fires ten published reward-hacking patterns
> at the verifier and reports how many got through, alongside two control patches that
> must *not* be caught. It has already earned its keep: it found an attack we had not
> thought of, where patched source rewrites a graded test file at import time, after
> the revert. And `make evals` runs a controlled experiment on our own machinery —
> same bugs, same draws, same call budget, linear loop versus verified search.

## Numbers to quote

- `make redteam` → caught `__/10`, false positives `__`
- `make evals` → linear `__%` ±`__` · search `__%` ±`__`; cheating patches that
  persisted: linear `__`, search `__`
- `make bench` → fork round trip `__`s, so we ran on `snapshots / worktrees`
- tests: `__` passing in `__`s, no docker and no network
- nodes explored in the demo run: `__`; cost `$__`
- receipts: hash-chained and signed; `ratchet audit` verifies a whole run

## Qodo evidence table (copy into the README)

| PR | What it changed | Qodo findings | Resolution |
|----|-----------------|---------------|------------|
| #  |                 |               |            |

## Last checks before you hit submit

- [ ] Clone the repo into a fresh directory and run `make dev && make demo && make test`
- [ ] Every secret is in `.env`, and `.env` is git-ignored
- [ ] The video plays without your machine
- [ ] Someone who did not write it can explain the architecture from the README
