"""Research mode: papers in, skills out, and nothing unproven reaching a prompt.

The load-bearing test in this file is `test_a_proposed_skill_never_reaches_a_prompt`.
Everything else here is plumbing; that one is the thesis. A skill distilled from a
paper is a claim about what makes the agent work better, and this project does not
take claims on trust -- not from a model about its own patch, and not from a paper
about its own technique.

No network. The arXiv fixture is a trimmed real response, so a change to the parser
fails here rather than at a demo.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ratchet.context import Context
from ratchet.research.distill import NO_TECHNIQUE, _parse
from ratchet.research.scrape import PaperScraper, parse_papers, to_text
from ratchet.research.skills import ADOPTED, PROPOSED, REJECTED, Skill, SkillLibrary, Trial
from ratchet.research.sources import Paper, rank, relevance
from ratchet.research.trial import Arm, TrialOutcome, can_measure
from ratchet.subagents import ScriptedBackend

#: A trimmed but faithful slice of what arXiv's search page looks like after Bright
#: Data returns it. The highlight spans are the important part: arXiv wraps every
#: matched query term in one, which is exactly what shattered the titles the first
#: time this parser ran.
ARXIV_HTML = """
<li class="arxiv-result">
  <p class="list-title"><a href="/abs/2608.22103">arXiv:2608.22103</a>
    <span>&nbsp;[<a href="/pdf/2608.22103">pdf</a>, <a>ps</a>, <a>other</a>]</span></p>
  <div class="tags"><span class="tag">cs.LG</span> <span class="tag">cs.SE</span></div>
  <p class="title"><span class="search-hit">Hack</span>-Verifiable Terminal Bench: Evaluating
     <span class="search-hit">Reward</span> <span class="search-hit">Hacking</span></p>
  <p class="authors"><span>Authors:</span>
     <a>Amit Roth</a>, <a>Ivan Bercovich</a>, <a>Dana Fine</a></p>
  <p class="abstract"><span class="descriptor">Abstract</span>:
     We introduce a benchmark of terminal tasks whose rewards are verifiable.</p>
</li>
<li class="arxiv-result">
  <p class="list-title"><a href="/abs/2510.20270">arXiv:2510.20270</a>
    <span>&nbsp;[<a>pdf</a>, <a>other</a>]</span></p>
  <div class="tags"><span class="tag">cs.SE</span></div>
  <p class="title">ImpossibleBench: Measuring LLMs' Propensity of Exploiting Test Cases</p>
  <p class="authors"><span>Authors:</span> <a>Ziqian Zhong</a>, <a>Nicholas Carlini</a></p>
  <p class="abstract"><span class="descriptor">Abstract</span>:
     We build a benchmark of impossible tasks.</p>
