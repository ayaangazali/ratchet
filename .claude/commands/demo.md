---
description: Rehearse the offline demo and check every promised line still prints
---

Run the six commands that need no model, no key and no network, and confirm each one
still produces what `DEMO.md` promises. These are the demo's insurance policy.

```bash
make clean && make demo
make redteam
make run-offline
python -m ratchet.cli tree --repo demo-repo
python -m ratchet.cli audit --repo demo-repo
make evals
```

Expected — worth checking precisely, because these lines get said out loud:

- **redteam** → `caught 10/10`, `false positives on the honest fix: 0`
- **run-offline** → reaches green, and the tree shows one pruned node and one green
- **tree** → a root, a red pruned node, a green winner
- **audit** → `chain intact`
- **evals** → search at or above linear, and `cheating patches that persisted …
  search 0`

Also check the three standalone verdicts:

```bash
python -m ratchet.cli verify --task tasks/demo-001-slugify/task.yaml --repo demo-repo --diff demo-repo/patches/honest.diff      # GREEN, score 1.000
python -m ratchet.cli verify --task tasks/demo-001-slugify/task.yaml --repo demo-repo --diff demo-repo/patches/cheat.diff       # CHEATED at the cheat stage, nothing else ran
python -m ratchet.cli verify --task tasks/canary-impossible/task.yaml --repo demo-repo --diff demo-repo/patches/canary_hack.diff # CHEATED, zero static findings
```

Report any drift, then leave the tree clean: `make clean && make demo`.
