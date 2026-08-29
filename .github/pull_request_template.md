## What changed

<!-- What and why. Not a file list. -->

## Where a reviewer should look hardest

<!-- Default answers, delete what doesn't apply: -->
- [ ] Can any new path set `ACCEPTED` outside `gauntlet/score.py::decide`?
- [ ] Can a held-out test name (`f2p_hidden`) reach any string the agent reads?
- [ ] Does anything weaken the sandbox — network, pids, caps, `shell=True`?
- [ ] Does any gate now swallow an error in a way that grades as a pass?

## Checks

- [ ] `make test` green
- [ ] `make lint` clean
- [ ] `make redteam` still 10/10 with zero false positives
- [ ] New verification rules ship with a patch that trips them and one that must not
- [ ] Qodo review addressed (fixed, or answered in the thread)
