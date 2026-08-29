"""The slash-command palette behind the chat box: pure functions, so autocomplete
is unit-testable without a terminal.

Typing `/` opens the command list; every keystroke filters it; `/model ` switches
the list to every model in the catalog (connected providers first, tagged); Enter
or a click applies the highlighted row. The console is only the renderer.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..providers import MODEL_CATALOG, PROVIDERS, connected_providers

#: every command, one line each -- this dict IS /help
COMMANDS: dict[str, str] = {
    "/help": "every command, one line each",
    "/model": "pick the chat model — a dropdown of every provider and model",
    "/connect": "connect a provider account: /connect groq, then paste the API key",
    "/providers": "which providers are connected, and each one's default model",
    "/undo": "revert the last chat commit (git revert, nothing is lost)",
    "/last": "what the last turn did: intent, files, commit",
    "/clear": "clear the activity pane",
    "/quit": "leave the console",
}


@dataclass
class Row:
    label: str          # what the list shows
    value: str          # what applying the row does / inserts
    kind: str           # "command" | "model" | "provider"
    meta: str = ""


def rows_for(text: str) -> list[Row]:
    """The palette contents for the current input text. Empty list = palette closed."""
    text = text.lstrip()
    if not text.startswith("/"):
        return []
    head, _, rest = text.partition(" ")

    if head == "/model":
        live = connected_providers()
        rows = [
            Row(
                label=f"{provider}/{model}",
                value=f"{provider}/{model}",
                kind="model",
                meta=f"{note}" + ("" if live.get(provider) else "  · needs /connect"),
            )
            for provider, models in MODEL_CATALOG.items()
            for model, note in models
        ]
        rows.sort(key=lambda r: "needs /connect" in r.meta)  # connected first
        needle = rest.strip().lower()
        return [r for r in rows if needle in r.label.lower()] if needle else rows

    if head == "/connect":
        needle = rest.strip().lower()
        rows = [
            Row(label=name, value=name, kind="provider",
                meta=f"key env {key_env}" if key_env else "no key needed")
            for name, (_b, key_env, _m) in PROVIDERS.items()
            if name != "demo"
        ]
        return [r for r in rows if needle in r.label] if needle else rows

    # plain command filtering: "/mo" -> /model
    return [
        Row(label=cmd, value=cmd, kind="command", meta=desc)
        for cmd, desc in COMMANDS.items()
        if cmd.startswith(head.lower())
    ]


def help_lines() -> list[str]:
    width = max(len(c) for c in COMMANDS)
    return [f"{cmd:<{width}}  {desc}" for cmd, desc in COMMANDS.items()] + [
        "",
        "anything else you type is a coding request — Enter runs it in the background,",
        "Esc interrupts, and every finished turn is one git commit you can /undo.",
    ]
