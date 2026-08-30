"""The gate owns the irreversible half, not just the question.

`Gate.push` exists so no caller holds a `Decision` and a `git push` at the same
time. These tests are the enforcement: the push must not run on a denial, and the
argv it runs on an approval is the one the gate builds.
"""

from __future__ import annotations

import threading
import time

from ratchet import gate as gate_mod


def _approver(g, *, allow=True):
    """Answer the request as soon as it appears, from the side, the way a human
    (or the console) would while `push` is blocked in its wait."""

    def run():
        for _ in range(200):
            pending = g.pending()
            if pending:
                g.decide(pending[0], allow)
                return
            time.sleep(0.02)

    return threading.Thread(target=run)


def test_a_denied_push_runs_no_git(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(gate_mod.subprocess, "run", lambda argv, **kw: calls.append(argv))

    g = gate_mod.Gate(tmp_path / "denied")
    t = _approver(g, allow=False)
    t.start()
    dec = g.push(summary="s", diff="d", timeout_s=10)
    t.join()

    assert not dec.allow
    assert calls == []


def test_an_unanswered_push_runs_no_git(tmp_path, monkeypatch):
    """Silence is a denial. The window closing must not be a way to push."""
    calls = []
    monkeypatch.setattr(gate_mod.subprocess, "run", lambda argv, **kw: calls.append(argv))

    g = gate_mod.Gate(tmp_path / "ignored")
    dec = g.push(summary="s", diff="d", timeout_s=0.5)

    assert not dec.allow
    assert calls == []


def test_an_approved_push_runs_git_push_in_the_repo(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(gate_mod.subprocess, "run", lambda argv, **kw: calls.append(argv))

    repo = tmp_path / "allowed"
    g = gate_mod.Gate(repo)
    t = _approver(g, allow=True)
    t.start()
    dec = g.push(summary="s", diff="d", timeout_s=10)
    t.join()

    assert dec.allow
    assert calls == [["git", "-C", str(repo), "push"]]
