# Best practices for this repository

- **The verifier is adversarial by design.** When a check looks paranoid, it is
  usually load-bearing. Do not simplify a guard without reading the comment above it.
- **Fail closed.** An error inside a stage is `INFRA` or a prune, never a pass. An
  empty test log is not a clean sweep; an unparseable exit code is not a zero.
- **Pure functions in `verifier/`.** `parsers`, `grade` and `cheat` take data and
  return data — no I/O, no subprocess, no network. Anything else belongs in
  `gauntlet.py` or `sandbox.py`.
- **We do not orchestrate containers.** Sandboxes come from the harness; the fallback
  is git worktrees. A `docker run` in this repo is a design regression, not a shortcut.
- **Subprocess calls take argv lists.** Never `shell=True`, and never interpolate an
  agent-influenced string into a shell.
- **Held-out test names are radioactive.** They may appear in the task file and the
  gauntlet, and nowhere that can reach a prompt.
- **Every new verification rule ships with two tests** — a patch that trips it and one
  that must not — plus an entry in the red-team battery.
- **Observations are the agent's whole feedback channel.** Keep `to_observation()`
  short and factual; long verdicts cost more than they teach.
- **Pruned work is parked, never destroyed.** A dead end is still a node you can
  rewind to.
- **Never widen what the agent can read** without asking what it would do with the
  information if it were trying to cheat.
