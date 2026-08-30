"""Qodo hosted code review, wrapped for the loop and the MCP server.

The Qodo Command CLI is discontinued upstream (the server refuses and points at
the Git-provider bot), so the one working Qodo surface is the hosted
``qodo-code-review[bot]`` on this repository's GitHub pull requests: trigger a
pass by commenting ``/review``, read the result back off the PR's comments. All
of that goes through ``gh`` — argv lists only, never a shell.

Advisory by design: nothing in this module can set ``green`` (CLAUDE.md
invariant 1). Findings feed prompts and consoles; the gauntlet stays the only
judge. The parsing here is ported from the battle-tested constellation gateway
(``ui/gateway.py``) rather than re-derived.

Wire facts the code leans on, verified against the live bot:
- The bot **edits its review comment in place** on ``/review`` re-runs, so the
  comment's ``updated_at`` is the timestamp of the latest pass.
- ~7s after ``/review`` it ACK-edits ("Qodo is busy working"); the full review
  lands ~2 minutes later. A waiter must not accept the ACK.
- Each finding carries an **Agent prompt** block — Qodo's own instruction to a
  coding agent. That block is the payload ``qodo-fix`` runs on.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

BOT_LOGIN = "qodo-code-review[bot]"
REVIEW_MARK = "Code Review by Qodo"

# Re-exported so consumers import the kinds from one place; the canonical
# constants live in bus.py next to the rest of the renderer contract.
QODO_REQUESTED = "qodo.review.requested"
QODO_DONE = "qodo.review.done"

# ---- parsing, ported from ui/gateway.py -------------------------------------
# The headline is the first match per category; later <code>N (x)</code>
# occurrences deeper in the body must not overwrite it.
CATEGORY_RE = re.compile(r"<code>\s*(?:[^\w<>\s]+\s*)?([A-Za-z][A-Za-z ]+?)\s*\((\d+)\)\s*</code>")
FINDING_RE = re.compile(
    r"<summary>\s*(\d+)\.\s+(.*?)((?:\s*<code>[^<]*</code>)+)\s*</summary>", re.S
)
TAG_RE = re.compile(r"<code>\s*(?:[^\w<>\s]+\s*)?([^<]*?)\s*</code>")
PRE_RE = re.compile(r"<pre>(.*?)</pre>", re.S)
# inside the blockquoted 'Agent prompt' details, every line is '>'-prefixed
PROMPT_RE = re.compile(r"(The issue below was found.*?)>?\s*```", re.S)

_GH_TIMEOUT = 30


@dataclass
class QodoFinding:
    n: int
    title: str
    tags: list[str]
    description: str
    #: Qodo's own instruction to a coding agent — the qodo-fix payload
    agent_prompt: str
    #: the bot's own ``✓ Resolved`` tag; the finding is history, not work
    resolved: bool = False


@dataclass
class QodoReview:
    pr: int
    reviewed_at: str
    findings: list[QodoFinding] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def open_findings(self) -> list[QodoFinding]:
        """Findings still outstanding — what any caller acting on a review wants.

        The bot never drops a finding it considers fixed; it strikes the title
        through and re-tags it ``✓ Resolved``, agent prompt and all. Counting or
        re-running those prompts makes a fixed PR look unfixed and asks the model
        to redo (or undo) work that already landed.
        """
        return [f for f in self.findings if not f.resolved]


def _clean(html: str) -> str:
    text = re.sub(r"<[^>]+>", "", html)
    text = re.sub(r"^>\s?", "", text, flags=re.M)
    for ent, ch in (("&#x27;", "'"), ("&quot;", '"'), ("&lt;", "<"),
                    ("&gt;", ">"), ("&amp;", "&")):
        text = text.replace(ent, ch)
    return re.sub(r"\s+", " ", text).strip()


def parse_counts(body: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for m in CATEGORY_RE.finditer(body):
        counts.setdefault(m.group(1).strip().lower(), int(m.group(2)))
    return counts


def parse_findings(body: str) -> list[QodoFinding]:
    """Each numbered finding, with the Agent prompt Qodo embeds per finding."""
    findings: list[QodoFinding] = []
    seen: set[str] = set()
    matches = list(FINDING_RE.finditer(body))
    for i, m in enumerate(matches):
        title = _clean(m.group(2))
        # the body repeats finding summaries in a later section; first one wins
        if title in seen:
            continue
        seen.add(title)
        chunk = body[m.end(): matches[i + 1].start() if i + 1 < len(matches) else len(body)]
        pres = PRE_RE.findall(chunk)
        prompt = PROMPT_RE.search(chunk)
        tags = [t for t in TAG_RE.findall(m.group(3)) if t]
        findings.append(QodoFinding(
            n=int(m.group(1)),
            title=title,
            tags=tags,
            description=_clean(pres[0]) if pres else "",
            agent_prompt=re.sub(r"^>\s?", "", prompt.group(1), flags=re.M).strip()
            if prompt else "",
            resolved=any(t.lower() == "resolved" for t in tags),
        ))
    return findings


def repo_slug(repo: Path) -> str | None:
    """`owner/name` from the repo's origin remote, or None. Never raises."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=_GH_TIMEOUT,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    if out.startswith("git@") and ":" in out:
        out = out.removeprefix("git@").split(":", 1)[1]
    else:
        m = re.search(r"[\w.-]+/[\w.-]+?(?:\.git)?/?$", out)
        out = m.group(0) if m else ""
    out = out.removesuffix("/").removesuffix(".git")
    return out if out.count("/") == 1 else None


