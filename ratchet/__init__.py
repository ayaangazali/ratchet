"""Ratchet -- a coding agent that cannot decide it is done.

The agent proposes; a verifier disposes. Every accepted step is a git commit,
every rejected step is rolled back and handed back as the next observation, and
the only irreversible action in the system waits for a human.
"""

__version__ = "0.4.1"
