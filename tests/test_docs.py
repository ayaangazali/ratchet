"""The Bright Data docs oracle -- its pure pipeline, offline.

The fetch itself needs a key and a network and is exercised with `ratchet docs`.
Everything downstream of the fetch -- version pinning, heading-based extraction,
expect-block validation, and the self-heal that relocates a section when a site
restructures -- is a pure function of markdown text, and that is what these test.
This is the logic that decides whether Bright Data's output is trustworthy, so it
is the part worth pinning down.
"""

from __future__ import annotations

import pytest

from ratchet.bus import Bus
from ratchet.config import Settings
from ratchet.docs import DocsOracle

# a realistic changelog page, the shape Bright Data returns for a docs site
MARKDOWN = """\
# Changelog

## 0.27.0

### Added

- `Client.send()` now accepts a `follow_redirects` argument.

### Removed

- The deprecated `proxies=` argument was removed; use `proxy=` instead.

## 0.26.0

### Fixed

- A bug in connection pooling under HTTP/2.
"""

# the same content after a redesign: the version heading changed format, so the
# configured "0.27.0" no longer appears as a substring and the extractor misses
RESTRUCTURED = MARKDOWN.replace("## 0.27.0", "## 0.27 (final release)")


@pytest.fixture()
def oracle(tmp_path) -> DocsOracle:
    repo = tmp_path / "repo"
    (repo / ".ratchet").mkdir(parents=True)
    (repo / "requirements.txt").write_text("httpx==0.27.0\n")
    settings = Settings(repo=str(repo), scrapers_path="scrapers.yaml", docs_cache_dir=".ratchet/docs-cache")
    (repo / "scrapers.yaml").write_text(
        "sources:\n"
        "  httpx:\n"
        "    url: https://www.python-httpx.org/changelog/\n"
        "    extract: {section: '0.27.0', max_chars: 4000}\n"
        "    expect: {min_chars: 20, must_contain: ['follow_redirects']}\n"
    )
    return DocsOracle(repo, Bus(repo / ".ratchet" / "t.bus.jsonl"), settings)


def test_pins_the_installed_version_from_the_lockfile(oracle):
    assert oracle.pinned_version("httpx") == "0.27.0"


def test_extract_slices_to_the_requested_section(oracle):
    text, problems = oracle._extract(MARKDOWN, {"section": "0.27.0", "max_chars": 4000})
    assert not problems
    assert "follow_redirects" in text
    assert "0.26.0" not in text  # stops at the next same-or-higher heading


def test_validate_flags_a_blocked_page(oracle):
    problems = oracle._validate("Just a moment... Enable JavaScript", {"min_chars": 5})
    assert any("blocked-page" in p for p in problems)


def test_validate_flags_a_missing_marker(oracle):
    problems = oracle._validate("some short unrelated text here", {"must_contain": ["follow_redirects"]})
    assert any("missing expected marker" in p for p in problems)


def test_self_heal_relocates_a_renamed_section_and_rewrites_the_config(oracle):
    """The heart of it: the site renamed the version heading, so the configured
    extractor no longer matches. The oracle relocates the section by heading
    similarity, validates the relocated text, and writes the fix back to
    scrapers.yaml with a timestamped history entry -- a reviewable diff."""
    extractor = {"section": "0.27.0", "max_chars": 4000}
    expect = {"min_chars": 20, "must_contain": ["follow_redirects"]}
    # the stale extractor misses on the restructured page
    text, problems = oracle._extract(RESTRUCTURED, extractor)
    assert problems

    healed_text, ok = oracle._heal("httpx", "https://x", RESTRUCTURED, extractor, expect, problems)
    assert ok
    assert "follow_redirects" in healed_text
    # the repair landed in the config, with an audit trail
    entry = oracle.config["sources"]["httpx"]
    assert "0.27 (final release)" in entry["extract"]["section"]
    assert entry["history"][-1]["from"] == "0.27.0"
    assert "0.27 (final release)" in entry["history"][-1]["to"]


def test_drift_shaped_failure_triggers_a_lookup_only_for_drift(oracle, monkeypatch):
    """hint_for_failure fires on import/attr/kwarg errors and stays silent on a
    plain assertion failure -- so a logic bug does not spend a web request."""
    calls = []
    monkeypatch.setattr(oracle, "lookup", lambda lib, **kw: calls.append(lib) or "DOCS")

    assert oracle.hint_for_failure("AssertionError: assert 3 == 4") is None
    assert not calls
    hint = oracle.hint_for_failure("TypeError: send() got an unexpected keyword argument 'proxies'")
    assert hint and "DOCS" in hint
    assert calls == ["send"]
