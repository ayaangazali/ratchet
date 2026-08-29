"""The gauntlet: every gate a patch must clear before it is allowed to stick."""

from .grade import grade  # noqa: F401
from .patchlint import lint  # noqa: F401
from .score import compute_score, decide  # noqa: F401
