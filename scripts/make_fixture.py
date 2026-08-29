#!/usr/bin/env python3
"""Write a fake run to a bus file so the TUI can be built without a model.

The console is driven entirely by the JSONL bus, which means the whole interface --
gate rail, spine, scoreboard, approval bar -- can be designed, demoed and rehearsed
with no harness, no API key and no network. Run this, then `ratchet console --bus
.ratchet/fixture.bus.jsonl`.

It is also the fallback if the live run dies during judging: this is a complete,
honest recording of the shape of a real run.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ratchet.bus import Bus  # noqa: E402

DELAY = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0


def beat(bus: Bus, kind: str, **payload) -> None:
    bus.emit(kind, **payload)
    if DELAY:
        time.sleep(DELAY)


def gates(bus: Bus, attempt: str, results: list[tuple[str, bool, str]]) -> None:
    for name, ok, detail in results:
        beat(bus, "gate.result", attempt=attempt, gate=name, passed=ok, detail=detail)


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".ratchet/fixture.bus.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    bus = Bus(path)

    beat(bus, "run.started", run_id="run-fixture", task="demo-001-slugify", backend="docker",
         trunk="ratchet/run-fixture/trunk", base="1c09aa2f")
    beat(bus, "agent.text", thread="main", text="Reading slugify.py and the visible tests.")
    beat(bus, "agent.tool", thread="main", tool="repo_read", ok=True, preview="src/textkit/slugify.py")

    # 1. an accepted attempt
    beat(bus, "attempt.submitted", attempt="a1", branch="trunk", rationale="fold accents with NFKD", diff_lines=9)
    gates(bus, "a1", [("cheat", True, "0 finding(s), 0 critical"), ("apply", True, "patch applied"),
                      ("build", True, "ok"), ("f2p", True, "3/3 visible"), ("hidden", True, "4/4 held-out"),
                      ("p2p", True, "3/3 kept green"), ("types", True, "clean"), ("lint", True, "clean"),
                      ("decision", True, "all gates green")])
    beat(bus, "verdict", attempt_id="a1", decision="accepted", score=0.744, commit_sha="0b7e441c",
         f2p_visible_rate=1.0, f2p_hidden_rate=1.0, p2p_rate=1.0, delta=0.0, findings=[], gates=[])

    # 2. a rejection that rolls back
    beat(bus, "agent.text", thread="main", text="Now the truncation boundary.")
    beat(bus, "attempt.submitted", attempt="71c9", branch="trunk", rationale="truncate at max_length", diff_lines=6)
    gates(bus, "71c9", [("cheat", True, "0 finding(s), 0 critical"), ("apply", True, "patch applied"),
                        ("build", True, "ok"), ("f2p", True, "3/3 visible"), ("hidden", False, "2/4 held-out  <-- fix does not generalise"),
                        ("p2p", False, "2/3 kept green"), ("decision", False, "1 pass-to-pass test(s) regressed")])
    beat(bus, "verdict", attempt_id="71c9", decision="rejected", score=0.612,
         f2p_visible_rate=1.0, f2p_hidden_rate=0.5, p2p_rate=0.67, delta=0.5, findings=[], gates=[])
    beat(bus, "rollback", attempt="71c9", to="0b7e441c", reason="rejected")

    # 3. an integrity violation
    beat(bus, "attempt.submitted", attempt="8a31", branch="trunk", rationale="handle the failing cases directly", diff_lines=14)
    gates(bus, "8a31", [("cheat", False, "3 finding(s), 2 critical"),
                        ("decision", False, "integrity violation: protected_path at tests/test_slugify_hidden.py:0")])
    beat(bus, "verdict", attempt_id="8a31", decision="disqualified", score=0.0,
         findings=[{"rule": "protected_path", "severity": "critical"},
                   {"rule": "skip_marker", "severity": "critical"},
                   {"rule": "special_casing", "severity": "high"}], gates=[])
    beat(bus, "rollback", attempt="8a31", to="0b7e441c", reason="disqualified")

    # 4. stall -> fan-out -> arbitration
    beat(bus, "stall", attempts=3)
    beat(bus, "fanout", labels=["cand-a", "cand-b", "cand-c"], base="0b7e441c",
         plan="a: normalise first  b: rewrite the regex  c: truncate on token boundary")
    for label, tid in [("cand-a", "th_9f21"), ("cand-b", "th_4c07"), ("cand-c", "th_11ab")]:
        beat(bus, "thread.created", thread=tid, title=label, parent="main")
        beat(bus, "agent.text", thread=tid, text=f"[{label}] restating the task and the hypothesis.")
    beat(bus, "docs.fetch", library="unicodedata", version="3.11", url="https://docs.python.org/3/library/unicodedata.html", via="cli")
    beat(bus, "arbitration", rows=[
        {"label": "cand-c", "score": 0.981, "hidden": 1.0, "visible": 1.0, "p2p": 1.0, "delta": 0.0, "findings": []},
        {"label": "cand-a", "score": 0.703, "hidden": 0.5, "visible": 1.0, "p2p": 1.0, "delta": 0.5, "findings": []},
        {"label": "cand-b", "score": 0.402, "hidden": 0.0, "visible": 1.0, "p2p": 1.0, "delta": 1.0, "findings": ["special_casing"]},
    ])
    for tid in ("th_9f21", "th_4c07", "th_11ab"):
        beat(bus, "thread.done", thread=tid, state="done")
    beat(bus, "verdict", attempt_id="c3", decision="accepted", score=0.981, commit_sha="4f2a19cd",
         f2p_visible_rate=1.0, f2p_hidden_rate=1.0, p2p_rate=1.0, delta=0.0, findings=[], gates=[])

    # 5. the gate
    beat(bus, "approval.required", tool_call_id="call_demo", thread="main", tool="open_pull_request",
         arguments={"title": "fix(slugify): fold accents and truncate on a word boundary",
                    "body": "Verified: 3/3 visible, 4/4 held-out, 3/3 regression, types and lint clean."})

    print(f"fixture written to {path}")
    print(f"  ratchet console --bus {path}")


if __name__ == "__main__":
    main()
