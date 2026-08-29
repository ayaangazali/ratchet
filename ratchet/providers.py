"""Chat model providers, spoken over the wire with httpx.

Deliberately not provider SDKs -- this repository's design contract bans them (the
harness owns model routing for *runs*; see CLAUDE.md). The chat box is a different
animal: a direct, user-configured line to one model, so it speaks the two wire
protocols that cover the market -- Anthropic's messages API and the OpenAI-compatible
chat/completions shape that OpenAI, Groq and Kimi (Moonshot) all serve -- with
nothing but httpx, which was already a dependency.

Selection: RATCHET_CHAT_PROVIDER + RATCHET_CHAT_MODEL, or `/model <provider>[/<model>]`
typed straight into the chat box. With no key configured, the `demo` provider keeps
the whole flow usable offline: it fabricates a small static page from the prompt, so
`ratchet` -> type -> Enter works on a fresh laptop with zero setup.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import httpx

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

#: provider -> (openai-compatible base url or None for native, key env var, default model)
PROVIDERS: dict[str, tuple[str | None, str, str]] = {
    "anthropic": (None, "ANTHROPIC_API_KEY", "claude-sonnet-4-6"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY", "gpt-5.2"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "llama-3.3-70b-versatile"),
    "kimi": ("https://api.moonshot.ai/v1", "MOONSHOT_API_KEY", "kimi-k2-0905-preview"),
    "demo": (None, "", "demo"),
}


class ChatProviderError(RuntimeError):
    pass


@dataclass
class ChatBackend:
    provider: str
    model: str

    @classmethod
    def from_env(cls) -> ChatBackend:
        provider = os.environ.get("RATCHET_CHAT_PROVIDER", "").strip().lower()
        if not provider:
            # pick the first provider with a key; fall back to the offline demo
            provider = next(
                (name for name, (_b, key_env, _m) in PROVIDERS.items() if key_env and os.environ.get(key_env)),
                "demo",
            )
        if provider not in PROVIDERS:
            raise ChatProviderError(f"unknown chat provider {provider!r}; known: {sorted(PROVIDERS)}")
        model = os.environ.get("RATCHET_CHAT_MODEL") or PROVIDERS[provider][2]
        return cls(provider=provider, model=model)

    def switch(self, spec: str) -> str:
        """`/model groq` or `/model kimi/kimi-k2-0905-preview`."""
        provider, _, model = spec.strip().partition("/")
        provider = provider.lower()
        if provider not in PROVIDERS:
            raise ChatProviderError(f"unknown provider {provider!r}; known: {sorted(PROVIDERS)}")
        self.provider = provider
        self.model = model or PROVIDERS[provider][2]
        return f"{self.provider}/{self.model}"

    # ---------------------------------------------------------------- calls --

    def complete(self, prompt: str, *, max_tokens: int = 8192, timeout: float = 180.0) -> str:
        if self.provider == "demo":
            return _demo_reply(prompt)
        base, key_env, _default = PROVIDERS[self.provider]
        key = os.environ.get(key_env, "")
        if not key:
            raise ChatProviderError(
                f"{key_env} is not set; export it, or `/model demo` for the offline provider"
            )
        if self.provider == "anthropic":
            r = httpx.post(
                ANTHROPIC_URL,
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                json={"model": self.model, "max_tokens": max_tokens,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=timeout,
            )
            _raise_for(r)
            return "".join(part.get("text", "") for part in r.json().get("content", []))
        r = httpx.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": self.model, "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=timeout,
        )
        _raise_for(r)
        return r.json()["choices"][0]["message"]["content"] or ""


def _raise_for(r: httpx.Response) -> None:
    if r.status_code >= 400:
        try:
            detail = json.dumps(r.json())[:300]
        except Exception:
            detail = r.text[:300]
        raise ChatProviderError(f"{r.request.url.host} -> {r.status_code}: {detail}")


def _demo_reply(prompt: str) -> str:
    """No key, no network: turn the prompt into a small honest page, so the whole
    chat -> code -> commit loop is demonstrable on a fresh machine."""
    # the chat session hands over its whole rendered prompt; title from the part
    # the user actually typed
    ask = prompt.rsplit("User request:", 1)[-1].strip() if "User request:" in prompt else prompt
    ask = ask.split("Reply with ONE line", 1)[0].strip()
    title = " ".join(ask.split()[:8]) or "ratchet demo"
    body = ask.replace('"', "'")[:300]
    return (
        f"intent: scaffold a static page for: {title}\n\n"
        "```file:index.html\n"
        "<!doctype html>\n<html>\n<head>\n"
        f"  <meta charset=\"utf-8\">\n  <title>{title}</title>\n"
        "  <style>body{font-family:system-ui;margin:4rem auto;max-width:60ch;line-height:1.6}</style>\n"
        "</head>\n<body>\n"
        f"  <h1>{title}</h1>\n"
        f"  <p>Scaffolded by ratchet's offline demo provider from: {body}</p>\n"
        "  <p>Point RATCHET_CHAT_PROVIDER at anthropic/openai/groq/kimi for the real thing.</p>\n"
        "</body>\n</html>\n"
        "```\n"
    )
