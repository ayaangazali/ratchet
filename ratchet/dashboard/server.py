"""A dashboard for a run, served from the standard library.

Same rule as the TUI: everything renders off the JSONL bus, so the dashboard can be
started, killed and restarted mid-run without touching the search, and a finished
run replays into it. It holds no state of its own -- refresh the page and the whole
run is rebuilt from the file.

Two decisions worth defending:

**No framework and no CDN.** The page is one HTML file with inline CSS and vanilla
JS, and the palette and the mascot are injected from `design` at request time so
the browser and the terminal cannot drift apart. A demo happens on conference wifi;
a dashboard that needs to reach a CDN is a dashboard that goes blank at the judging
table.

**The approve button writes the same file the TUI writes.** It does not call into
the search, and there is no second code path for approving things. `gate.Gate.wait`
polls one directory, and every front end -- TUI, browser, `echo` -- puts a file in
it. One gate, three ways to reach it.

Bound to loopback by default. It can resolve an approval, so it is not something to
put on an interface you do not control.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import design as m
from ..bus import Bus

HERE = Path(__file__).resolve().parent

#: Approval ids come from `uuid4().hex[:8]`. Anything else is not an id, and since
#: the id becomes a filename it does not get the benefit of the doubt.
SAFE_ID = re.compile(r"^[0-9a-f]{1,32}$")

MAX_BODY = 4096


def _remote_url(repo: Path) -> str:
    """The repo's browseable origin URL, or "" — so the page can link where a
    PR will land. Read-only; a repo with no remote just gets no link."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.startswith("git@") and ":" in out:
        host, _, rest = out.removeprefix("git@").partition(":")
        out = f"https://{host}/{rest}"
    return out.removesuffix(".git")


def _page(bus_path: Path, repo: Path) -> bytes:
    """The page, with the palette and the mascot injected from the TUI's module."""
    html = (HERE / "index.html").read_text()
    palette = "\n".join(f"      --{k}: {v};" for k, v in m.COLOURS.items())
    return (
        html.replace("/*__PALETTE__*/", palette)
        .replace("<!--__DOLPHIN__-->", m.to_svg(m.FIN, scale=1))
        .replace("<!--__DOLPHIN_TINY__-->", m.to_svg(m.FIN_TINY, scale=1))
        .replace("__RUN__", bus_path.stem.replace(".bus", ""))
        .replace("__REPO__", repo.name)
        .replace("__REPO_URL__", _remote_url(repo))
    ).encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "ratchet"
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, bus_path: Path, repo: Path, **kw) -> None:
        self.bus_path = bus_path
        self.repo = repo
        super().__init__(*args, **kw)

    # ------------------------------------------------------------------- util --

    def log_message(self, fmt: str, *args) -> None:  # pragma: no cover - noise
        """Silent. The dashboard runs next to a live console and a request log
        scrolling underneath it makes both unreadable."""

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -------------------------------------------------------------------- get --

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self._send(200, _page(self.bus_path, self.repo), "text/html; charset=utf-8")
        if path == "/health":
            return self._send(200, json.dumps({"ok": True, "bus": str(self.bus_path)}).encode())
        if path == "/events":
            return self._stream()
        self._send(404, b'{"error":"not found"}')

    def _stream(self) -> None:
        """Server-sent events, replayed from byte zero.

        A reader that connects late still gets the whole run, because the bus is a
        file and the offset starts at zero -- which is the same property that lets
        the TUI be restarted mid-run.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        bus = Bus(self.bus_path)
        idle = 0.0
        try:
            while True:
                sent = False
                for ev in bus.tail():
                    payload = json.dumps({"kind": ev.kind, "payload": ev.payload, "ts": ev.ts})
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    sent = True
                if sent:
                    self.wfile.flush()
                    idle = 0.0
                else:
                    idle += 0.25
                    if idle >= 15:  # a comment frame, so proxies do not time the stream out
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        idle = 0.0
                time.sleep(0.25)
        except (BrokenPipeError, ConnectionResetError):
            return  # the tab was closed; that is not an error

    # ------------------------------------------------------------------- post --

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/approve":
            return self._send(404, b'{"error":"not found"}')
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._send(400, b'{"error":"bad length"}')
        if length > MAX_BODY:
            return self._send(413, b'{"error":"too large"}')
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, b'{"error":"bad json"}')

        request_id = str(body.get("id", ""))
        if not SAFE_ID.match(request_id):
            return self._send(400, b'{"error":"bad id"}')

        allow = bool(body.get("allow"))
        # The decision travels as a file, exactly as it does from the TUI and from
        # the documented `echo` fallback. `gate.Gate.wait` is the only reader.
        approvals = self.repo / ".ratchet" / "approvals"
        approvals.mkdir(parents=True, exist_ok=True)
        (approvals / f"{request_id}.json").write_text(
            json.dumps({"allow": allow, "reason": "" if allow else "denied at the dashboard"})
        )
        self._send(200, json.dumps({"ok": True, "id": request_id, "allow": allow}).encode())


def serve(bus_path: Path, repo: Path, *, host: str = "127.0.0.1", port: int = 8788) -> None:
    """Block, serving the dashboard. Threaded because SSE holds a connection open
    for the whole run and a single-threaded server would then answer nothing else."""
    handler = partial(Handler, bus_path=Path(bus_path), repo=Path(repo))
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    print(f"ratchet dashboard  http://{host}:{port}")
    print(f"  bus   {bus_path}")
    print(f"  repo  {repo}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
