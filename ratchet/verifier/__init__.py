"""The gauntlet: every stage a candidate patch must clear before it is allowed to stick.

Usable with no agent attached — `ratchet verify` runs exactly this. That is the test
of whether a verifier is real or just prompt scaffolding.
"""

from .cheat import inspect, parse_unified_diff  # noqa: F401
from .gauntlet import WEIGHTS, Gauntlet  # noqa: F401
from .grade import grade  # noqa: F401
