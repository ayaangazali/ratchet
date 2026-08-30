"""Qodo as an MCP tool, so a review can happen before the commit exists.

Qodo reviews pull requests. Useful, and too late: by the time there is a PR the
work is already in the branch and the fix is a second commit. Wrapping the CLI as
an MCP server moves the same reviewer to where it does more good -- it reviews the
*diff*, in the sandbox, before anything is committed, and its findings become work
in the same loop that produced the patch.

One tool, `review_diff(diff, context) -> findings[]`. When the `qodo` CLI is on
PATH it is invoked; when it is not, `scripted=True` returns the findings a review
of this kind actually produced, so the pipeline can be demonstrated end to end
without four accounts and a network. The stream says which of the two happened,
because a demo that hides that it is one is worth nothing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass, field

SEVERITIES = ("critical", "high", "medium", "low")


@dataclass
class Finding:
    severity: str
    title: str
    detail: str
    path: str = ""
    line: int = 0
    fix: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def blocking(self) -> bool:
        """What must be answered before the diff may be committed. Qodo's own
        severities decide; ratchet does not get to reinterpret them downward."""
        return self.severity in ("critical", "high")


@dataclass
class Review:
    findings: list[Finding] = field(default_factory=list)
    reviewer: str = "qodo"
    scripted: bool = False

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.blocking]

    @property
    def clean(self) -> bool:
        return not self.blocking


#: What a review of a verifier change turns up. Every one of these is real: Qodo
#: raised each against this repository, and they are quoted as they arrived.
SCRIPTED_FINDINGS = [
    Finding("high", "Ignored protected files survive the reset",
            "git clean -fdq preserves ignored files, so a file created under a protected "
            "directory survives the pre-run reset and can still affect grading.",
            "ratchet/verifier/eval_script.py", 49, "clean with -x inside protected paths"),
    Finding("high", "End marker is forgeable",
            "parse_exit_code splits on the first end marker, so suite code can print a forged "
            "END followed by exit code 0 and be graded on the forgery.",
            "ratchet/verifier/parsers.py", 59, "bound the trusted region at the last end marker"),
    Finding("medium", "Empty truncation audits clean",
            "verify() only requires a seal when at least one receipt remains, so truncating the "
            "receipt file to zero bytes returns success.",
            "ratchet/receipts.py", 166, "an empty chain is a problem, not a pass"),
]


class QodoMCP:
    """The adapter. `available()` says whether this is the real reviewer."""

    tool_name = "review_diff"

    def __init__(self, *, scripted: bool | None = None, timeout: float = 240.0) -> None:
        self.exe = shutil.which("qodo") or shutil.which("qodo-cli")
        self.scripted = (self.exe is None) if scripted is None else scripted
        self.timeout = timeout

    def available(self) -> bool:
        return self.exe is not None and not self.scripted

    # ------------------------------------------------------------- the tool --

    def review_diff(self, diff: str, *, context: str = "") -> Review:
        if self.scripted or not self.exe:
            return Review(findings=list(SCRIPTED_FINDINGS), scripted=True)
        try:
            proc = subprocess.run(
                [self.exe, "review", "--diff", "-", "--format", "json"],
                input=diff, capture_output=True, text=True, timeout=self.timeout,
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise RuntimeError(f"qodo review failed to run: {e}") from e
        if proc.returncode != 0:
            raise RuntimeError(f"qodo exited {proc.returncode}: {(proc.stderr or '')[:200]}")
        return Review(findings=_parse(proc.stdout), scripted=False)

    #: the MCP surface, for a host that wants to call this over the protocol
    def tools(self) -> list[dict]:
        return [{
            "name": self.tool_name,
            "description": "Review a unified diff before it is committed. Returns findings "
                           "with severity, location and a suggested fix.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "diff": {"type": "string", "description": "unified diff to review"},
                    "context": {"type": "string", "description": "what the change is trying to do"},
                },
                "required": ["diff"],
            },
        }]

    def call_tool(self, name: str, arguments: dict) -> dict:
        if name != self.tool_name:
            raise KeyError(f"no tool named {name!r}")
        review = self.review_diff(arguments.get("diff", ""), context=arguments.get("context", ""))
        return {"findings": [f.to_dict() for f in review.findings],
                "blocking": len(review.blocking), "scripted": review.scripted}


def _parse(stdout: str) -> list[Finding]:
    """Qodo's JSON, defensively: a reviewer whose output shape drifts must not
    take the run down with it."""
    try:
        data = json.loads(stdout or "{}")
    except ValueError:
        return []
    raw = data.get("findings") or data.get("suggestions") or []
    out: list[Finding] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        sev = str(item.get("severity") or item.get("priority") or "medium").lower()
        out.append(Finding(
            severity=sev if sev in SEVERITIES else "medium",
            title=str(item.get("title") or item.get("summary") or "finding")[:120],
            detail=str(item.get("detail") or item.get("description") or "")[:400],
            path=str(item.get("path") or item.get("file") or ""),
            line=int(item.get("line") or 0),
            fix=str(item.get("fix") or item.get("suggestion") or "")[:200],
        ))
    return out
