"""The receipt chain is only worth having if it breaks when someone edits history.

Each of these tests is a specific way to fake a run: rewrite a past result, append a
forged green one, drop a receipt from the middle. All three have to be detectable
without trusting anything the agent could reach.
"""

from __future__ import annotations

import json
import threading

from ratchet.models import GauntletResult, Outcome
from ratchet.receipts import ReceiptBook


def _r(outcome: Outcome, score: float) -> GauntletResult:
    return GauntletResult(outcome=outcome, score=score, green=outcome is Outcome.GREEN)


def _book(tmp_path) -> ReceiptBook:
    return ReceiptBook(tmp_path / "run.receipts.jsonl")


def test_concurrent_writers_keep_the_chain_intact(tmp_path):
    """loop.expand grades a fan-out on a thread pool; every candidate records a
    receipt. Unsynchronised, two writers read the same tail and both append seq=n
    with the same prev -- a broken chain the run inflicted on itself."""
    book = _book(tmp_path)
    n = 32
    barrier = threading.Barrier(n)

    def write(i: int) -> None:
        barrier.wait()  # maximise the collision window
        book.record_result(f"n{i}", _r(Outcome.PROGRESS, i / n))

    threads = [threading.Thread(target=write, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    book.seal("done")
    ok, problems = book.verify()
    assert ok, problems
    receipts = book.all()
    assert len(receipts) == n + 1
    assert sorted(r.seq for r in receipts) == list(range(n + 1))


def test_chain_verifies_when_untouched(tmp_path):
    book = _book(tmp_path)
    book.record_result("root", _r(Outcome.PROGRESS, 0.3))
    book.record_result("0f3a", _r(Outcome.REGRESSED, 0.4))
    book.record_result("4f2a", _r(Outcome.GREEN, 1.0))
    book.seal("done")
    ok, problems = book.verify()
    assert ok, problems
    assert book.summary()["receipts"] == 4
    assert book.summary()["chain"] == "intact"


def test_truncating_the_tail_is_detected(tmp_path):
    """Link integrity alone proves order, not completeness: deleting receipts from
    the tail used to audit clean. The seal makes a shortened chain visible."""
    book = _book(tmp_path)
    book.record_result("a", _r(Outcome.PROGRESS, 0.3))
    book.record_result("b", _r(Outcome.CHEATED, 0.0))  # the verdict someone wants gone
    book.seal("done")
    lines = book.path.read_text().splitlines()
    book.path.write_text("\n".join(lines[:-1]) + "\n")  # drop the seal
    ok, problems = book.verify()
    assert not ok
    assert any("unsealed" in p for p in problems)
    book.path.write_text("\n".join(lines[:-2]) + "\n")  # drop the cheat verdict too
    ok, problems = book.verify()
    assert not ok


def test_a_chain_truncated_to_nothing_does_not_audit_clean(tmp_path):
    book = _book(tmp_path)
    book.record_result("a", _r(Outcome.CHEATED, 0.0))
    book.seal("done")
    book.path.write_text("")  # delete every verdict, including the seal
    ok, problems = book.verify()
    assert not ok and any("empty" in p for p in problems)


def test_an_unsealed_chain_does_not_audit_clean(tmp_path):
    book = _book(tmp_path)
    book.record_result("a", _r(Outcome.PROGRESS, 0.3))
    ok, problems = book.verify()
    assert not ok and any("unsealed" in p for p in problems)


def test_rewriting_a_past_result_breaks_the_chain(tmp_path):
    book = _book(tmp_path)
    book.record_result("0f3a", _r(Outcome.REGRESSED, 0.2))
    book.record_result("4f2a", _r(Outcome.GREEN, 0.95))
    book.seal("done")

    lines = book.path.read_text().splitlines()
    first = json.loads(lines[0])
    first["outcome"] = "green"  # "that pruned node was fine all along"
    lines[0] = json.dumps(first, separators=(",", ":"))
    book.path.write_text("\n".join(lines) + "\n")

    ok, problems = book.verify()
    assert not ok
    assert any("signature" in p for p in problems)
    assert any("chain broken" in p for p in problems)


def test_appending_a_forged_green_fails_the_signature(tmp_path):
    book = _book(tmp_path)
    real = book.record_result("0f3a", _r(Outcome.REGRESSED, 0.1))
    forged = {
        "seq": 1, "prev": real.digest(), "node_id": "fake", "outcome": "green", "score": 1.0,
        "green": True, "result_digest": "0" * 64, "findings": [], "ts": 0.0, "sig": "f" * 64,
    }
    with book.path.open("a") as fh:
        fh.write(json.dumps(forged, separators=(",", ":")) + "\n")

    ok, problems = book.verify()
    assert not ok
    assert any("signature does not verify" in p for p in problems)


def test_dropping_a_receipt_is_detected(tmp_path):
    book = _book(tmp_path)
    for i in range(3):
        book.record_result(f"n{i}", _r(Outcome.PROGRESS, 0.1 * i))
    lines = book.path.read_text().splitlines()
    book.path.write_text("\n".join([lines[0], lines[2]]) + "\n")  # drop the middle
    ok, problems = book.verify()
    assert not ok
    assert any("sequence number" in p or "chain broken" in p for p in problems)


def test_the_key_is_not_readable_from_the_repo_tree(tmp_path):
    """The agent never has a path to the key. Check the mode anyway — defence in
    depth is free, and someone will eventually run this on a shared machine."""
    book = _book(tmp_path)
    book.record_result("root", _r(Outcome.PROGRESS, 0.1))
    mode = book.key_path.stat().st_mode & 0o777
    assert mode == 0o600