</li>
"""

#: Hugging Face's listing, in the markdown Bright Data returns. No author or
#: abstract labels here at all -- that is why `enrich` exists.
HF_MARKDOWN = """
![](/papers/2608.25518)
Submitted by
!
IntJudge
132
Agentic Game Development as a Verifiable Trajectory Data Engine
![](/papers/2608.27448)
Submitted by
!
someone
69
TTPO: Test-Time Policy Optimization
"""


# --------------------------------------------------------------------------- #
# scraping
# --------------------------------------------------------------------------- #


def test_inline_tags_do_not_break_a_highlighted_title() -> None:
    """arXiv highlights every matched query term. Turn each tag into a newline and
    "Hack-Verifiable Terminal Bench" arrives as three fragments, and the parser
    picks whichever is longest -- so you search for reward hacking and get titles
    that begin mid-word."""
    papers = parse_papers(to_text(ARXIV_HTML))
    by_id = {p.id: p for p in papers}
    assert by_id["2608.22103"].title.startswith("Hack-Verifiable Terminal Bench")


def test_arxiv_block_yields_title_authors_and_abstract() -> None:
    by_id = {p.id: p for p in parse_papers(to_text(ARXIV_HTML))}
    assert set(by_id) == {"2608.22103", "2510.20270"}
    p = by_id["2510.20270"]
    assert p.title.startswith("ImpossibleBench")
    assert p.authors == ["Ziqian Zhong", "Nicholas Carlini"]
    assert p.abstract == "We build a benchmark of impossible tasks."
    assert p.citation() == "arXiv:2510.20270"


def test_format_links_and_subject_tags_never_become_the_title() -> None:
    """`[pdf, ps, other]` and `cs.LG cs.SE` sit between the id and the title. Both
    are longer than the minimum title length, so both have been the title once."""
    titles = [p.title for p in parse_papers(to_text(ARXIV_HTML))]
    assert not any(t.startswith("[") or "pdf" in t.lower() for t in titles)
    assert not any(re.fullmatch(r"(?:[a-z-]+\.[A-Z]{2}\s*)+", t) for t in titles)


def test_hugging_face_listing_parses_titles_without_labels() -> None:
    papers = parse_papers(to_text(HF_MARKDOWN), {"id_pattern": r"/papers/(\d{4}\.\d{4,5})"},
                          source="huggingface")
    assert {p.id for p in papers} == {"2608.25518", "2608.27448"}
    assert any(p.title.startswith("TTPO") for p in papers)


def test_a_listing_with_no_ids_yields_nothing_rather_than_guessing() -> None:
    assert parse_papers(to_text("<p>Just a moment...</p>")) == []


@pytest.mark.parametrize("body", [
    "Residential Failed (bad_endpoint): Requested site is not available",
    "Just a moment... Verifying you are human",
])
def test_an_interstitial_is_detected_as_a_failed_fetch(body: str) -> None:
    """Bright Data's refusal arrives as a 200 with a short body. Detected here or
    it parses as "this topic has no papers", which is the worst possible outcome:
    a broken pipeline that looks like an empty field."""
    assert PaperScraper._blocked(body)


def test_relevance_weights_the_title_over_the_abstract() -> None:
    """Merging a trending feed into a search result goes wrong exactly here: rank by
    popularity and you get this week's most-liked papers whatever was asked for."""
    want = {"reward", "hacking"}
    on_topic = Paper(id="1", title="Reward Hacking in Coding Agents", abstract="x")
    mentioned = Paper(id="2", title="A Vision Model", abstract="related work on reward hacking", upvotes=900)
    assert relevance(on_topic, want) > relevance(mentioned, want)
    assert [p.id for p in rank([mentioned, on_topic], "reward hacking")] == ["1", "2"]


def test_rank_merges_duplicates_keeping_the_richer_record() -> None:
    """The same paper arrives from both sources: arXiv with an abstract, Hugging
    Face with an upvote count. Neither copy alone is the best one."""
    a = Paper(id="1", title="T", abstract="a long abstract from arxiv", authors=["X"])
    b = Paper(id="1", title="T", abstract="", upvotes=42)
    merged = rank([a, b], "T")
    assert len(merged) == 1
    assert merged[0].upvotes == 42 and merged[0].abstract.startswith("a long")


# --------------------------------------------------------------------------- #
# distillation
# --------------------------------------------------------------------------- #

PAPER = Paper(id="2410.20285", title="SWE-Search", abstract="mcts for software agents",
              url="https://arxiv.org/abs/2410.20285")

GOOD = """NAME: Fork wide before deep
KIND: skill
TRIGGERS: stall, regression
APPLIES_TO: pytest
KEYWORDS: search, stall, tree
BODY:
When two attempts from the same state have both been pruned, change which file you
edit rather than how you edit it."""


def test_distilled_skill_carries_its_citation_and_starts_proposed() -> None:
    skill = _parse(GOOD, PAPER)
    assert skill is not None
    assert skill.name == "Fork wide before deep"
    assert skill.triggers == ["stall", "regression"]
    assert skill.applies_to == ["pytest"]
    assert skill.source == "arXiv:2410.20285"
    assert skill.status == PROPOSED, "distillation proposes; only a trial adopts"


def test_a_paper_with_no_technique_yields_no_skill() -> None:
    """Most papers do not contain an instruction. Stretching to find one fills the
    library with restated abstracts that cost context and change nothing."""
    assert _parse(f"{NO_TECHNIQUE} this is a benchmark paper.", PAPER) is None


@pytest.mark.parametrize("text", ["", "NAME: no body here", "BODY:\nno name"])
def test_malformed_distillations_are_dropped_not_guessed(text: str) -> None:
    assert _parse(text, PAPER) is None


# --------------------------------------------------------------------------- #
# the library
# --------------------------------------------------------------------------- #


def _skill(name: str, status: str = ADOPTED, **kw) -> Skill:
    return Skill(name=name, body=f"body of {name}", status=status, **kw)


def test_skill_round_trips_through_markdown(tmp_path: Path) -> None:
    s = _skill("Fork wide", trial=Trial(0.5, 0.8, 6, ADOPTED), keywords=["stall"])
    lib = SkillLibrary(tmp_path)
    path = lib.save(s)
    back = Skill.from_markdown(path.read_text())
    assert back.name == s.name and back.body == s.body
    assert back.trial.delta == pytest.approx(0.3)
    assert SkillLibrary.load(tmp_path).get("fork-wide") is not None


