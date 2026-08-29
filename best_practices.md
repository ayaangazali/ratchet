# Best practices for this repository

- **The verifier is adversarial by design.** When a check looks paranoid, it is
  usually load-bearing. Do not simplify a guard without reading the comment above it.
- **Fail closed.** An error inside a gate is `INFRA` or a rejection, never a pass. An
  empty test log is not a clean sweep; an unparseable exit code is not a zero.
- **Pure functions in `gauntlet/`.** `parse`, `grade`, `patchlint` and `score` take
  data and return data. No I/O, no subprocess, no network. Anything else belongs in
  `runner.py`.
- **Subprocess calls take argv lists.** Never `shell=True`. Never interpolate a path
  the agent influenced into a shell string.
- **Every new verification rule ships with two tests**: a patch that trips it and a
  patch that must not.
- **Observations are the agent's context window.** Keep `to_observation()` short and
  factual. Long verdicts cost more than they teach.
- **Never widen what the agent can read** without asking what it would do with the
  information if it were trying to cheat.
