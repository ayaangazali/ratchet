---
description: Run the full local gate — tests, lint, types, and the red-team battery
---

Run every check this repository has, in this order, and report the results as a
short table. Do not fix anything yet; report first.

```bash
make test
make lint
make redteam
```

Then state, in one line each:

1. Whether `make redteam` printed `caught 10/10` with zero false positives. If it
   did not, that is the only thing that matters — say which attack got through and
   which gate should have caught it.
2. Whether any test failure is in `gauntlet/` (the verifier itself) or elsewhere.
   A failure in the verifier is a stop-everything event; a failure in the TUI is not.
3. Whether `ratchet verify` still works with no network, since that is the demo's
   insurance policy:

```bash
python -m ratchet.cli verify --task tasks/demo-001-slugify/task.yaml \
  --repo demo-repo --diff demo-repo/patches/cheat.diff --backend local
```