def test_only_adopted_skills_are_selected(tmp_path: Path) -> None:
    lib = SkillLibrary(tmp_path)
    lib.skills = [_skill("Proven"), _skill("Untested", PROPOSED), _skill("Failed", REJECTED)]
    assert [s.name for s in lib.select()] == ["Proven"]
    # the trial harness is the one caller allowed to see an unproven skill
    assert {s.name for s in lib.select(include_proposed=True)} == {"Proven", "Untested"}


def test_selection_respects_trigger_and_framework(tmp_path: Path) -> None:
    lib = SkillLibrary(tmp_path)
    lib.skills = [
        _skill("On stall", triggers=["stall"]),
        _skill("Always", triggers=["always"]),
        _skill("Jest only", applies_to=["jest"], triggers=["always"]),
    ]
    names = {s.name for s in lib.select(trigger="stall", framework="pytest")}
    assert names == {"On stall", "Always"}, "a jest skill must not reach a pytest run"
    assert {s.name for s in lib.select(trigger="always", framework="pytest")} == {"Always"}


def test_better_measured_skills_sort_first(tmp_path: Path) -> None:
    lib = SkillLibrary(tmp_path)
    lib.skills = [_skill("Small win", trial=Trial(0.5, 0.55, 4, ADOPTED)),
                  _skill("Big win", trial=Trial(0.5, 0.9, 4, ADOPTED))]
    assert [s.name for s in lib.select()][0] == "Big win"


# --------------------------------------------------------------------------- #
# the invariant
# --------------------------------------------------------------------------- #


def test_a_proposed_skill_never_reaches_a_prompt(tmp_path: Path) -> None:
    """The thesis of research mode, as a test.

    A paper does not get to change how this agent works just because it was
    published. An unproven skill is inert: it sits in git, it is reviewable, and it
    is absent from every prompt until a trial says otherwise.
    """
    lib = SkillLibrary(tmp_path)
    lib.skills = [_skill("Unproven idea", PROPOSED)]
    chosen = lib.select(trigger="always", framework="pytest")
    assert chosen == []

    rendered = Context(task="t", repo_map="", failure="boom", diff_so_far="", dead_ends=[],
                       skills=[s.render() for s in chosen]).render()
    assert "Unproven idea" not in rendered
    assert "Techniques that measurably helped" not in rendered


def test_an_adopted_skill_reaches_the_prompt_with_its_citation(tmp_path: Path) -> None:
    lib = SkillLibrary(tmp_path)
    lib.skills = [_skill("Fork wide", source="arXiv:2410.20285")]
    rendered = Context(task="t", repo_map="", failure="boom", diff_so_far="", dead_ends=[],
                       skills=[s.render() for s in lib.select()]).render()
    assert "Fork wide" in rendered
    assert "arXiv:2410.20285" in rendered, "a claim in a prompt should be traceable to its source"


# --------------------------------------------------------------------------- #
# trials
# --------------------------------------------------------------------------- #


def test_a_scripted_backend_cannot_be_ab_tested() -> None:
    """You cannot A/B a prompt against a generator that does not read prompts.
    Printing two identical numbers and a verdict would be worse than refusing:
    it looks like evidence."""
    ok, why = can_measure(ScriptedBackend([]))
    assert not ok
    assert "never reads the prompt" in why


def test_break_even_is_a_rejection() -> None:
    """Every skill costs context in every prompt it appears in, so a tie is a loss."""
    same = [Arm(True, 6, 0.1, 1.0), Arm(False, 8, 0.1, 1.0)]
    out = TrialOutcome(skill="s", n=2, baseline=list(same), treatment=list(same))
    assert out.delta == 0.0
    assert TrialOutcome._mean_nodes(out.baseline) == TrialOutcome._mean_nodes(out.treatment)


def test_outcome_reports_the_delta_it_measured() -> None:
    out = TrialOutcome(
        skill="Fork wide", n=2,
        baseline=[Arm(False, 8, 0.2, 1.0), Arm(False, 8, 0.2, 1.0)],
        treatment=[Arm(True, 5, 0.2, 1.0), Arm(True, 5, 0.2, 1.0)],
        verdict=ADOPTED,
    )
    assert out.delta == pytest.approx(1.0)
    assert out.to_trial().verdict == ADOPTED
    assert "delta +1.00" in out.report()
