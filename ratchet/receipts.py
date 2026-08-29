"""Tamper-evident verdict receipts.

A run's credibility rests on its history: these commits were green, those were
rolled back. If that history can be edited after the fact, the whole apparatus is
theatre. So every verdict gets a receipt, and the receipts form a hash chain.

    receipt_n.prev  = sha256(receipt_{n-1} without its signature)
    receipt_n.sig   = HMAC-SHA256(run key, receipt_n hash)

**What this proves.** The agent cannot forge, insert, reorder or edit a verdict, so
"these nodes were green" is evidence rather than assertion.
It never touches the orchestrator process, never sees the run key, and cannot
reach the receipt file from its sandbox. Any change to a past verdict breaks
every hash after it, and any new verdict without the key fails signature
verification.

**What this does not prove.** It is not a defence against the operator of the
machine, who has the key. It is not a notary and it is not a blockchain. It is
exactly one thing: evidence that the run you are looking at is the run that
happened.

`ratchet audit` verifies a chain and prints where it breaks. Point a judge at it
when they ask -- reasonably -- how they know the demo was not staged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .models import GauntletResult

GENESIS = "0" * 64


@dataclass
class Receipt:
    seq: int
    prev: str
    node_id: str
    outcome: str
    score: float
    green: bool
    result_digest: str
    findings: list[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)
    sig: str = ""

    def payload(self) -> str:
        d = asdict(self)
        d.pop("sig")
        return json.dumps(d, sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.payload().encode()).hexdigest()


class ReceiptBook:
    def __init__(self, path: Path, key_path: Path | None = None) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.key_path = key_path or path.with_suffix(".key")
        self.key = self._load_or_create_key()

    def _load_or_create_key(self) -> bytes:
        if self.key_path.exists():
            return bytes.fromhex(self.key_path.read_text().strip())
        key = secrets.token_bytes(32)
        # 0600: the agent has no path to this file anyway, but defence in depth is
        # cheap and someone will eventually run this on a shared box.
        fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(key.hex())
        return key

    # ----------------------------------------------------------------- write --

    def _sign(self, r: Receipt) -> str:
        return hmac.new(self.key, r.digest().encode(), hashlib.sha256).hexdigest()

    def all(self) -> list[Receipt]:
        if not self.path.exists():
            return []
        return [Receipt(**json.loads(line)) for line in self.path.read_text().splitlines() if line.strip()]

    def record_result(self, node_id: str, result: GauntletResult) -> Receipt:
        chain = self.all()
        prev = chain[-1].digest() if chain else GENESIS
        r = Receipt(
            seq=len(chain),
            prev=prev,
            node_id=node_id,
            outcome=result.outcome.value,
            score=round(result.score, 6),
            green=result.green,
            result_digest=hashlib.sha256(
                json.dumps(result.to_dict(), sort_keys=True, separators=(",", ":"), default=str).encode()
            ).hexdigest(),
            findings=[f.rule for f in result.findings],
        )
        r.sig = self._sign(r)
        with self.path.open("a") as fh:
            fh.write(json.dumps(asdict(r), separators=(",", ":")) + "\n")
        return r

    # ------------------------------------------------------------------ read --

    def verify(self) -> tuple[bool, list[str]]:
        """Return (ok, problems). Checks link integrity first, then signatures."""
        problems: list[str] = []
        prev = GENESIS
        for i, r in enumerate(self.all()):
            if r.seq != i:
                problems.append(f"receipt {i}: sequence number is {r.seq}, expected {i}")
            if r.prev != prev:
                problems.append(f"receipt {i} ({r.node_id}): chain broken -- prev {r.prev[:12]} != {prev[:12]}")
            if not hmac.compare_digest(r.sig, self._sign(r)):
                problems.append(f"receipt {i} ({r.node_id}): signature does not verify")
            prev = r.digest()
        return (not problems), problems

    def summary(self) -> dict[str, int | str]:
        rs = self.all()
        counts: dict[str, int] = {}
        for r in rs:
            counts[r.outcome] = counts.get(r.outcome, 0) + 1
        ok, problems = self.verify()
        return {
            "receipts": len(rs),
            "head": rs[-1].digest()[:16] if rs else GENESIS[:16],
            "chain": "intact" if ok else f"BROKEN ({len(problems)} problem(s))",
            **counts,
        }
