"""Papers via Bright Data, with the extractors in git.

Research mode does not talk to the arXiv or Hugging Face APIs. That is a deliberate
trade and worth stating plainly, because the APIs are perfectly good:

* An API is a second integration surface with its own auth story, its own rate
  limits and its own outage. Ratchet already has exactly one way of reaching the
  outside world -- Bright Data, configured in `scrapers.yaml` -- and a project that
  reaches the web two different ways has two things to fix when the web moves.
* The pages carry more than the APIs expose, and they carry it in the same shape a
  human sees, which is the shape the extractors are written against.
* Most of all: a scraper is a thing that *breaks*, and the interesting engineering
  is what happens when it does. An API that returns clean JSON teaches nobody
  anything about that.

So the pipeline is the same one the docs oracle uses, pointed at a different corner
of the web:

    scrapers.yaml (in git)  ->  Bright Data CLI  ->  Web Unlocker REST (fallback)
                            ->  extract by labelled block, not by CSS selector
                            ->  validate against `expect`
                            ->  on failure, widen and record the repair as a diff

Extracting by the `arXiv:<id>` marker rather than by a class name is the cheapest
resilience available. arXiv can restyle its search page freely; the day it stops
printing an arXiv id next to each result is the day it has stopped being arXiv.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .sources import Paper

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

UNLOCKER_URL = "https://api.brightdata.com/request"

#: Interstitials. If one of these is in the body, we did not get the page -- and a
#: parser that runs anyway will report "0 papers found" as though the topic were
#: obscure rather than the fetch broken.
BLOCKED_MARKERS = (
    "Just a moment", "Enable JavaScript", "Access denied", "captcha",
    "Verifying you are human",
    # Bright Data's own refusal, which arrives as a 200 with a short body -- so it
    # has to be detected here or it parses as "this topic has no papers".
    "Failed (bad_endpoint)", "not available for immediate residential",
)


@dataclass
class Fetched:
    ok: bool
    url: str
    text: str = ""
    via: str = ""
    problems: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# text normalisation
# --------------------------------------------------------------------------- #

_MD_LINK = re.compile(r"!?\[([^\]]*)\]\(([^)]*)\)", re.S)
_SCRIPT = re.compile(r"(?is)<(script|style)\b.*?</\1>")

#: Tags that do not break a line. arXiv's search page wraps every matched query
#: term in a highlight span, so turning *every* tag into a newline shatters exactly
#: the titles you searched for: "Hack-Verifiable Terminal Bench" arrives as "Hack",
#: "-Verifiable Terminal Bench" and "Hacking" on three separate lines, and the
#: parser picks whichever fragment is longest. Inline tags are dropped in place.
_INLINE = re.compile(r"(?is)</?(span|em|strong|b|i|u|mark|small|sub|sup|code|a|font|abbr|cite|q)\b[^>]*>")
_TAG = re.compile(r"(?s)<[^>]+>")


def to_text(body: str) -> str:
    """Markdown or HTML in, plain lines out.

    Bright Data returns markdown; the fallback surfaces sometimes return HTML. The
    extractors are written against text so that either is acceptable, which means
    one parser instead of two and one thing to fix instead of two.
    """
    if "<" in body and re.search(r"<(html|body|div|li)\b", body, re.I):
        body = _SCRIPT.sub(" ", body)
        body = _INLINE.sub("", body)
        body = _TAG.sub("\n", body)
    # Keep the target when the link text is empty. Hugging Face's listing marks
    # each paper with a bare image link -- `![](/papers/2608.25518)` -- and the id
    # in that target is the only thing identifying the entry. Collapsing it to the
    # (empty) link text deletes every id on the page and the parser then reports
    # "no papers found" for a page full of papers.
    body = _MD_LINK.sub(lambda m: m.group(1).strip() or m.group(2), body)
    body = body.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
    body = body.replace("&lt;", "<").replace("&gt;", ">").replace("&nbsp;", " ")
    lines = [ln.strip(" \t*#>") for ln in body.splitlines()]
    return "\n".join(ln for ln in lines if ln)


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #

DEFAULT_PARSE: dict[str, Any] = {
    "id_pattern": r"(?:arXiv:|/papers/|/abs/)(\d{4}\.\d{4,5})",
    "authors_label": r"^Authors?\s*:?\s*$|^Authors?\s*:",
    "abstract_label": r"^Abstract\s*:?\s*$|^Abstract\s*:",
    "drop_patterns": [
        # The format links, whether they arrive one per line or collapsed into one
        # by inline-tag stripping: "[pdf, ps, other]".
        r"^\[?\s*(pdf|ps|other|html|v\d+)(\s*,\s*(pdf|ps|other|html|v\d+))*\s*\]?\s*[,;]?\s*$",
        # Subject classes, alone or several to a line: "cs.LG", "cs.LG cs.AI stat.ML".
        r"^(?:[a-z-]+\.[A-Z]{2}[\s;,]*)+$",
        r"^[\[\],;:]+$",
        r"^(Submitted|Comments|Journal ref|doi|Report number|MSC class|ACM class)\b",
        r"^▽?\s*(More|Less)$",
        r"^\d+$",
        r"^…$",
    ],
    "max_authors": 12,
}


def _compiled(patterns: list[str]) -> list[re.Pattern]:
    out = []
    for p in patterns:
        try:
            out.append(re.compile(p))
        except re.error:
            continue
    return out


def parse_papers(text: str, cfg: dict[str, Any] | None = None, *, source: str = "arxiv") -> list[Paper]:
    """Split a scraped listing into papers on the arXiv id marker.

    Blocks, not selectors. Every result on either site prints an arXiv id, and the
    id is the only thing on the page guaranteed not to be restyled away.
    """
    c = {**DEFAULT_PARSE, **(cfg or {})}
    ident = re.compile(c["id_pattern"])
    authors_label = re.compile(c["authors_label"], re.I)
    abstract_label = re.compile(c["abstract_label"], re.I)
    drops = _compiled(list(c["drop_patterns"]))

    lines = text.splitlines()
    starts = [(i, m.group(1)) for i, ln in enumerate(lines) for m in [ident.search(ln)] if m]
    if not starts:
        return []

    seen: dict[str, Paper] = {}
    for n, (start, pid) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        block = [ln for ln in lines[start + 1 : end] if not any(d.match(ln) for d in drops)]
        title, authors, abstract = _split_block(block, authors_label, abstract_label, int(c["max_authors"]))
        if not title:
            continue
        paper = Paper(
            id=pid,
            title=title,
            abstract=abstract,
            authors=authors,
            published="",
            url=f"https://arxiv.org/abs/{pid}",
            source=source,
        )
        prev = seen.get(pid)
        # A listing page prints a truncated abstract and then the full one. Keep the
        # longer text rather than whichever happened to be parsed last.
        if prev is None or len(paper.abstract) > len(prev.abstract):
            if prev is not None and not paper.authors:
                paper.authors = prev.authors
            seen[pid] = paper
    return list(seen.values())


def _split_block(
    block: list[str], authors_label: re.Pattern, abstract_label: re.Pattern, max_authors: int
) -> tuple[str, list[str], str]:
    """Title / authors / abstract out of one result block, by labels."""
    a_idx = next((i for i, ln in enumerate(block) if authors_label.search(ln)), None)
    b_idx = next((i for i, ln in enumerate(block) if abstract_label.search(ln)), None)

    head_end = a_idx if a_idx is not None else (b_idx if b_idx is not None else len(block))
    # The title is the first line in the block long enough not to be a category tag
    # like "cs.LG"; those sit between the id and the title on arXiv's listing.
    title = next((ln for ln in block[:head_end] if len(ln) > 12 and not re.fullmatch(r"[a-zA-Z.\-]{2,12}", ln)), "")

    authors: list[str] = []
    if a_idx is not None:
        tail = re.sub(r"^Authors?\s*:?\s*", "", block[a_idx], flags=re.I).strip()
        raw = [tail] if tail else []
        raw += block[a_idx + 1 : (b_idx if b_idx is not None else len(block))]
        for chunk in raw:
            for name in re.split(r"\s*[,;]\s*", chunk):
                name = name.strip()
                if name and len(name) < 60 and not abstract_label.search(name):
                    authors.append(name)
    authors = authors[:max_authors]

    abstract = ""
    if b_idx is not None:
        rest = re.sub(r"^Abstract\s*:?\s*", "", block[b_idx], flags=re.I).strip()
        parts = ([rest] if rest else []) + block[b_idx + 1 :]
        abstract = re.sub(r"\s+", " ", " ".join(parts)).strip()
    return title.strip(), authors, abstract


# --------------------------------------------------------------------------- #
# the scraper
# --------------------------------------------------------------------------- #


class PaperScraper:
    """Fetch and parse paper listings, with the config in `scrapers.yaml`."""

    def __init__(self, settings: Any, bus: Any = None, *, config_path: Path | None = None) -> None:
        self.s = settings
        self.bus = bus
        self.config_path = Path(config_path or getattr(settings, "scrapers_path", None) or "ratchet/scrapers.yaml")
        self._cfg: dict | None = None

    # ------------------------------------------------------------- config --

    def config(self) -> dict:
        if self._cfg is None:
            if yaml is None or not self.config_path.exists():
                self._cfg = {}
            else:
                self._cfg = yaml.safe_load(self.config_path.read_text()) or {}
        return self._cfg

    def sources(self) -> dict:
        return (self.config().get("papers") or {}).get("sources") or {}

    def _save_config(self) -> None:
        if yaml is None or self._cfg is None:
            return
        self.config_path.write_text(yaml.safe_dump(self._cfg, sort_keys=False, allow_unicode=True))

    # ------------------------------------------------------------ transport --

    def _fetch_cli(self, url: str) -> Fetched:
        exe = shutil.which("brightdata") or shutil.which("bdata")
        if not exe:
            return Fetched(False, url, via="cli", problems=["brightdata CLI not on PATH"])
        try:
            proc = subprocess.run([exe, "scrape", url, "-f", "markdown"], capture_output=True, text=True, timeout=120)
        except (subprocess.TimeoutExpired, OSError) as e:
            return Fetched(False, url, via="cli", problems=[str(e)[:160]])
        if proc.returncode != 0:
            return Fetched(False, url, via="cli", problems=[proc.stderr.strip()[:200]])
        return Fetched(True, url, proc.stdout, "cli")

    def _fetch_unlocker(self, url: str, zone: str = "") -> Fetched:
        key = getattr(self.s, "brightdata_api_key", None)
        if not key:
            return Fetched(False, url, via="unlocker", problems=["BRIGHTDATA_API_KEY not set"])
        body = json.dumps({
            "zone": zone or getattr(self.s, "brightdata_unlocker_zone", "mcp_unlocker"),
            "url": url,
            "format": "raw",
            "data_format": "markdown",
        }).encode()
        req = urllib.request.Request(
            UNLOCKER_URL, data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310 - fixed host
                return Fetched(True, url, r.read().decode("utf-8", "replace"), "unlocker")
        except (urllib.error.URLError, TimeoutError) as e:  # pragma: no cover - network
            return Fetched(False, url, via="unlocker", problems=[str(e)[:200]])

    def fetch(self, url: str, *, zones: list[str] | None = None) -> Fetched:
        """CLI first, then each configured zone in turn.

        Zones are per-source because Bright Data's access policy is per-host: the
        unblocker zone serves huggingface.co happily and refuses arxiv.org outright
        ("not available for immediate residential (no KYC) access mode in
        accordance with robots.txt"), while an ISP zone serves it. Hard-coding one
        zone means one of the two sources is always broken, so the zone list lives
        beside the URL in `scrapers.yaml` where it can be changed without a deploy.
        """
        got = self._fetch_cli(url)
        if got.ok and not self._blocked(got.text):
            return got
        problems = list(got.problems)
        for zone in (zones or [""]):
            got = self._fetch_unlocker(url, zone)
            if got.ok and self._blocked(got.text):
                got.ok = False
                got.problems.append(f"zone {zone or 'default'}: {self._why_blocked(got.text)}")
            if got.ok:
                got.problems = problems + got.problems
                return got
            problems += got.problems
        got.problems = problems
        return got

    @staticmethod
    def _why_blocked(text: str) -> str:
        return re.sub(r"\s+", " ", text[:200]).strip()

    @staticmethod
    def _blocked(text: str) -> bool:
        head = text[:4000]
        return any(m.lower() in head.lower() for m in BLOCKED_MARKERS)

    # -------------------------------------------------------------- search --

    def search(self, query: str, *, limit: int = 8, source: str = "arxiv") -> tuple[list[Paper], list[str]]:
        """Papers for a topic, plus whatever went wrong getting them.

        Problems are returned rather than raised: a partial reading list is useful,
        and the caller is usually mid-run. They are also what drives the repair.
        """
        cfg = self.sources().get(source)
        if not cfg:
            return [], [f"no paper source {source!r} in {self.config_path}"]

        url = str(cfg["url"]).format(query=urllib.parse.quote_plus(query), limit=limit)
        got = self.fetch(url, zones=list(cfg.get("zones") or []))
        if not got.ok:
            return [], got.problems

        text = to_text(got.text)
        papers = parse_papers(text, cfg.get("parse"), source=source)
        problems = self._validate(papers, text, cfg.get("expect") or {})

        if problems:
            papers, repaired = self._repair(source, cfg, text, problems)
            if repaired:
                problems = [f"repaired: {p}" for p in problems]

        if self.bus is not None:
            self.bus.emit("docs.fetch", library=f"papers/{source}", version=query, via=got.via)
        return papers[:limit], problems

    def _validate(self, papers: list[Paper], text: str, expect: dict) -> list[str]:
        problems: list[str] = []
        if len(papers) < int(expect.get("min_entries", 1)):
            problems.append(f"only {len(papers)} entries parsed, expected at least {expect.get('min_entries', 1)}")
        if len(text) < int(expect.get("min_chars", 200)):
            problems.append(f"page body was {len(text)} chars, expected at least {expect.get('min_chars', 200)}")
        for marker in expect.get("must_contain", []):
            if marker.lower() not in text.lower():
                problems.append(f"missing marker {marker!r}")
        titled = [p for p in papers if p.title]
        if papers and len(titled) < len(papers) * 0.6:
            problems.append("most entries parsed without a title; the block layout has probably moved")
        return problems

    def enrich(self, paper: Paper) -> Paper:
        """Fill in an abstract from the paper's own arXiv page.

        Hugging Face's listing carries a title and an upvote count and nothing else,
        which is enough to rank a reading list and nowhere near enough to distil a
        technique from. So enrichment is on demand: one extra fetch, for the handful
        of papers actually being distilled, rather than for the whole listing.
        """
        if paper.abstract and len(paper.abstract) > 200:
            return paper
        cfg = self.sources().get("arxiv_abs")
        if not cfg:
            return paper
        got = self.fetch(str(cfg["url"]).format(id=paper.id), zones=list(cfg.get("zones") or []))
        if not got.ok:
            return paper
        text = to_text(got.text)
        m = re.search(r"^Abstract\s*:?\s*$([\s\S]{40,4000}?)(?=^(Comments|Subjects|Cite as|Submission history)\b)",
                      text, re.M)
        if not m:
            m = re.search(r"Abstract\s*:?\s*([\s\S]{40,4000}?)(?=(Comments|Subjects|Cite as)\b)", text)
        if m:
            paper.abstract = re.sub(r"\s+", " ", m.group(1)).strip()
        if not paper.authors:
            a = re.search(r"^Authors?\s*:?\s*$([\s\S]{0,600}?)(?=^(Abstract|Comments|Subjects)\b)", text, re.M)
            if a:
                paper.authors = [n.strip() for n in re.split(r"[,;\n]", a.group(1)) if 1 < len(n.strip()) < 60][:12]
        return paper

    # -------------------------------------------------------------- repair --

    def _repair(self, source: str, cfg: dict, text: str, problems: list[str]) -> tuple[list[Paper], bool]:
        """Widen the extractor and try again, then write the change back to git.

        The repair is deliberately narrow: relax the labels that identify the
        author and abstract blocks, because those are the parts a redesign renames.
        The id marker is never relaxed -- if that is gone we did not fetch a paper
        listing at all, and guessing harder would only manufacture nonsense.
        """
        widened = {
            **DEFAULT_PARSE,
            **(cfg.get("parse") or {}),
            "authors_label": r"^(Authors?|By)\b\s*:?",
            "abstract_label": r"^(Abstract|Summary|TL;DR)\b\s*:?",
        }
        papers = parse_papers(text, widened, source=source)
        if not papers:
            return [], False

        cfg.setdefault("parse", {}).update(
            {"authors_label": widened["authors_label"], "abstract_label": widened["abstract_label"]}
        )
        cfg.setdefault("history", []).append({
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "why": "; ".join(problems)[:200],
            "change": "relaxed the authors and abstract labels",
            "entries_after": len(papers),
        })
        self._save_config()
        if self.bus is not None:
            self.bus.emit(
                "docs.heal", library=f"papers/{source}",
                old_section="strict labels", new_section="relaxed labels",
            )
        return papers, True
