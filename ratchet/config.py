"""Settings and task loading.

Everything is env-overridable, because a demo machine is not a dev machine and you
will be changing exactly one value with thirty seconds left before you present.
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


#: Fan-out uses different providers on purpose: three samples from one model give
#: three phrasings of one idea, three models give three ideas.
DEFAULT_GENERATORS = ["anthropic/claude-sonnet-4-6", "openai/gpt-5.2", "google-gemini/gemini-3-pro"]


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


@dataclass
class Settings:
    # what we are working on
    repo: str = "demo-repo"
    task_path: str = "tasks/demo-001-slugify/task.yaml"
    run_id: str | None = None

    # where patches run
    provider: str = "auto"  # auto | harness | worktree
    venv: str | None = None

    # search
    max_nodes: int = 40
    max_seconds: float = 900.0
    max_usd: float = 3.0
    patience: int = 3
    parallel: bool = True

    # models, by role
    model_cartographer: str = "openai/gpt-5-mini"
    model_reviewer: str = "openai/gpt-5-mini"
    model_generators: list[str] = field(default_factory=lambda: list(DEFAULT_GENERATORS))

    # harness
    trueforge_base_url: str = "http://localhost:8790"

    # bright data
    brightdata_api_key: str | None = None
    brightdata_unlocker_zone: str = "mcp_unlocker"
    scrapers_path: str = "ratchet/scrapers.yaml"
    docs_cache_dir: str = ".ratchet/docs-cache"

    # git
    base_branch: str = "main"
    remote: str = "origin"

    @classmethod
    def from_env(cls) -> Settings:
        gens = _env("RATCHET_GENERATORS")
        return cls(
            repo=_env("RATCHET_REPO", "demo-repo") or "demo-repo",
            task_path=_env("RATCHET_TASK", "tasks/demo-001-slugify/task.yaml") or "",
            run_id=_env("RATCHET_RUN_ID"),
            provider=_env("RATCHET_PROVIDER", "auto") or "auto",
            venv=_env("RATCHET_VENV"),
            max_nodes=int(_env("RATCHET_MAX_NODES", "40") or 40),
            max_seconds=float(_env("RATCHET_MAX_SECONDS", "900") or 900),
            max_usd=float(_env("RATCHET_MAX_USD", "3") or 3),
            patience=int(_env("RATCHET_PATIENCE", "3") or 3),
            parallel=(_env("RATCHET_PARALLEL", "1") or "1") not in ("0", "false", "no"),
            model_cartographer=_env("RATCHET_MODEL_CARTOGRAPHER", "openai/gpt-5-mini") or "",
            model_reviewer=_env("RATCHET_MODEL_REVIEWER", "openai/gpt-5-mini") or "",
            model_generators=[m.strip() for m in gens.split(",")] if gens else list(DEFAULT_GENERATORS),
            trueforge_base_url=_env("TRUEFORGE_BASE_URL", "http://localhost:8790") or "",
            brightdata_api_key=_env("BRIGHTDATA_API_KEY") or _env("API_TOKEN"),
            brightdata_unlocker_zone=_env("BRIGHTDATA_UNLOCKER_ZONE", "mcp_unlocker") or "mcp_unlocker",
            scrapers_path=_env("RATCHET_SCRAPERS", "ratchet/scrapers.yaml") or "",
            docs_cache_dir=_env("RATCHET_DOCS_CACHE", ".ratchet/docs-cache") or "",
            base_branch=_env("RATCHET_BASE_BRANCH", "main") or "main",
            remote=_env("RATCHET_REMOTE", "origin") or "origin",
        )

    def roles(self):
        from .subagents import Roles

        return Roles(
            cartographer=self.model_cartographer,
            reviewer=self.model_reviewer,
            generators=list(self.model_generators),
        )

    def budget(self):
        from .scheduler import Budget

        return Budget(max_nodes=self.max_nodes, max_seconds=self.max_seconds, max_usd=self.max_usd)


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
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"{p}: unknown task fields {sorted(unknown)}")
    return TaskSpec(**{k: v for k, v in data.items() if k in known})
