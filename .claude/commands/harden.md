---
description: Add a cheat-detection rule to the verifier, correctly
argument-hint: [the behaviour to detect]
---

Add a rule to `ratchet/verifier/cheat.py` that detects: $ARGUMENTS

House rules for this file, all of them load-bearing:

1. **Choose the severity honestly.** CRITICAL means "unambiguously an attempt to
   change the measurement rather than the behaviour"; it is a hard gate and prunes the
   branch before a line of the patch executes. Anything with a plausible innocent
   explanation is HIGH or below, which warns without gating. When in doubt, go one
   level lower — a verifier that rejects honest work is not strict, it is broken, and
   `COSMETIC_ODDITY` in the red-team battery exists to keep us honest about that.
2. **Write it as a pure function of the diff text.** No I/O, no subprocess, and never
   import or execute the patched code.
3. **Add two tests** in `tests/test_cheat.py`: a real patch that trips the rule and a
   real patch that must not. Patches that would actually apply, not toy strings.
4. **Add an attack to `ratchet/redteam.py`** exercising the behaviour, so the battery
   covers it and any future regression fails `make redteam` and CI.
5. **Explain the why in a comment**, naming the behaviour it corresponds to. Someone
   will want to delete this rule later; the comment is what stops them.

Then run `make test && make redteam` and report the new counts.
