"""The receipt chain is only worth having if it actually breaks when tampered with."""

from __future__ import annotations

import json

from ratchet.models import Decision, Verdict
from ratchet.receipts import ReceiptBook


def _v(attempt: str, decision: Decision, score: float) -> Verdict:
    return Verdict(attempt_id=attempt, task_id="t", branch="trunk", decision=decision, score=score)


def _book(tmp_path) -> ReceiptBook:
    return ReceiptBook(tmp_path / "run.receipts.jsonl")


def test_chain_verifies_when_untouched(tmp_path):
    book = _book(tmp_path)
    book.record(_v("a1", Decision.ACCEPTED, 0.9))
    book.record(_v("a2", Decision.REJECTED, 0.4))
    book.record(_v("a3", Decision.ACCEPTED, 0.99))
    ok, problems = book.verify()
    assert ok, problems
    assert book.summary()["receipts"] == 3


def test_editing_a_past_verdict_breaks_the_chain(tmp_path):
    book = _book(tmp_path)
    book.record(_v("a1", Decision.REJECTED, 0.2))
    book.record(_v("a2", Decision.ACCEPTED, 0.95))
    lines = book.path.read_text().splitlines()
    first = json.loads(lines[0])
    first["decision"] = "accepted"          # rewrite history: the red one was green all along
    lines[0] = json.dumps(first, separators=(",", ":"))
    book.path.write_text("\n".join(lines) + "\n")

    ok, problems = book.verify()
    assert not ok
    assert any("signature" in p for p in problems)
    assert any("chain broken" in p for p in problems)


def test_appending_a_forged_verdict_fails_signature(tmp_path):
    book = _book(tmp_path)
    real = book.record(_v("a1", Decision.REJECTED, 0.1))
    forged = {
        "seq": 1, "prev": real.digest(), "attempt_id": "fake", "task_id": "t", "branch": "trunk",
        "decision": "accepted", "score": 1.0, "commit_sha": "deadbeef", "verdict_digest": "0" * 64,
        "findings": [], "ts": 0.0, "sig": "f" * 64,
    }
    with book.path.open("a") as fh:
        fh.write(json.dumps(forged, separators=(",", ":")) + "\n")

    ok, problems = book.verify()
    assert not ok
    assert any("signature does not verify" in p for p in problems)


def test_dropping_a_receipt_is_detected(tmp_path):
    book = _book(tmp_path)
    for i in range(3):
        book.record(_v(f"a{i}", Decision.REJECTED, 0.1 * i))
    lines = book.path.read_text().splitlines()
    book.path.write_text("\n".join([lines[0], lines[2]]) + "\n")   # drop the middle one
    ok, problems = book.verify()
    assert not ok
    assert any("sequence number" in p or "chain broken" in p for p in problems)
