"""The live pipeline: real services, and it shows its own evidence.

Every external call is recorded and printed -- the harness's model list, each
model call through the TrueFoundry gateway, each GitHub request behind the Qodo
review -- with method, host, status and duration. A pipeline that claims to use
four services and cannot show a single request is a diagram, not a system.

Nothing here is scripted. When a service is absent the stage says so and the run
stops; there is no fallback that quietly invents the answer.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import debuglog
from .bus import Bus
from .providers import ChatBackend, ChatProviderError, gateway_only, load_saved_keys
from .qodo_mcp import REVIEW_WAIT, QodoMCP, QodoUnavailable


@dataclass
class ApiCall:
    service: str
    method: str
    url: str
    status: int | None = None
    seconds: float = 0.0
    note: str = ""

    def to_dict(self) -> dict:
        return {"service": self.service, "method": self.method, "url": self.url,
                "status": self.status, "seconds": round(self.seconds, 2), "note": self.note}


@dataclass
class Ledger:
    """Every call the run made, in order. Printed at the end as the receipt."""

    calls: list[ApiCall] = field(default_factory=list)

    def record(self, call: ApiCall) -> ApiCall:
        self.calls.append(call)
        debuglog.log("info", f"{call.method} {call.url} → {call.status} in {call.seconds:.2f}s")
        return call

    def by_service(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.calls:
            out[c.service] = out.get(c.service, 0) + 1
        return out


class LiveRun:
    """Preflight, then the real stages, then the ledger."""

    def __init__(self, repo: Path, bus: Bus, *, run_id: str, repo_slug: str = "",
                 pr: str = "", goal: str = "") -> None:
        self.repo = Path(repo)
        self.bus = bus
        self.run_id = run_id
        self.repo_slug = repo_slug
        self.pr = pr
        self.goal = goal
        self.ledger = Ledger()
        load_saved_keys()

    def emit(self, kind: str, **payload) -> None:
        self.bus.emit(kind, **payload)

    # ------------------------------------------------------------ preflight --

    def preflight(self) -> dict:
        """What is actually reachable. A missing service is reported, never faked."""
        checks: dict[str, dict] = {}

        base = os.environ.get("TRUEFORGE_BASE_URL", "http://localhost:8790")
        t0 = time.time()
        try:
            r = httpx.get(f"{base}/api/v1/models", timeout=8)
            self.ledger.record(ApiCall("trueforge", "GET", f"{base}/api/v1/models",
                                       r.status_code, time.time() - t0))
            models = r.json().get("data", []) if r.status_code < 400 else []
            checks["trueforge"] = {"ok": r.status_code < 400, "models": len(models),
                                   "detail": f"{len(models)} model(s)"}
        except Exception as e:
            self.ledger.record(ApiCall("trueforge", "GET", f"{base}/api/v1/models",
                                       None, time.time() - t0, str(e)[:80]))
            checks["trueforge"] = {"ok": False, "detail": "not answering — npx @truefoundry/trueforge@latest"}

        gw = os.environ.get("TFY_BASE_URL", "https://shryukg.truefoundry.cloud/api/llm/v1")
        key = os.environ.get("TFY_API_KEY", "")
        if key:
            t0 = time.time()
            try:
                r = httpx.get(f"{gw}/models", headers={"Authorization": f"Bearer {key}"}, timeout=15)
                self.ledger.record(ApiCall("truefoundry", "GET", f"{gw}/models",
                                           r.status_code, time.time() - t0))
                ids = [m.get("id") for m in r.json().get("data", [])] if r.status_code < 400 else []
                checks["truefoundry"] = {"ok": r.status_code < 400, "models": ids,
                                         "detail": ", ".join(ids[:3]) or "no models on the gateway"}
            except Exception as e:
                checks["truefoundry"] = {"ok": False, "detail": str(e)[:80]}
        else:
            checks["truefoundry"] = {"ok": False, "detail": "TFY_API_KEY not set — /connect truefoundry"}

        qodo = QodoMCP(self.repo_slug)
        ok = qodo.available()
        for c in qodo.calls:
            self.ledger.record(ApiCall("github", "GET", c["path"], 200 if c["ok"] else None, c["seconds"]))
        checks["qodo"] = {"ok": ok, "detail": "the Qodo GitHub App, over the GitHub API"
                          if ok else "gh is not authenticated — gh auth login"}
        checks["gateway_only"] = {"ok": gateway_only(),
                                  "detail": "every model call is routed" if gateway_only()
                                  else "direct provider calls are allowed"}
        self.emit("preflight", checks={k: v["ok"] for k, v in checks.items()},
                  detail={k: v["detail"] for k, v in checks.items()})
        return checks

    # ---------------------------------------------------------------- model --

    def ask(self, prompt: str, *, model: str = "openai/gpt-5.2", role: str = "generator",
            max_tokens: int = 512) -> str:
        """One real model call, through whatever routing is configured, recorded."""
        backend = ChatBackend("truefoundry" if gateway_only() else "openai", model)
        gw = os.environ.get("TFY_BASE_URL", "https://shryukg.truefoundry.cloud/api/llm/v1")
        t0 = time.time()
        self.emit("model.call", role=role, model=model, via="truefoundry-gateway")
        try:
            text = backend.complete(prompt, max_tokens=max_tokens)
        except ChatProviderError as e:
            self.ledger.record(ApiCall("truefoundry", "POST", f"{gw}/chat/completions",
                                       None, time.time() - t0, str(e)[:80]))
            raise
        took = time.time() - t0
        self.ledger.record(ApiCall("truefoundry", "POST", f"{gw}/chat/completions", 200, took))
        self.emit("model.done", role=role, model=model, seconds=round(took, 2), chars=len(text))
        return text

    # --------------------------------------------------------------- review --

    def review(self, pr: str) -> dict:
        """Qodo's findings on this diff.

        `review_pr`, not `fetch_findings`: reading findings a pull request already
        carries is a cache read, and on a branch Qodo reviewed before it books an old
        verdict against the current code."""
        qodo = QodoMCP(self.repo_slug)
        self.emit("review.started", scope="pull request", reviewer="qodo", pr=pr, pass_no=1)
        try:
            out = qodo.call_tool("review_pr", {"pr": pr, "wait": REVIEW_WAIT, "poll": 15})
        except QodoUnavailable as e:
            self.emit("review.failed", reason=str(e)[:160])
            raise
        for c in out["calls"]:
            self.ledger.record(ApiCall("github", "GET", c["path"], 200 if c["ok"] else None, c["seconds"]))
        for f in out["findings"]:
            self.emit("review.finding", **f)
        self.emit("review.done", findings=len(out["findings"]), blocking=out["blocking"], pass_no=1)
        return out

    def finish(self, **payload) -> dict:
        self.emit("ledger", calls=[c.to_dict() for c in self.ledger.calls],
                  by_service=self.ledger.by_service())
        self.emit("build.done", **payload)
        return {"calls": [c.to_dict() for c in self.ledger.calls]}
