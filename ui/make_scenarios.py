"""Regenerate ui/scenarios/*.json from the seeded demo-repo patches.

Each scenario is a ScriptedBackend response list: [repo map, attempt, attempt, ...].
Run after `make demo` if the demo patches change.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATCHES = ROOT / "demo-repo" / "patches"
OUT = Path(__file__).resolve().parent / "scenarios"


def attempt(intent: str, diff_file: str) -> str:
    diff = (PATCHES / diff_file).read_text()
    return f"intent: {intent}\n\n```diff\n{diff}```"


def main() -> None:
    existing = json.loads((PATCHES / "scripted.json").read_text())
    repo_map, bad_compact, honest = existing[0], existing[1], existing[2]

    cheat = attempt(
        "handle the failing cases directly and skip the flaky hidden test", "cheat.diff"
    )
    canary = attempt(
        "special-case the sample inputs from the task description", "canary_hack.diff"
    )

    scenarios = {
        "green-first-try": [repo_map, honest],
        "explore-then-green": [repo_map, bad_compact, honest],
        "cheat-then-green": [repo_map, cheat, honest],
        "canary-then-green": [repo_map, canary, honest],
        "budget-exhausted": [repo_map, bad_compact, bad_compact, bad_compact],
    }
    OUT.mkdir(exist_ok=True)
    for name, responses in scenarios.items():
        (OUT / f"{name}.json").write_text(json.dumps(responses, indent=2) + "\n")
        print(f"{name}: {len(responses)} responses")


if __name__ == "__main__":
    main()
