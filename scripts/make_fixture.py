#!/usr/bin/env python3
"""Write a recorded run to a bus file so the console can be built with no model.

The TUI renders entirely off the JSONL bus, which means the whole interface — tree,
stage rail, counters, budget line, approval bar — can be designed, demoed and
rehearsed with no harness, no key and no network. Run this, then:

    ratchet console --bus .ratchet/fixture.bus.jsonl

It is also the demo's insurance. If the live run dies at the judging table, this is
a faithful recording of the shape of one, and `ratchet replay` will play it back.

    python scripts/make_fixture.py [path] [delay-seconds-between-events]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ratchet.bus import Bus  # noqa: E402

DELAY = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0


def beat(bus: Bus, kind: str, **payload) -> None:
    bus.emit(kind, **payload)
    if DELAY:
        time.sleep(DELAY)


def stages(bus: Bus, label: str, results: list[tuple[str, bool, str]]) -> None:
    for name, ok, detail in results:
        beat(bus, "stage.result", label=label, stage=name, passed=ok, detail=detail,
             skipped=detail.startswith("no "))


def budget(nodes: int, elapsed: float, usd: float) -> dict:
    return {"nodes_used": nodes, "max_nodes": 40, "elapsed": elapsed,
            "max_seconds": 900, "usd_used": usd, "max_usd": 3.0}


GREEN = [("build", True, "ok"), ("cheat", True, "0 finding(s), 0 critical"), ("f2p", True, "7/7"),
         ("p2p", True, "3/3"), ("types", True, "clean"), ("lint", True, "clean"),
         ("hygiene", True, "1 file, 14 added lines")]


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".ratchet/fixture.bus.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    bus = Bus(path)

    beat(bus, "run.started", run_id="run-fixture", task="demo-001-slugify", provider="harness",
         snapshots=True, trunk="ratchet/run-fixture/trunk", budget=budget(0, 0, 0))
    beat(bus, "sandbox.created", label="root", provider="harness")
    beat(bus, "node.added", id="root", parent=None, score=0.35, green=False, outcome="progress",
         intent="baseline", depth=0, findings=[], reason="baseline")
    beat(bus, "repo.mapped", lines=18)

    # 1. a step that sticks
    beat(bus, "expand", node="root", fanout=1, depth=0, dead_ends=0)
    beat(bus, "sandbox.created", label="root-0", provider="harness", parent="root")
    beat(bus, "verify.started", label="root-0", parent="root", model="anthropic/claude-sonnet-4-6",
         intent="fold accents with NFKD before the ascii encode")
    stages(bus, "root-0", [("build", True, "ok"), ("cheat", True, "0 finding(s), 0 critical"),
                           ("f2p", False, "5/7  (hidden failing)"), ("p2p", True, "3/3"),
                           ("types", True, "clean"), ("lint", True, "clean"),
                           ("hygiene", True, "1 file, 6 added lines")])
    beat(bus, "node.added", id="0f3a", parent="root", score=0.62, green=False, outcome="progress",
         intent="fold accents with NFKD", model="anthropic/claude-sonnet-4-6", depth=1, findings=[])

    # 2. a regression, pruned
    beat(bus, "expand", node="0f3a", fanout=1, depth=1, dead_ends=0)
    beat(bus, "sandbox.created", label="0f3a-0", provider="harness", parent="0f3a")
    beat(bus, "verify.started", label="0f3a-0", parent="0f3a", model="anthropic/claude-sonnet-4-6",
         intent="truncate at max_length")
    stages(bus, "0f3a-0", [("build", True, "ok"), ("cheat", True, "0 finding(s), 0 critical"),
                           ("f2p", True, "7/7"), ("p2p", False, "2/3")])
    beat(bus, "node.pruned", id="4de0", parent="0f3a", score=0.44, green=False, outcome="regressed",
         intent="truncate at max_length", model="anthropic/claude-sonnet-4-6", depth=2,
         findings=[], reason="1 previously-passing test now fails")

    # 3. an integrity violation, pruned before it runs
    beat(bus, "expand", node="0f3a", fanout=1, depth=1, dead_ends=1)
    beat(bus, "verify.started", label="0f3a-1", parent="0f3a", model="openai/gpt-5.2",
         intent="handle the remaining cases directly")
    stages(bus, "0f3a-1", [("cheat", False, "3 finding(s), 2 critical")])
    beat(bus, "node.pruned", id="9ba4", parent="0f3a", score=0.0, green=False, outcome="cheated",
         intent="handle the remaining cases directly", model="openai/gpt-5.2", depth=2,
         findings=["protected_path", "skip_marker", "special_casing"],
         reason="integrity violation: skip_marker at tests/test_slugify_hidden.py:5")

    # 4. stall -> fan-out across providers
    beat(bus, "stall", node="0f3a", fanout=3, depth=1)
    beat(bus, "expand", node="0f3a", fanout=3, depth=1, dead_ends=2)
    for label, model, intent in [
        ("0f3a-a", "anthropic/claude-sonnet-4-6", "normalise first, then truncate on a token boundary"),
        ("0f3a-b", "openai/gpt-5.2", "rewrite the separator regex"),
        ("0f3a-c", "google-gemini/gemini-3-pro", "truncate on the last hyphen before the limit"),
    ]:
        beat(bus, "sandbox.created", label=label, provider="harness", parent="0f3a")
        beat(bus, "verify.started", label=label, parent="0f3a", model=model, intent=intent)
    beat(bus, "docs.fetch", library="unicodedata", version="3.11", via="cli")
    stages(bus, "0f3a-b", [("build", True, "ok"), ("cheat", True, "1 finding(s), 0 critical"),
                           ("f2p", False, "6/7  (hidden failing)"), ("p2p", True, "3/3")])
    beat(bus, "node.added", id="7c21", parent="0f3a", score=0.71, green=False, outcome="progress",
         intent="rewrite the separator regex", model="openai/gpt-5.2", depth=2, findings=["special_casing"])
    stages(bus, "0f3a-c", GREEN)
    beat(bus, "node.added", id="4f2a", parent="0f3a", score=1.0, green=True, outcome="green",
         intent="truncate on the last hyphen before the limit", model="google-gemini/gemini-3-pro",
         depth=2, findings=[], reason="all gates green")

    # 5. the search is over -- and only now does anything irreversible get proposed.
    # `cmd_run` searches to green first and asks to ship second, so the recording
    # ends held at the gate rather than sailing past it.
    beat(bus, "run.done", winner="4f2a", green=True, score=1.0, reason="verifier returned green",
         nodes=6, budget=budget(6, 372, 1.14))
    beat(bus, "approval.required", id="a1b2", action="open_pull_request",
         summary="demo-001-slugify: truncate on the last hyphen before the limit",
         stats={"nodes_explored": 6, "path_length": 3, "score": 1.0, "green": True, "cost_usd": 1.14},
         diff_preview="diff --git a/src/textkit/slugify.py b/src/textkit/slugify.py\n"
                      "@@\n-    return slug[:max_length]\n+    if len(slug) <= max_length:\n"
                      "+        return slug\n+    cut = slug[: max_length + 1]\n"
                      '+    if "-" in cut:\n+        cut = cut[: cut.rindex("-")]\n')

    print(f"fixture written to {path}")
    print(f"  ratchet console --bus {path}")
    print(f"  ratchet replay --bus {path}")


if __name__ == "__main__":
    main()
