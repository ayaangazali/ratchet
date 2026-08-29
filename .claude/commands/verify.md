---
description: Run the full local gate — tests, lint, types, the red team, and the eval suite
---

Run every check this repository has and report the results as a short table. Report
first; do not fix anything yet.

```bash
make test
make lint
make redteam
make evals
```

Then say, in one line each:

1. Whether `make redteam` printed `caught 10/10` with zero false positives. If not,
   that is the only thing that matters — name the attack that got through and the
   stage that should have caught it. (If it refuses because the demo repo is not at
   its baseline, run `make clean && make demo` and try again.)
2. Whether any test failure is in `verifier/` — a failure there is a stop-everything
   event; one in the TUI is not.
3. Whether `make evals` still shows search at or above linear, and zero cheating
   patches persisting under search.
4. Whether the offline path still works, since it is the demo's insurance:

```bash
make run-offline && python -m ratchet.cli tree --repo demo-repo
```
