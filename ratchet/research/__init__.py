"""Research mode: read the literature, turn it into skills, and make it prove itself.

The rule that governs patches governs skills too. A paper does not get to tell this
agent how to work just because it was published -- a skill distilled from a paper is
a *proposal*, and it is adopted only if measuring it against the eval suite shows it
actually helps. Same thesis as the rest of Ratchet, one level up: the agent does not
decide that a technique works, the evals do.
"""

from .scrape import PaperScraper, parse_papers, to_text
from .skills import Skill, SkillLibrary
from .sources import Cache, Paper, rank, relevance

__all__ = [
    "Cache", "Paper", "PaperScraper", "Skill", "SkillLibrary",
    "parse_papers", "rank", "relevance", "to_text",
]
