"""Qodo as MCP tools, against the reviewer that actually exists.

The Qodo *CLI* is discontinued -- run it and it prints a notice and exits -- so an
adapter that shells out to it would be theatre. The Qodo **GitHub App** is alive,
it has reviewed every pull request in this repository, and its findings are
retrievable over the GitHub API. That is what this talks to.

    review_pr(pr, wait)   ask for a review and wait for it to land
    fetch_findings(pr)    the findings it has already left, parsed

There is no scripted mode. When something is unreachable these raise, and the
caller reports it -- inventing an answer is the one thing a review gate must
never do.

Every call is recorded with its duration so a run can print its own evidence.
`gh` carries the credentials, so no token passes through this process.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Collection
from dataclasses import asdict, dataclass, field

from . import debuglog

SEVERITIES = ("critical", "high", "medium", "low")
QODO_BOT = "qodo-code-review"

#: Qodo leaves this heading on a pull request when a review finishes, whether or not
#: it found anything. It is the completion signal; the inline findings are its output.
#: Waiting on the findings alone cannot tell a clean review from a reviewer that never
#: answered, and those two must never be confused.
#:
#: On a re-review Qodo *edits* this comment in place rather than posting a second one
#: (measured on #17: same id, `updated_at` moved), so watching for a new id alone waits
#: forever on the second pass. What is watched is the (id, updated_at) pair.
REVIEW_DONE = "Code Review by Qodo"

#: How long a run waits for Qodo. Measured twice on this repository at ~104s; the
#: margin is for a queue, not for hope. Past it the gate raises rather than merging
#: unreviewed.
REVIEW_WAIT = 360.0

#: The summary lands a beat before the inline comments (observed ~1s apart). Reading
#: findings the instant the summary appears would report a clean review that is not.
SETTLE = 6.0

#: Qodo posts findings as HTML: a shields.io severity badge, a numbered title,
#: and the explanation inside a <pre>. This is how they actually arrive.
_SEV_BADGE = re.compile(r"badge/(Critical|High|Medium|Low)-", re.I)
_TITLE = re.compile(r"^\s*\d+\\?\.\s*(.+?)$", re.M)
_BODY = re.compile(r"<pre>\s*(.+?)\s*</pre>", re.S)


class QodoUnavailable(RuntimeError):
    pass


@dataclass
class Finding:
    severity: str
    title: str
    detail: str
    path: str = ""
    line: int = 0
    url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def blocking(self) -> bool:
        """Qodo's severity decides. Ratchet does not get to reinterpret it
        downward -- that is the only way a review gate means anything."""
        return self.severity in ("critical", "high")


@dataclass
class Review:
    findings: list[Finding] = field(default_factory=list)
    reviewer: str = "qodo"
    pr: str = ""
    calls: list[dict] = field(default_factory=list)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.blocking]

    @property
    def clean(self) -> bool:
        return not self.blocking


class QodoMCP:
    def __init__(self, repo_slug: str = "", *, timeout: float = 60.0) -> None:
        self.repo_slug = repo_slug
        self.timeout = timeout
        self.calls: list[dict] = []

    # ------------------------------------------------------------- plumbing --

    def _gh(self, *args: str):
        argv = ["gh", "api", *args]
        started = time.time()
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.SubprocessError) as e:
            raise QodoUnavailable(f"gh api failed to run: {e}") from e
        took = time.time() - started
        call = {"api": "github", "path": args[0] if args else "",
                "ok": proc.returncode == 0, "seconds": round(took, 2)}
        self.calls.append(call)
        debuglog.log("info", f"GET github {call['path']} → "
                             f"{'ok' if call['ok'] else 'error'} in {took:.2f}s")
        if proc.returncode != 0:
            raise QodoUnavailable((proc.stderr or proc.stdout).strip()[:200])
        raw = (proc.stdout or "").strip()
        try:
            return json.loads(raw or "null")
        except ValueError:
            return raw      # --jq can return a bare string; that is still an answer

    def _gh_list(self, path: str) -> list[dict]:
        """Every page of a list endpoint, not the first thirty.

        GitHub pages comments oldest-first, so on a busy pull request the newest ones
        -- the review we are waiting for -- are the ones that fall off the end. Reading
        page one would make the gate time out on a review that had already landed.
        `--slurp` gives one array per page; they flatten into one list.
        """
        pages = self._gh("--paginate", "--slurp", path)
        if not isinstance(pages, list):
            return []
        return [c for page in pages for c in (page if isinstance(page, list) else [page])]

    def available(self) -> bool:
        try:
            self._gh("user", "--jq", ".login")
            return True
        except QodoUnavailable:
            return False

    # ----------------------------------------------------------------- tools --

    def fetch_findings(self, pr: str, *, exclude: Collection[int] = ()) -> Review:
        """Qodo's findings on a pull request. `exclude` drops comment ids that were
        already there, which is how this run's review is told from an older one."""
        number = str(pr).lstrip("#")
        raw = self._gh_list(f"repos/{self.repo_slug}/pulls/{number}/comments")
        findings = [
            f for c in (raw or [])
            if int(c.get("id") or 0) not in exclude and (f := _parse_comment(c))
        ]
        return Review(findings=findings, pr=f"#{number}", calls=list(self.calls))

    def _bot_comments(self, number: str, *, endpoint: str, mark: str = "") -> set[tuple[int, str]]:
        """The bot's comments as (id, updated_at) pairs, optionally only those carrying
        `mark`. A pair is compared for equality, never for order -- so an edit counts as
        a change, and no clock skew or one-second tie can get the answer wrong."""
        raw = self._gh_list(f"repos/{self.repo_slug}/{endpoint}/{number}/comments")
        return {
            (int(c.get("id") or 0), str(c.get("updated_at", "")))
            for c in (raw or [])
            if QODO_BOT in str(c.get("user", {}).get("login", ""))
            and (not mark or mark in str(c.get("body", "")))
        }

    def review_pr(self, pr: str, *, wait: float = 0.0, poll: float = 10.0) -> Review:
        """Ask for a review and wait for THAT review.

        Two things this must not do. It must not accept an older review: a pull
        request Qodo saw yesterday answers in a second, and the run books yesterday's
        verdict against today's diff. And it must not read an empty result as a
        reviewer that never replied -- a genuinely clean review has no findings, and
        blocking on that would jam the gate shut on exactly the good pull requests.

        So the wait watches Qodo's own "review finished" comment rather than the
        findings, and it watches it as an (id, updated_at) pair tested for *equality*
        against a snapshot -- because a re-review edits that comment instead of adding
        one. Nothing here compares two clocks: a timestamp read as an opaque string
        cannot be skewed early or tied inside the same second.

        A wait that expires raises -- silence is not approval.
        """
        number = str(pr).lstrip("#")
        if not wait:
            return self.fetch_findings(number)

        seen_reviews = self._bot_comments(number, endpoint="issues", mark=REVIEW_DONE)
        seen_findings = {cid for cid, _ in self._bot_comments(number, endpoint="pulls")}
        self._gh("--method", "POST", f"repos/{self.repo_slug}/issues/{number}/comments",
                 "-f", "body=/review")
        debuglog.log("info", f"asked qodo to review #{number}; {len(seen_reviews)} prior review(s)")

        deadline = time.time() + wait
        while (remaining := deadline - time.time()) > 0:
            time.sleep(min(poll, remaining))
            if self._bot_comments(number, endpoint="issues", mark=REVIEW_DONE) - seen_reviews:
                time.sleep(SETTLE)  # let the inline findings catch up with the summary
                review = self.fetch_findings(number, exclude=seen_findings)
                debuglog.log("info", f"qodo reviewed #{number}: {len(review.findings)} finding(s)")
                return review
        raise QodoUnavailable(
            f"qodo did not answer for #{number} within {int(wait)}s — silence is not approval"
        )

    def review_diff(self, diff: str, *, context: str = "") -> Review:
        raise QodoUnavailable(
            "the Qodo app reviews pull requests, not loose diffs; open one and call review_pr"
        )

    # ------------------------------------------------------------------ mcp --

    def tools(self) -> list[dict]:
        return [
            {"name": "review_pr",
             "description": "Ask Qodo to review a pull request and return its findings.",
             "inputSchema": {"type": "object", "required": ["pr"],
                             "properties": {"pr": {"type": "string"},
                                            "wait": {"type": "number"}}}},
            {"name": "fetch_findings",
             "description": "The findings Qodo has already left on a pull request.",
             "inputSchema": {"type": "object", "required": ["pr"],
                             "properties": {"pr": {"type": "string"}}}},
        ]

    def call_tool(self, name: str, arguments: dict) -> dict:
        if name == "fetch_findings":
            review = self.fetch_findings(arguments["pr"])
        elif name == "review_pr":
            review = self.review_pr(arguments["pr"], wait=float(arguments.get("wait") or 0),
                                    poll=float(arguments.get("poll") or 10))
        else:
            raise KeyError(f"no tool named {name!r}")
        return {"findings": [f.to_dict() for f in review.findings],
                "blocking": len(review.blocking), "pr": review.pr, "calls": review.calls}


def _parse_comment(comment: dict) -> Finding | None:
    if QODO_BOT not in str(comment.get("user", {}).get("login", "")):
        return None
    body = str(comment.get("body", ""))
    sev = m.group(1).lower() if (m := _SEV_BADGE.search(body)) else "medium"
    plain = _strip_html(body)
    title = t.group(1).strip() if (t := _TITLE.search(plain)) else "finding"
    detail = " ".join(_strip_html(b.group(1)).split())[:400] if (b := _BODY.search(body)) else ""
    return Finding(
        severity=sev if sev in SEVERITIES else "medium",
        title=title[:120],
        detail=detail,
        path=str(comment.get("path", "")),
        line=int(comment.get("line") or comment.get("original_line") or 0),
        url=str(comment.get("html_url", "")),
    )


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)
