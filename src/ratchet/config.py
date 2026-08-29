"""Settings and task loading.

Everything is env-overridable because a demo machine is not a dev machine and you
will be changing one value with thirty seconds left before you present.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .models import TaskSpec

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


@dataclass
class Settings:
    # repo under work
    repo_path: str = "."
    base_ref: str = "HEAD"
    base_branch: str = "main"
    remote: str = "origin"
    task_path: str = "tasks/demo-001-slugify/task.yaml"
    run_id: str | None = None

    # verification
    backend: str = "docker"  # docker | local
    type_cmd: str | None = "python -m mypy --ignore-missing-imports ."
    lint_cmd: str | None = "python -m ruff check ."

    # ratchet mcp server
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8931

    # trueforge
    trueforge_base_url: str = "http://localhost:8790"
    model: str = "anthropic/claude-sonnet-4-6"
    agent_spec_path: str = "agent/agent.json"

    # bright data
    brightdata_api_key: str | None = None
    brightdata_unlocker_zone: str = "mcp_unlocker"
    scrapers_path: str = "src/ratchet/scrapers.yaml"
    docs_cache_dir: str = ".ratchet/docs-cache"

    extras: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            repo_path=_env("RATCHET_REPO", ".") or ".",
            base_ref=_env("RATCHET_BASE_REF", "HEAD") or "HEAD",
            base_branch=_env("RATCHET_BASE_BRANCH", "main") or "main",
            remote=_env("RATCHET_REMOTE", "origin") or "origin",
            task_path=_env("RATCHET_TASK", "tasks/demo-001-slugify/task.yaml") or "",
            run_id=_env("RATCHET_RUN_ID"),
            backend=_env("RATCHET_BACKEND", "docker") or "docker",
            type_cmd=_env("RATCHET_TYPE_CMD", "python -m mypy --ignore-missing-imports ."),
            lint_cmd=_env("RATCHET_LINT_CMD", "python -m ruff check ."),
            mcp_host=_env("RATCHET_MCP_HOST", "127.0.0.1") or "127.0.0.1",
            mcp_port=int(_env("RATCHET_MCP_PORT", "8931") or 8931),
            trueforge_base_url=_env("TRUEFORGE_BASE_URL", "http://localhost:8790") or "",
            model=_env("RATCHET_MODEL", "anthropic/claude-sonnet-4-6") or "",
            agent_spec_path=_env("RATCHET_AGENT_SPEC", "agent/agent.json") or "",
            brightdata_api_key=_env("BRIGHTDATA_API_KEY") or _env("API_TOKEN"),
            brightdata_unlocker_zone=_env("BRIGHTDATA_UNLOCKER_ZONE", "mcp_unlocker") or "mcp_unlocker",
            scrapers_path=_env("RATCHET_SCRAPERS", "src/ratchet/scrapers.yaml") or "",
            docs_cache_dir=_env("RATCHET_DOCS_CACHE", ".ratchet/docs-cache") or "",
        )


def load_task(path: str | Path) -> TaskSpec:
    p = Path(path)
    raw = p.read_text()
    if p.suffix in (".yaml", ".yml"):
        if yaml is None:  # pragma: no cover
            raise RuntimeError("pip install pyyaml, or use a .json task file")
        data = yaml.safe_load(raw)
    else:
        data = json.loads(raw)
    known = {f for f in TaskSpec.__dataclass_fields__}
    return TaskSpec(**{k: v for k, v in data.items() if k in known})
