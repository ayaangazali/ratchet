"""The `Paper` record, the on-disk cache, and relevance ranking.

There is no HTTP in this module any more, and that is the point. Research mode used
to call the arXiv and Hugging Face APIs directly; it now goes through Bright Data
like every other outside-world read in this project (`research/scrape.py`). One way
out means one thing to configure, one thing to authenticate, and one thing to fix
when the web moves.

What stays here is everything that is a pure function of text: the record itself,
the cache, and the relevance score used to merge two sources into one ranked list.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

ARXIV_ABS = "https://arxiv.org/abs"

#: Words that carry no signal in a paper search but do narrow a query to nothing.
#: "in code agents" should not be three constraints.
STOPWORDS = frozenset(
    "a an and are as at be by for from how in into is it of on or that the their to "
    "using via with within without over under between about across when where which".split()
)


@dataclass
class Paper:
    """One paper, from either source, normalised."""

    id: str  # arXiv id, e.g. "2510.20270"
    title: str
    abstract: str
    authors: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    published: str = ""
    url: str = ""
    source: str = "arxiv"  # arxiv | huggingface
    upvotes: int = 0  # huggingface only; 0 elsewhere

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Paper:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def citation(self) -> str:
        return f"arXiv:{self.id}"

    def one_line(self) -> str:
        who = (self.authors[0].split()[-1] + " et al.") if self.authors else "unknown"
        vote = f"  ▲{self.upvotes}" if self.upvotes else ""
        return f"{self.id}  {self.title[:68]}  ({who}){vote}"

    def brief(self, limit: int = 2400) -> str:
        """What the distiller is shown. Title plus abstract is usually enough to
        tell whether a paper contains a technique; the full PDF usually is not
        worth the tokens, and often is not worth the wait either."""
        return (
            f"Title: {self.title}\n"
            f"arXiv: {self.id}   ({', '.join(self.categories[:4])})\n"
            f"Authors: {', '.join(self.authors[:6])}\n\n"
            f"Abstract:\n{self.abstract[:limit]}"
        )


# --------------------------------------------------------------------------- #
# cache
# --------------------------------------------------------------------------- #


class Cache:
    """Disk cache keyed by a query string. Papers do not change; queries repeat,
    and every repeat that misses is a Bright Data request somebody pays for."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / f"{re.sub(r'[^a-zA-Z0-9._-]', '_', key)[:120]}.json"

    def get(self, key: str, max_age_s: float) -> list[Paper] | None:
        p = self._path(key)
        if not p.exists() or (time.time() - p.stat().st_mtime) > max_age_s:
            return None
        try:
            return [Paper.from_dict(d) for d in json.loads(p.read_text())]
        except Exception:
            return None

    def put(self, key: str, papers: list[Paper]) -> None:
        self._path(key).write_text(json.dumps([p.to_dict() for p in papers], indent=2))


# --------------------------------------------------------------------------- #
# ranking
# --------------------------------------------------------------------------- #


def terms(query: str) -> list[str]:
    return [w for w in re.findall(r"[A-Za-z][A-Za-z0-9-]{1,}", query.lower()) if w not in STOPWORDS]


def relevance(paper: Paper, want: set[str]) -> float:
    """Term overlap, title weighted heavily.

    A paper whose *title* says "reward hacking" is about reward hacking. One that
    mentions it once in related work is not, and upvotes will not make it so --
    which is the trap when merging a curated trending feed into a search result.
    """
    if not want:
        return 1.0
    title, body = paper.title.lower(), paper.abstract.lower()
    hits = sum(3.0 for t in want if t in title) + sum(1.0 for t in want if t in body)
    return hits / (4.0 * len(want))


def rank(papers: list[Paper], query: str, *, limit: int = 8, min_relevance: float = 0.12) -> list[Paper]:
    """Merge, deduplicate by arXiv id, and rank by relevance.

    Upvotes are a *tiebreak*, never the sort key. Hugging Face's listing is a
    trending feed: sort by popularity and you get this week's most-liked papers
    whatever was asked for, which is a worse answer than returning nothing.
    """
    want = set(terms(query))
    merged: dict[str, Paper] = {}
    for p in papers:
        prev = merged.get(p.id)
        if prev is None:
            merged[p.id] = p
            continue
        prev.upvotes = max(prev.upvotes, p.upvotes)
        if len(p.abstract) > len(prev.abstract):
            prev.abstract = p.abstract
        if len(p.authors) > len(prev.authors):
            prev.authors = p.authors
    ordered = sorted(merged.values(), key=lambda p: (-relevance(p, want), -p.upvotes, p.id))
    keep = [p for p in ordered if relevance(p, want) >= min_relevance]
    return (keep or ordered)[:limit]
