## What changed

<!-- What and why. Not a file list. -->

## Where a reviewer should look hardest

<!-- Delete what doesn't apply: -->
- [ ] Can any new path set `green` outside `verifier/gauntlet.py`?
- [ ] Can a held-out test name (`f2p_hidden`) reach any string the agent reads?
- [ ] Does anything orchestrate a container directly, rather than going through the
      sandbox provider?
- [ ] Does any stage swallow an error in a way that grades as a pass?
- [ ] Is a pruned node still parked and reachable?

## Checks

- [ ] `make test` green
- [ ] `make lint` clean
- [ ] `make redteam` still catches the whole battery with zero false positives
- [ ] New verification rules ship with a patch that trips them, one that must not,
      and a red-team entry
- [ ] Qodo review addressed — fixed, or answered in the thread
