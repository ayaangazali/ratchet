"""Ratchet web gateway — real offline runs streamed to the UI, plus the live Qodo feed.

Every run the UI shows is a genuine `ratchet run --scripted` subprocess: the search
loop, the verifier gauntlet, the receipts and the approval gate are all the real
thing. This file only seeds a demo repo, spawns the CLI, tails the bus JSONL and
translates events to SSE. It never decides outcomes — the gauntlet does.

The Qodo panel is live data too: review comments the `qodo-code-review[bot]` left on
this repository's actual pull requests, fetched with `gh` and cached to disk so the
demo survives venue Wi-Fi.

Run:  uv run --with fastapi --with uvicorn python ui/gateway.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse

UI_DIR = Path(__file__).resolve().parent
ROOT = UI_DIR.parent
RUNS_DIR = UI_DIR / "runs"
SCENARIOS = UI_DIR / "scenarios"
QODO_CACHE = UI_DIR / "qodo_cache.json"
QODO_TTL_S = 600
GH_REPO = "ayaangazali/ratchet"
# The ratchet CLI needs the project venv; the gateway itself runs anywhere.
# The venv's bin must also lead PATH — the harness invokes `pytest` by name, and
# without it every node grades as an infra failure ("no evidence the suite ran").
VENV_BIN = ROOT.parent.parent.parent / ".venv" / "bin"
RATCHET_PY = str(VENV_BIN / "python")
RUN_ENV = {**os.environ, "PATH": f"{VENV_BIN}:{os.environ.get('PATH', '')}"}
# A demo run grades in seconds; anything alive this long without a result is the
# scripted-exhaustion spin (see TESTING.md) and gets killed, not waited out.
RUN_KILL_S = 60

app = FastAPI()

# ---------------------------------------------------------------------------
# Runs

# prompt keyword -> scenario file; first match wins, default is the honest fix.
SCENARIO_RULES: list[tuple[str, str]] = [
    ("cheat", "cheat-then-green"),
    ("canary", "canary-then-green"),
    ("special-case", "canary-then-green"),
    ("explore", "explore-then-green"),
    ("exhaust", "budget-exhausted"),
    ("give up", "budget-exhausted"),
]


def pick_scenario(prompt: str) -> str:
    p = prompt.lower()
    for needle, name in SCENARIO_RULES:
        if needle in p:
            return name
    return "green-first-try"


class Run:
    def __init__(self, prompt: str, budget: int | None) -> None:
        self.id = f"run-{uuid.uuid4().hex[:8]}"
        self.prompt = prompt
        self.scenario = pick_scenario(prompt)
        self.budget = budget
        self.dir = RUNS_DIR / self.id
        self.proc: subprocess.Popen | None = None
        self.started_at = time.time()
        self.approval_id: str | None = None
        self.result: dict | None = None
        self.decision: dict | None = None
        self.error: str | None = None

    @property
    def attempts(self) -> int:
        return len(json.loads((SCENARIOS / f"{self.scenario}.json").read_text())) - 1

    def start(self) -> None:
        self.dir.parent.mkdir(exist_ok=True)
        subprocess.run(
            [RATCHET_PY, "-m", "ratchet.cli", "demo", "--dir", str(self.dir)],
            cwd=ROOT, env=RUN_ENV, capture_output=True, check=True, timeout=120,
        )
        argv = [
            RATCHET_PY, "-m", "ratchet.cli", "run",
            "--repo", str(self.dir),
            "--scripted", str(SCENARIOS / f"{self.scenario}.json"),
        ]
        if self.budget:
            argv += ["--budget", str(self.budget)]
        self.started_at = time.time()
        self.proc = subprocess.Popen(
            argv, cwd=ROOT, env=RUN_ENV,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def reap_if_spinning(self) -> bool:
        """Kill a run that outlived the demo timescale without producing a result."""
        if (
            self.proc is not None
            and self.proc.poll() is None
            and self.result is None
            and time.time() - self.started_at > RUN_KILL_S
        ):
            self.proc.kill()
            self.error = "run killed: no verdict within the demo window"
            # An honest synthetic result: the search never went green.
            self.result = {
                "run_id": self.id, "prompt": self.prompt, "scenario": self.scenario,
                "green": False, "winner": "—", "score": 0.0,
                "reason": self.error, "nodes": None, "budget": {},
            }
            return True
        return False

    def bus_path(self) -> Path | None:
        ratchet_dir = self.dir / ".ratchet"
        if not ratchet_dir.is_dir():
            return None
        buses = sorted(ratchet_dir.glob("*.bus.jsonl"), key=lambda p: p.stat().st_mtime)
        return buses[-1] if buses else None

    def approve(self, allow: bool, reason: str = "ui") -> bool:
        if not self.approval_id:
            return False
        approvals = self.dir / ".ratchet" / "approvals"
        approvals.mkdir(parents=True, exist_ok=True)
        (approvals / f"{self.approval_id}.json").write_text(
            json.dumps({"allow": allow, "reason": reason})
        )
        return True

    def audit(self) -> str:
        try:
            out = subprocess.run(
                [RATCHET_PY, "-m", "ratchet.cli", "audit", "--repo", str(self.dir)],
                cwd=ROOT, capture_output=True, text=True, timeout=60,
            )
            return (out.stdout + out.stderr).strip()
        except Exception as e:  # audit is display-only; never fail the result on it
            return f"audit unavailable: {e}"


RUNS: dict[str, Run] = {}


@app.post("/api/create")
async def create(body: dict):
    prompt = str(body.get("prompt", "")).strip()
    budget = body.get("budget") or {}
    max_nodes = int(budget.get("max") or 0) or None
    run = Run(prompt, max_nodes)
    RUNS[run.id] = run
    try:
        await asyncio.to_thread(run.start)
    except Exception as e:
        return JSONResponse({"error": f"could not start run: {e}"}, status_code=500)
    return {"run_id": run.id, "scenario": run.scenario}


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def translate(run: Run, kind: str, p: dict, state: dict) -> list[tuple[str, dict]]:
    """One bus event -> zero or more SSE events. Pure translation, no judgement."""
    out: list[tuple[str, dict]] = []
    if kind == "run.started":
        out.append(("start", {
            "run_id": run.id,
            "slug": p.get("task", ""),
            "total": run.attempts,
            "scenario": run.scenario,
        }))
    elif kind == "verify.started":
        row = {
            "index": state["n"], "key": p["label"], "label": p.get("intent") or "attempt",
            "detail": f"node {p['label']} · parent {p.get('parent') or 'root'}",
            "layer": p.get("model", ""), "status": "active",
        }
        state["n"] += 1
        state["rows"][p["label"]] = row
        out.append(("stage", row))
    elif kind == "stage.result":
        line = f"{p['stage']}: {'ok' if p.get('passed') else p.get('detail') or 'fail'}"
        if p.get("skipped"):
            line = f"{p['stage']}: skipped"
        out.append(("log", {"key": p.get("label", ""), "line": line}))
    elif kind in ("node.added", "node.pruned"):
        # Bus nodes carry the intent, sandbox rows carry the label; pair them by
        # intent, newest active row first (repeated intents exist in some scenarios).
        row = None
        for r in reversed(list(state["rows"].values())):
            if r["status"] == "active" and r["label"] == (p.get("intent") or r["label"]):
                row = r
                break
        if row is None and p.get("id") != "root":
            return out
        if row is not None:
            status = "pruned" if kind == "node.pruned" else ("green" if p.get("green") else "done")
            row.update({
                "status": status, "score": p.get("score"), "outcome": p.get("outcome"),
                "findings": p.get("findings") or [], "reason": p.get("reason") or "",
            })
            out.append(("stage", row))
    elif kind == "docs.fetch":
        out.append(("log", {"key": "", "line": f"docs oracle: {p.get('library')} {p.get('version')} via {p.get('via')}"}))
    elif kind == "approval.required":
        run.approval_id = p.get("id")
        out.append(("approval", {
            "id": p.get("id"), "summary": p.get("summary"),
            "stats": p.get("stats") or {}, "diff_preview": p.get("diff_preview") or "",
        }))
    elif kind == "approval.resolved":
        run.decision = {"approved": bool(p.get("approved")), "reason": p.get("reason") or ""}
        out.append(("resolved", run.decision))
    elif kind == "run.done":
        run.result = {
            "run_id": run.id, "prompt": run.prompt, "scenario": run.scenario,
            "green": bool(p.get("green")), "winner": p.get("winner"),
            "score": p.get("score"), "reason": p.get("reason"),
            "nodes": p.get("nodes"), "budget": p.get("budget") or {},
        }
        out.append(("done", {"result": run.result}))
    return out


@app.get("/api/stream/{run_id}")
async def stream(run_id: str):
    run = RUNS.get(run_id)
    if run is None:
        return JSONResponse({"error": "unknown run"}, status_code=404)

    async def gen():
        state: dict = {"n": 0, "rows": {}}
        offset = 0
        buf = b""
        deadline = time.time() + 1200  # past the 900s approval window
        finished = False
        while time.time() < deadline and not finished:
            bus = run.bus_path()
            if bus is not None:
                # incremental byte reads: a spinning run writes megabytes fast,
                # and re-reading the whole file every tick would melt the loop
                with bus.open("rb") as f:
                    f.seek(offset)
                    chunk = f.read()
                offset += len(chunk)
                buf += chunk
                parts = buf.split(b"\n")
                buf = parts.pop()  # trailing partial line, if any
                for raw in parts:
                    line = raw.decode()
                    if not line.strip():
                        continue
                    ev = json.loads(line)
                    for name, data in translate(run, ev["kind"], ev.get("payload", {}), state):
                        yield sse(name, data)
                        # the stream is over once the gate resolves, or once a
                        # non-green run reports done (no gate follows)
                        if name == "resolved" or (name == "done" and not data["result"]["green"]):
                            finished = True
            if run.reap_if_spinning():
                yield sse("done", {"result": run.result})
                finished = True
            if run.proc is not None and run.proc.poll() is not None and run.result is not None:
                finished = True
            await asyncio.sleep(0.15)
        yield sse("eof", {})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@app.post("/api/approve/{run_id}")
async def approve(run_id: str, body: dict):
    run = RUNS.get(run_id)
    if run is None:
        return JSONResponse({"error": "unknown run"}, status_code=404)
    ok = run.approve(bool(body.get("allow")), str(body.get("reason", "ui")))
    if not ok:
        return JSONResponse({"error": "no approval pending"}, status_code=409)
    return {"ok": True}


@app.get("/api/result/{run_id}")
async def result(run_id: str):
    run = RUNS.get(run_id)
    if run is None:
        return JSONResponse({"error": "unknown run"}, status_code=404)
    if run.result is None:
        return JSONResponse({"error": "result not ready"}, status_code=404)
    audit = await asyncio.to_thread(run.audit)
    return {**run.result, "decision": run.decision, "audit": audit}


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# ---------------------------------------------------------------------------
# Qodo — live review data from this repository's real pull requests

_qodo_lock = threading.Lock()

CATEGORY_RE = re.compile(r"<code>\s*(?:[^\w<>\s]+\s*)?([A-Za-z][A-Za-z ]+?)\s*\((\d+)\)\s*</code>")


def _fetch_qodo() -> dict:
    prs = json.loads(subprocess.run(
        ["gh", "pr", "list", "--repo", GH_REPO, "--state", "all", "--limit", "30",
         "--json", "number,title,state,url"],
        capture_output=True, text=True, check=True, timeout=30,
    ).stdout)
    out = []
    for pr in prs:
        comments = json.loads(subprocess.run(
            ["gh", "api", f"repos/{GH_REPO}/issues/{pr['number']}/comments"],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout)
        reviews = []
        for c in comments:
            if c.get("user", {}).get("login") != "qodo-code-review[bot]":
                continue
            body = c.get("body", "")
            kind = "review" if "Code Review by Qodo" in body else (
                "summary" if "PR Summary by Qodo" in body else "comment")
            counts = {m.group(1).strip().lower(): int(m.group(2))
                      for m in CATEGORY_RE.finditer(body)}
            # Qodo edits its review comment in place on /review re-runs, so the
            # updated timestamp is the one that reflects the latest review pass.
            reviews.append({
                "kind": kind, "counts": counts,
                "at": c.get("updated_at") or c.get("created_at"),
            })
        if reviews:
            out.append({**pr, "reviews": reviews})
    return {"fetched_at": time.time(), "repo": GH_REPO, "prs": out}


def _qodo_cached() -> dict:
    with _qodo_lock:
        if QODO_CACHE.exists():
            cached = json.loads(QODO_CACHE.read_text())
            if time.time() - cached.get("fetched_at", 0) < QODO_TTL_S:
                return cached
        else:
            cached = None
        try:
            fresh = _fetch_qodo()
            QODO_CACHE.write_text(json.dumps(fresh, indent=2))
            return fresh
        except Exception as e:
            if cached is not None:
                return {**cached, "stale": True}
            return {"fetched_at": 0, "repo": GH_REPO, "prs": [], "error": str(e)}


@app.get("/api/qodo")
async def qodo():
    return await asyncio.to_thread(_qodo_cached)


@app.post("/api/qodo/rereview")
async def qodo_rereview(body: dict):
    """Command the hosted Qodo bot to re-review a PR ('/review' comment).

    This is Qodo's supported trigger — the Command CLI was discontinued upstream,
    so the Git-provider bot is the one review engine there is.
    """
    pr = int(body.get("pr") or 0)
    if not pr:
        return JSONResponse({"error": "pr required"}, status_code=400)
    try:
        out = subprocess.run(
            ["gh", "pr", "comment", str(pr), "--repo", GH_REPO, "--body", "/review"],
            capture_output=True, text=True, check=True, timeout=30,
        )
    except subprocess.CalledProcessError as e:
        return JSONResponse({"error": e.stderr.strip()}, status_code=502)
    # drop the cache so the panel picks up the fresh review on its next fetch
    QODO_CACHE.unlink(missing_ok=True)
    return {"ok": True, "comment_url": out.stdout.strip()}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)
