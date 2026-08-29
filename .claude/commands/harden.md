---
description: Add a new cheat-detection rule to patchlint, correctly
argument-hint: [the behaviour to detect]
---

Add a rule to `src/ratchet/gauntlet/patchlint.py` that detects: $ARGUMENTS

Follow the house rules for this file exactly:

1. **Decide the severity honestly.** CRITICAL means "this is unambiguously an
   attempt to change the measurement rather than the behaviour", and it
   disqualifies an attempt before a single line of the patch executes. Anything
   with a plausible innocent explanation is HIGH or below, which warns rather than
   gates. When in doubt, go one level lower — a verifier that rejects honest work
   is not strict, it is broken.
2. **Write the detector as a pure function of the diff text.** No I/O, no
   subprocess, no importing the patched code, ever.
3. **Add two tests** in `tests/test_patchlint.py`: a real patch that trips the rule
   and a real patch that must not. Not toy strings — patches that would actually
   apply.
4. **Add an attack to `src/ratchet/redteam.py`** exercising the behaviour, so the
   battery covers it from now on and any future regression is caught by
   `make redteam`.
5. **Explain the *why* in a comment** above the rule, naming the behaviour it
   corresponds to. Someone will want to delete this rule later; the comment is what
   stops them.

Then run `make test && make redteam` and report the new counts.
