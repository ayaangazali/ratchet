#!/usr/bin/env python3
"""Fix TrueForge's OpenAI token parameter, in place.

TrueForge builds a provider's default params from the model catalog:

    defaultModelParams: model.properties.max_output_tokens
      ? { max_tokens: model.properties.max_output_tokens } : {}

OpenAI's Chat Completions API rejects `max_tokens` for the gpt-5 family; it wants
`max_completion_tokens`. So connecting an OpenAI provider fails at the first call,
and the error surfaces as a generic provider problem rather than as a parameter
name -- which sends you looking at your API key.

This is a patch to somebody else's shipped bundle, so it is written down rather than
done by hand: `npx` re-fetches the package into a content-addressed cache and wipes
the edit, usually five minutes before a demo. Re-run this after any `npx` fetch.

    python scripts/patch_trueforge.py            # patch
    python scripts/patch_trueforge.py --check    # report only, exit 1 if unpatched

Report it upstream too. A local patch is a stopgap, not a fix.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

OLD = ("defaultModelParams: model.properties.max_output_tokens ? "
       "{ max_tokens: model.properties.max_output_tokens } : {}")

#: `type` is the provider type and is already in scope at this point in the bundle.
NEW = ("defaultModelParams: model.properties.max_output_tokens ? "
       '(type === "openai" ? { max_completion_tokens: model.properties.max_output_tokens } '
       ": { max_tokens: model.properties.max_output_tokens }) : {}")


def bundles() -> list[Path]:
    """Every installed copy: npx caches by content hash, so there can be several."""
    roots = [
        Path.home() / ".npm" / "_npx",
        Path.home() / ".npm" / "_cacache" / "tmp",
        Path("node_modules"),
    ]
    found: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        found += list(root.glob("**/@truefoundry/trueforge/dist/main.js"))
    return sorted(set(found))


def main() -> int:
    ap = argparse.ArgumentParser(description="patch TrueForge's OpenAI token parameter")
    ap.add_argument("--check", action="store_true", help="report only; exit 1 if any copy is unpatched")
    args = ap.parse_args()

    paths = bundles()
    if not paths:
        print("no installed TrueForge bundle found.\n"
              "  start it once so npx downloads it:  npx @truefoundry/trueforge@latest")
        return 1

    unpatched = 0
    for p in paths:
        text = p.read_text(errors="replace")
        if "max_completion_tokens" in text:
            print(f"  already patched  {p}")
            continue
        if OLD not in text:
            print(f"  ! pattern not found; the bundle has changed  {p}")
            unpatched += 1
            continue
        unpatched += 1
        if args.check:
            print(f"  NEEDS PATCH      {p}")
            continue
        backup = p.with_suffix(".js.ratchet-backup")
        if not backup.exists():
            shutil.copy2(p, backup)
        p.write_text(text.replace(OLD, NEW, 1))
        print(f"  patched          {p}\n    backup at      {backup}")

    if args.check:
        return 1 if unpatched else 0
    if unpatched:
        print("\n  restart TrueForge for this to take effect.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
