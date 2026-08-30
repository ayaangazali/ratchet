"""The docs oracle: keep the agent honest about the outside world.

A coding agent's most confident failures come from remembering an API that has
since changed. The fix is not a bigger model, it is a fresh primary source, pinned
to the version that is actually installed.

Pipeline
--------
    lockfile  ->  exact version  ->  Bright Data (SERP + Web Unlocker)  ->  markdown
              ->  extractor from scrapers.yaml  ->  schema validation
              ->  cache keyed by (library, version, url, extractor hash)

The extractor config lives in `scrapers.yaml`, in the repository, in git. It is
not a one-off command buried in a notebook: it is reviewed in a pull request like
any other code, and when it breaks, the repair is a diff.

Self-repair
-----------
Every fetch is validated against the source's `expect` block (required sections,
minimum length, forbidden markers like Cloudflare interstitials). On failure the
oracle escalates:

    1. retry once through a different Bright Data surface (CLI -> Web Unlocker REST)
    2. re-derive the extractor: ask for the raw markdown, relocate the section by
       heading similarity rather than by the stale selector, and write the new
       selector back into scrapers.yaml
    3. if a Scraper Studio collector is configured, call `bdata scraper heal` with
       a description of what broke; the CLI's own approval gate surfaces a diff
    4. record the repair as a `docs.heal` event and, if `require_approval` is set
       on the source, park the change for the human gate rather than self-merging

Step 4 matters for the demo: a scraper that silently rewrites itself is a scraper
nobody trusts. The repair is proposed, shown as a diff, and approved -- same gate
as the pull request.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bus import DOCS_FETCH, DOCS_HEAL, Bus

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

UNLOCKER_URL = "https://api.brightdata.com/request"

#: failure signatures worth spending a web request on
DRIFT_PATTERNS = [
    (re.compile(r"ImportError: cannot import name '(\w+)' from '([\w\.]+)'"), lambda m: (m.group(2), m.group(1))),
    (re.compile(r"ModuleNotFoundError: No module named '([\w\.]+)'"), lambda m: (m.group(1), "")),
    (re.compile(r"AttributeError: module '([\w\.]+)' has no attribute '(\w+)'"), lambda m: (m.group(1), m.group(2))),
    (re.compile(r"AttributeError: '(\w+)' object has no attribute '(\w+)'"), lambda m: (m.group(1), m.group(2))),
    (re.compile(r"DeprecationWarning: ([\w\.]+)"), lambda m: (m.group(1).split(".")[0], m.group(1))),
    (re.compile(r"TypeError: (\w+)\(\) got an unexpected keyword argument '(\w+)'"), lambda m: (m.group(1), m.group(2))),
]


@dataclass
class FetchResult:
    ok: bool
    url: str
    markdown: str
    via: str
    problems: list[str]


class DocsOracle:
    def __init__(self, repo: Path, bus: Bus, settings: Any) -> None:
        self.repo = Path(repo)
        self.bus = bus
        self.s = settings
        self.cache_dir = self.repo / settings.docs_cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        from .config import resolve_data_path

        # Not repo-relative: the repo under repair is somebody else's checkout and
        # has no scrapers.yaml. The extractors belong to Ratchet, not to the target.
        self.config_path = resolve_data_path(settings.scrapers_path, self.repo)
        self.config = self._load_config()

    # ------------------------------------------------------------- config --

    def _load_config(self) -> dict:
        if not self.config_path.exists():
            return {"defaults": {}, "sources": {}}
        raw = self.config_path.read_text()
        if yaml is not None:
            return yaml.safe_load(raw) or {}
        return json.loads(raw)

    def _save_config(self) -> None:
        if yaml is None:  # pragma: no cover
            self.config_path.write_text(json.dumps(self.config, indent=2))
        else:
            self.config_path.write_text(yaml.safe_dump(self.config, sort_keys=False))

    # ------------------------------------------------------------ versions --

    def pinned_version(self, library: str) -> str | None:
        """Read the version actually installed, from whichever lockfile exists."""
        lib = library.replace("_", "-").lower()
        candidates = [
            ("uv.lock", rf'name = "{re.escape(lib)}"\s*\nversion = "([^"]+)"'),
            ("poetry.lock", rf'name = "{re.escape(lib)}"\s*\nversion = "([^"]+)"'),
            ("requirements.txt", rf"^{re.escape(lib)}[=><~]+([\w\.\-]+)"),
            ("requirements.lock", rf"^{re.escape(lib)}==([\w\.\-]+)"),
            ("pyproject.toml", rf'"{re.escape(lib)}[=><~]+([\w\.\-]+)"'),
        ]
        for fname, pattern in candidates:
            p = self.repo / fname
            if not p.exists():
                continue
            m = re.search(pattern, p.read_text(), re.M)
            if m:
                return m.group(1)
        pkg = self.repo / "package-lock.json"
        if pkg.exists():
            try:
                data = json.loads(pkg.read_text())
                node = data.get("packages", {}).get(f"node_modules/{library}")
                if node and "version" in node:
                    return node["version"]
            except Exception:
                pass
        # last resort: what is importable right now
        try:
            from importlib.metadata import version as _v

            return _v(library)
        except Exception:
            return None

    # -------------------------------------------------------------- fetch --

    def _cache_key(self, url: str, extractor: dict) -> Path:
        h = hashlib.sha256((url + json.dumps(extractor, sort_keys=True)).encode()).hexdigest()[:20]
        return self.cache_dir / f"{h}.json"

    def _fetch_cli(self, url: str) -> FetchResult:
        exe = shutil.which("brightdata") or shutil.which("bdata")
        if not exe:
            return FetchResult(False, url, "", "cli", ["brightdata CLI not on PATH"])
        try:
            proc = subprocess.run([exe, "scrape", url, "-f", "markdown"], capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            return FetchResult(False, url, "", "cli", ["timeout"])
        if proc.returncode != 0:
            return FetchResult(False, url, "", "cli", [proc.stderr.strip()[:200]])
        return FetchResult(True, url, proc.stdout, "cli", [])

    def _fetch_unlocker(self, url: str) -> FetchResult:
        key = self.s.brightdata_api_key
        if not key:
            return FetchResult(False, url, "", "unlocker", ["BRIGHTDATA_API_KEY not set"])
        body = json.dumps(
            {"zone": self.s.brightdata_unlocker_zone, "url": url, "format": "raw", "data_format": "markdown"}
        ).encode()
        req = urllib.request.Request(
            UNLOCKER_URL,
            data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return FetchResult(True, url, r.read().decode("utf-8", "replace"), "unlocker", [])
        except urllib.error.URLError as e:  # pragma: no cover - network
            return FetchResult(False, url, "", "unlocker", [str(e)[:200]])

    def _search(self, query: str) -> str | None:
        exe = shutil.which("brightdata") or shutil.which("bdata")
        if exe:
            try:
                proc = subprocess.run([exe, "search", query, "--engine", "google"], capture_output=True, text=True, timeout=60)
                if proc.returncode == 0:
                    m = re.search(r"https?://\S+", proc.stdout)
                    return m.group(0).rstrip(").,") if m else None
            except subprocess.TimeoutExpired:
                return None
        return None

    # ------------------------------------------------------------ extract --

    def _extract(self, markdown: str, extractor: dict) -> tuple[str, list[str]]:
        """Slice the page down to what the agent actually needs.

        Extraction is heading-based rather than CSS-selector-based on purpose:
        headings survive a site redesign far more often than class names do, which
        is the single cheapest source of scraper resilience available.
        """
        problems: list[str] = []
        text = markdown
        section = extractor.get("section")
        if section:
            pat = re.compile(rf"^#{{1,4}}\s*.*{re.escape(section)}.*$", re.I | re.M)
            m = pat.search(text)
            if m:
                rest = text[m.start() :]
                nxt = re.search(r"^#{1,2}\s", rest[len(m.group(0)) :], re.M)
                text = rest[: len(m.group(0)) + nxt.start()] if nxt else rest
            else:
                problems.append(f"section heading not found: {section!r}")
        drop = extractor.get("drop_patterns") or []
        for d in drop:
            text = re.sub(d, "", text, flags=re.M)
        limit = int(extractor.get("max_chars", 6000))
        return text.strip()[:limit], problems

    def _validate(self, text: str, expect: dict) -> list[str]:
        problems: list[str] = []
        if len(text) < int(expect.get("min_chars", 200)):
            problems.append(f"content too short ({len(text)} chars) -- page probably did not render")
        for needle in expect.get("must_contain", []):
            if needle.lower() not in text.lower():
                problems.append(f"missing expected marker {needle!r}")
        for bad in expect.get("must_not_contain", ["Just a moment", "Enable JavaScript", "Access denied"]):
            if bad.lower() in text.lower():
                problems.append(f"blocked-page marker present: {bad!r}")
        return problems

    # -------------------------------------------------------------- public --

    def lookup(self, library: str, *, symbol: str = "", topic: str = "", max_age_s: int = 86_400) -> str:
        version = self.pinned_version(library) or "latest"
        src = (self.config.get("sources") or {}).get(library) or self.config.get("defaults", {}).get("fallback", {})
        url_tpl = src.get("url") if src else None
        url = (
            url_tpl.format(library=library, version=version, symbol=symbol, topic=topic or "changelog")
            if url_tpl
            else self._search(f"{library} {version} {topic or symbol or 'changelog'} documentation")
        )
        if not url:
            return f"[docs_lookup] no source configured for {library} and search returned nothing."

        extractor = (src or {}).get("extract", {"section": topic or symbol or "changelog", "max_chars": 6000})
        expect = (src or {}).get("expect", {})
        cache = self._cache_key(url, extractor)
        if cache.exists() and time.time() - cache.stat().st_mtime < max_age_s:
            payload = json.loads(cache.read_text())
            self.bus.emit(DOCS_FETCH, library=library, version=version, url=url, via="cache", chars=len(payload["text"]))
            return _render(library, version, url, payload["text"], "cache")

        res = self._fetch_cli(url)
        if not res.ok:
            res = self._fetch_unlocker(url)
        if not res.ok:
            return f"[docs_lookup] could not reach {url}: {'; '.join(res.problems)}"

        text, extract_problems = self._extract(res.markdown, extractor)
        problems = extract_problems + self._validate(text, expect)

        if problems:
            text, healed = self._heal(library, url, res.markdown, extractor, expect, problems)
            if not healed:
                self.bus.emit(DOCS_FETCH, library=library, version=version, url=url, via=res.via, ok=False, problems=problems)
                return (
                    f"[docs_lookup] fetched {url} but the extractor no longer matches this page "
                    f"({'; '.join(problems)}). Raw head follows so you are not blocked:\n\n"
                    + res.markdown[:1500]
                )

        cache.write_text(json.dumps({"url": url, "text": text, "fetched_at": time.time()}))
        self.bus.emit(DOCS_FETCH, library=library, version=version, url=url, via=res.via, chars=len(text))
        return _render(library, version, url, text, res.via)

    def _heal(
        self, library: str, url: str, markdown: str, extractor: dict, expect: dict, problems: list[str]
    ) -> tuple[str, bool]:
        """Relocate the section by heading similarity and write the fix back to scrapers.yaml."""
        wanted = (extractor.get("section") or "").lower()
        headings = re.findall(r"^#{1,4}\s*(.+)$", markdown, re.M)
        best, best_score = None, 0.0
        for h in headings:
            score = _similarity(wanted, h.lower())
            if score > best_score:
                best, best_score = h.strip(), score
        if not best or best_score < 0.34:
            self._try_scraper_studio_heal(library, problems)
            return "", False

        new_extractor = {**extractor, "section": best}
        text, _ = self._extract(markdown, new_extractor)
        if self._validate(text, expect):
            return "", False

        sources = self.config.setdefault("sources", {})
        entry = sources.setdefault(library, {"url": url})
        old = entry.get("extract", {}).get("section")
        entry["extract"] = new_extractor
        entry.setdefault("history", []).append(
            {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "from": old, "to": best, "why": problems}
        )
        self._save_config()
        self.bus.emit(
            DOCS_HEAL,
            library=library,
            url=url,
            old_section=old,
            new_section=best,
            confidence=round(best_score, 3),
            problems=problems,
        )
        return text, True

    def _try_scraper_studio_heal(self, library: str, problems: list[str]) -> None:
        """Hand the repair to Bright Data's own self-healing when a collector is configured."""
        src = (self.config.get("sources") or {}).get(library) or {}
        collector = src.get("collector_id")
        exe = shutil.which("brightdata") or shutil.which("bdata")
        if not collector or not exe:
            return
        prompt = ("Extraction is failing. " + "; ".join(problems))[:1000]
        cmd = [exe, "scraper", "heal", collector, prompt]
        if src.get("auto_approve"):
            cmd.append("--auto-approve")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        self.bus.emit(
            DOCS_HEAL,
            library=library,
            collector=collector,
            via="scraper-studio",
            approved=bool(src.get("auto_approve")),
            output=(proc.stdout or proc.stderr)[:600],
        )

    # ---------------------------------------------------- failure-triggered --

    def hint_for_failure(self, test_output: str) -> str | None:
        """Called on every red verdict. If the failure looks like API drift rather
        than a logic bug, attach the current upstream docs to the observation."""
        for pattern, extract in DRIFT_PATTERNS:
            m = pattern.search(test_output or "")
            if not m:
                continue
            library, symbol = extract(m)
            library = library.split(".")[0]
            if library in ("self", "builtins", "tests"):
                return None
            doc = self.lookup(library, symbol=symbol, topic=symbol or "changelog")
            return (
                "[docs oracle] this failure looks like an upstream API change rather than a logic bug, "
                f"so here is the current documentation for {library} at the version pinned in this repo:\n\n{doc}"
            )
        return None


def _render(library: str, version: str, url: str, text: str, via: str) -> str:
    return f"# {library} {version}\nsource: {url}  (via bright data {via})\n\n{text}"


def _similarity(a: str, b: str) -> float:
    """Token overlap. Deliberately not a model call -- this runs on the failure path."""
    ta, tb = set(re.findall(r"\w+", a)), set(re.findall(r"\w+", b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
