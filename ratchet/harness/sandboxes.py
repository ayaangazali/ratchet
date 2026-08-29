"""Wiring the sandbox provider that the harness owns.

Ratchet never creates a container. When TrueForge is configured with a snapshot-
capable sandbox provider, this returns a `HarnessProvider` bound to it and the tree
search runs on real snapshots, with each child inheriting its parent's installed
dependencies and warm build cache.

When the deployment has no snapshot primitive exposed -- which is the common case
for a local `npx` install -- this returns `None`, and the caller falls back to git
worktrees off a prebuilt base with a shared warm virtualenv. That fallback is a
first-class path, not an apology: same search, same verifier, same demo. You lose
the warm-cache speed, you keep everything that makes the project interesting.

`ratchet bench-snapshot` is how you decide which one you have, and the build plan is
explicit that the decision happens before noon, not at three o'clock.
"""

from __future__ import annotations

from pathlib import Path

from ..sandbox import HarnessProvider


class SandboxUnavailable(RuntimeError):
    pass


def harness_provider(client, repo: Path) -> HarnessProvider | None:
    """Return a harness-backed provider, or None if this deployment cannot fork one.

    The probe is deliberately cheap and non-destructive: we ask the server what it
    can do rather than trying to create a sandbox and catching the failure.
    """
    try:
        caps = client.capabilities() or {}
    except Exception:
        return None
    sandbox = (caps.get("sandbox") or {}) if isinstance(caps, dict) else {}
    if not sandbox.get("enabled") and not sandbox.get("providers"):
        return None
    if not hasattr(client, "fork_sandbox"):
        # The HTTP surface of the local install exposes no direct exec/snapshot
        # primitive. Say so plainly rather than pretending; the caller falls back.
        return None
    return HarnessProvider(client, repo_url=str(repo))


def describe_provider(settings) -> tuple[str, str]:
    """Which sandbox `ratchet run` would actually use, and why.

    `ratchet doctor` asks this before a run rather than after one: "harness" and
    "worktree" behave the same to the search but differ enormously in speed, and
    finding out which you got by watching a demo crawl is finding out too late.
    """
    want = getattr(settings, "provider", "auto")
    if want in ("harness", "auto"):
        try:
            from .client import TrueForgeClient

            client = TrueForgeClient(getattr(settings, "trueforge_base_url", ""))
            prov = harness_provider(client, Path(getattr(settings, "repo", ".")))
            if prov is not None:
                snap = "snapshots" if getattr(prov, "supports_snapshots", False) else "no snapshots"
                return "harness", f"TrueForge sandboxes, {snap}"
        except Exception as e:
            if want == "harness":
                return "harness", f"unavailable: {str(e)[:100]}"
    return "worktree", "git worktrees off a prebuilt base, sharing one warm venv"
