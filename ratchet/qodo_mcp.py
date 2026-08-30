"""The QODO MCP server — hosted code review as four MCP tools, over stdio.

The Qodo Command CLI is discontinued upstream, so these tools drive the surface
that actually works: the ``qodo-code-review[bot]`` on this repository's GitHub
pull requests, via ``gh``. Register it once (``.mcp.json`` ships the entry) and
any MCP client can command a review, wait for it, and read the findings —
including the per-finding **Agent prompt** blocks Qodo writes for coding agents.

Tools never raise: a machine without ``gh`` or a repo without a GitHub remote
gets an explicit "QODO unavailable" string, because a tool error reads as the
server being broken and this server is not broken, just unplugged.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from .qodo import QodoOracle

mcp = MCPServer(
    "qodo",
    instructions=(
        "QODO hosted code review for this repository's GitHub pull requests. "
        "The Qodo Command CLI is discontinued upstream; these tools drive the working "
        "surface — the qodo-code-review[bot] — via gh. Reviews are advisory: in ratchet, "
        "only the verifier gauntlet decides green."
    ),
)


def _oracle() -> QodoOracle:
    return QodoOracle(Path.cwd())


def _unavailable(o: QodoOracle) -> str | None:
    if o.available():
        return None
    return ("QODO unavailable: needs `gh` on PATH and a GitHub origin remote "
            f"(slug={o.slug!r}).")


@mcp.tool()
def qodo_status() -> str:
    """QODO at a glance: repo, this branch's PR, and its latest review's counts."""
    o = _oracle()
    if (msg := _unavailable(o)) is not None:
        return msg
    pr = o.current_pr()
    if pr is None:
        return f"QODO: gh ok · repo {o.slug} · no open PR for the checked-out branch"
    review = o.latest_review(pr)
    tail = (f"last review {review.reviewed_at} · {len(review.open_findings)} open finding(s) · "
            f"{json.dumps(review.counts)}" if review else "no review yet — try qodo_request_review")
    return f"QODO: gh ok · repo {o.slug} · PR #{pr} · {tail}"


@mcp.tool()
def qodo_findings(pr: int) -> str:
    """The latest QODO review of a PR as JSON — findings include Qodo's own
    per-finding `agent_prompt`, written to be fed straight to a coding agent."""
    o = _oracle()
    if (msg := _unavailable(o)) is not None:
        return msg
    review = o.latest_review(pr, fresh=True)
    if review is None:
        return f"QODO: no review found on PR #{pr} — qodo_request_review first."
    return json.dumps(asdict(review), indent=2)


@mcp.tool()
def qodo_request_review(pr: int) -> str:
    """Command the hosted QODO bot to review a PR (posts `/review`). Posting is a
    remote change, so it waits for a yes at ratchet's approval gate first; the bot
    then ACKs in seconds and edits its full review into place ~2 minutes later."""
    o = _oracle()
    if (msg := _unavailable(o)) is not None:
        return msg
    if not o.trigger_review(pr):
        return f"QODO: no /review posted on PR #{pr} (denied at the approval gate, or gh failed)."
    return f"QODO: /review posted on PR #{pr} — poll with qodo_wait_review."


@mcp.tool()
def qodo_wait_review(pr: int, timeout_s: int = 240) -> str:
    """Trigger a QODO review if needed (via the approval gate), wait for the bot's
    pass, return it as JSON."""
    o = _oracle()
    if (msg := _unavailable(o)) is not None:
        return msg
    prev = o.latest_review(pr, fresh=True)
    since = prev.reviewed_at if prev else ""
    if not o.trigger_review(pr):
        return f"QODO: no /review posted on PR #{pr} (denied at the approval gate, or gh failed)."
    review = o.wait_for_review(pr, since=since, timeout_s=timeout_s)
    if review is None:
        return f"QODO: no review landed on PR #{pr} within {timeout_s}s."
    return json.dumps(asdict(review), indent=2)


def main() -> None:
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