class QodoOracle:
    """The hosted reviewer as an optional loop collaborator (DocsOracle shape).

    Every public method degrades to None/""/stale-cache on any failure: a repo
    with no remote, no gh, or no network is a clean no-op, never a traceback.
    """

    def __init__(self, repo: Path, bus=None, *, slug: str | None = None, ttl_s: int = 600) -> None:
        self.repo = Path(repo)
        self.bus = bus
        self.slug = slug or repo_slug(self.repo)
        self.ttl_s = ttl_s
        self.cache_path = self.repo / ".ratchet" / "qodo_cache.json"

    # ----------------------------------------------------------------- state --

    def available(self) -> bool:
        return bool(self.slug) and shutil.which("gh") is not None

    def _gh(self, *argv: str) -> str | None:
        try:
            r = subprocess.run(["gh", *argv], capture_output=True, text=True, timeout=_GH_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout if r.returncode == 0 else None

    def _emit(self, kind: str, **payload) -> None:
        if self.bus is not None:
            self.bus.emit(kind, **payload)

    def open_pr(self) -> int | None:
        out = self._gh("pr", "list", "--repo", str(self.slug), "--state", "open",
                       "--limit", "1", "--json", "number")
        if not out:
            return None
        try:
            rows = json.loads(out)
            return int(rows[0]["number"]) if rows else None
        except (ValueError, KeyError, IndexError, TypeError):
            return None

    # --------------------------------------------------------------- reviews --

    def _latest_review_comment(self, pr: int) -> tuple[str, str] | None:
        """(body, updated_at) of the bot's review comment, or None."""
        out = self._gh("api", f"repos/{self.slug}/issues/{pr}/comments")
        if not out:
            return None
        try:
            comments = json.loads(out)
        except ValueError:
            return None
        reviews = [c for c in comments
                   if c.get("user", {}).get("login") == BOT_LOGIN
                   and REVIEW_MARK in c.get("body", "")]
        latest = max(reviews, key=lambda c: c.get("updated_at") or "", default=None)
        if latest is None:
            return None
        return latest.get("body", ""), latest.get("updated_at") or latest.get("created_at") or ""

    def latest_review(self, pr: int, *, fresh: bool = False) -> QodoReview | None:
        if not fresh:
            cached = self._cache_get(pr)
            if cached is not None:
                return cached
        if not self.available():
            return self._cache_get(pr, stale_ok=True)
        got = self._latest_review_comment(pr)
        if got is None:
            return self._cache_get(pr, stale_ok=True)
        body, at = got
        review = QodoReview(pr=pr, reviewed_at=at,
                            findings=parse_findings(body), counts=parse_counts(body))
        self._cache_put(review)
        return review

    def trigger_review(self, pr: int) -> bool:
        """Post ``/review`` — Qodo's supported trigger since the CLI's discontinuation."""
        out = self._gh("pr", "comment", str(pr), "--repo", str(self.slug), "--body", "/review")
        ok = out is not None
        if ok:
            self._emit(QODO_REQUESTED, pr=pr, slug=self.slug)
            self.cache_path.unlink(missing_ok=True)
        return ok

    def wait_for_review(self, pr: int, *, since: str, timeout_s: float = 240,
                        poll_s: float = 10) -> QodoReview | None:
        """Poll until the bot's in-place edit lands, then parse it.

        Accept only a comment newer than ``since`` whose body actually parses —
        the ~7s ACK edit ("busy working") has no findings and no category
        counts, so it is skipped.

        Returns None on timeout, never the previous pass: a caller that cannot
        tell "no review landed" from "the review is clean" reports a failed
        review as a green one.
        # ponytail: marker+parse heuristic; if Qodo's ACK wording ever gains a
        # parseable shape, tighten this to an explicit completion marker.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            got = self._latest_review_comment(pr)
            if got is not None:
                body, at = got
                if at > since and (FINDING_RE.search(body) or parse_counts(body)):
                    review = QodoReview(pr=pr, reviewed_at=at,
                                        findings=parse_findings(body),
                                        counts=parse_counts(body))
                    self._cache_put(review)
                    self._emit(QODO_DONE, pr=pr, reviewed_at=at,
                               counts=review.counts,
                               findings=[f.title for f in review.open_findings])
                    return review
            time.sleep(poll_s)
        return None

    # ---------------------------------------------------------------- prompt --

    def findings_for_prompt(self, pr: int | None = None, *, cap: int = 2000) -> str:
        """The latest review as compact prompt text, or "" when anything is missing."""
        if not self.available():
            return ""
        pr = pr or self.open_pr()
        if not pr:
            return ""
        review = self.latest_review(pr)
        if review is None or not review.open_findings:
            return ""
        lines = [f"PR #{review.pr}, reviewed {review.reviewed_at}:"]
        for f in review.open_findings:
            lines.append(f"{f.n}. {f.title} [{', '.join(f.tags)}]")
        # the top findings' own agent prompts, while the budget lasts
        for f in review.open_findings:
            if not f.agent_prompt:
                continue
            block = f"\nAgent prompt for finding {f.n}:\n{f.agent_prompt}"
            if sum(len(x) for x in lines) + len(block) > cap:
                break
            lines.append(block)
        return "\n".join(lines)[:cap]

    # ----------------------------------------------------------------- cache --

    def _cache_load(self) -> dict:
        try:
            return json.loads(self.cache_path.read_text())
        except (OSError, ValueError):
            return {}

    def _cache_get(self, pr: int, *, stale_ok: bool = False) -> QodoReview | None:
        data = self._cache_load().get(str(pr))
        if not data:
            return None
        if not stale_ok and time.time() - data.get("fetched_at", 0) > self.ttl_s:
            return None
        r = data.get("review") or {}
        try:
            return QodoReview(pr=r["pr"], reviewed_at=r["reviewed_at"],
                              findings=[QodoFinding(**f) for f in r.get("findings", [])],
                              counts=r.get("counts", {}))
        except (KeyError, TypeError):
            return None

    def _cache_put(self, review: QodoReview) -> None:
        try:
            cache = self._cache_load()
            cache[str(review.pr)] = {"fetched_at": time.time(), "review": asdict(review)}
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(cache, indent=2))
        except OSError:
            pass  # a cache that cannot be written is a cache, not an error


def oracle_or_none(settings, repo: Path, bus=None) -> QodoOracle | None:
    """Optional-collaborator factory, the `_docs_oracle` shape: None unless the
    feature is on, gh is on PATH, and the repo has a GitHub remote."""
    if not getattr(settings, "qodo", True):
        return None
    oracle = QodoOracle(Path(repo), bus)
    return oracle if oracle.available() else None
