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
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

#: provider -> (openai-compatible base url or None for native, key env var, default model)
PROVIDERS: dict[str, tuple[str | None, str, str]] = {
    "anthropic": (None, "ANTHROPIC_API_KEY", "claude-sonnet-4-6"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY", "gpt-4o"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "llama-3.3-70b-versatile"),
    "kimi": ("https://api.moonshot.ai/v1", "MOONSHOT_API_KEY", "kimi-k2-0905-preview"),
    # the Claude Code CLI you are already signed into: no API key, no wire call --
    # ratchet shells out to `claude -p` and grades what comes back like any other
    "claude-code": (None, "", "default"),
    "trueforge": (None, "", "auto"),  # the local agent harness; TRUEFORGE_BASE_URL, no key
    # the TrueFoundry AI Gateway (cloud): OpenAI-compatible, key from
    # https://shryukg.truefoundry.cloud/gateway-onboarding — base overridable via TFY_BASE_URL
    "truefoundry": ("https://shryukg.truefoundry.cloud/api/llm/v1", "TFY_API_KEY", "auto"),
    "demo": (None, "", "demo"),
}


#: the dropdown behind /model: every provider's sensible choices, curated so the
#: picker is instant and offline. /model also accepts any free-form provider/model.
MODEL_CATALOG: dict[str, list[tuple[str, str]]] = {
    "anthropic": [
        ("claude-sonnet-4-6", "fast + sharp; the default"),
        ("claude-opus-4-6", "deepest reasoning"),
        ("claude-haiku-4-5", "cheapest, quick edits"),
    ],
    "openai": [
        ("gpt-5.2", "flagship"),
        ("gpt-5-mini", "fast + cheap"),
        ("gpt-4o", "solid all-rounder"),
    ],
    "groq": [
        ("llama-3.3-70b-versatile", "fast open-weights"),
        ("moonshotai/kimi-k2-instruct", "kimi k2, served fast"),
        ("llama-3.1-8b-instant", "instant, small"),
    ],
    "kimi": [
        ("kimi-k2-0905-preview", "k2 flagship"),
        ("kimi-k2-turbo-preview", "k2, faster"),
        ("moonshot-v1-32k", "long context"),
    ],
    "claude-code": [
        ("default", "your signed-in Claude Code — no API key needed"),
        ("opus", "deepest reasoning"),
        ("sonnet", "fast + sharp"),
        ("haiku", "quick edits"),
    ],
    "trueforge": [("auto", "whatever the harness routes — needs TrueForge on :8790")],
    "truefoundry": [("auto", "first model on the gateway — /connect truefoundry with your TFY key")],
    "demo": [("demo", "offline scaffolder — no key, no network")],
}

#: where /connect keeps keys: a 0600 env file, loaded before any env lookup, so
#: connecting once means connected next session too
KEYS_PATH = Path.home() / ".config" / "ratchet" / "keys.env"


def load_saved_keys() -> None:
    """Saved keys fill only the env vars that are unset -- a live export wins."""
    if not KEYS_PATH.exists():
        return
    for line in KEYS_PATH.read_text().splitlines():
        name, _, value = line.strip().partition("=")
        if name and value and not os.environ.get(name):
            os.environ[name] = value


def save_key(provider: str, key: str) -> Path:
    if provider not in PROVIDERS or not PROVIDERS[provider][1]:
        raise ChatProviderError(f"{provider!r} takes no key")
    env_name = PROVIDERS[provider][1]
    KEYS_PATH.parent.mkdir(parents=True, exist_ok=True)
    # ~/.config is a git repo in plenty of dotfiles setups; make sure the key
    # file can never ride along in a commit
    gi = KEYS_PATH.parent / ".gitignore"
    if not gi.exists():
        gi.write_text("keys.env\n")
    lines = [
        line for line in (KEYS_PATH.read_text().splitlines() if KEYS_PATH.exists() else [])
        if not line.startswith(f"{env_name}=")
    ]
    lines.append(f"{env_name}={key.strip()}")
    NL = chr(10)
    KEYS_PATH.write_text(NL.join(lines) + NL)
    KEYS_PATH.chmod(0o600)
    os.environ[env_name] = key.strip()
    return KEYS_PATH


def validate_key(provider: str, key: str, *, timeout: float = 20.0) -> str:
    """One cheap authenticated call; returns a human line or raises. This is what
    makes /connect honest -- a saved key that never worked is worse than none."""
    if provider == "demo":
        return "demo needs no key"
    if provider == "claude-code":
        if shutil.which("claude"):
            return "connected — using your signed-in Claude Code"
        raise ChatProviderError("the `claude` CLI is not on PATH; install Claude Code first")
    if provider == "trueforge":
        if trueforge_alive(ttl=0):
            return "connected — the harness is answering"
        raise ChatProviderError("no TrueForge answering — `npx @truefoundry/trueforge@latest` first")
    base, _env, _default = PROVIDERS[provider]
    if provider == "anthropic":
        r = httpx.get("https://api.anthropic.com/v1/models",
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout=timeout)
    else:
        r = httpx.get(f"{_base_for(provider, base)}/models",
                      headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
    _raise_for(r)
    n = len(r.json().get("data", []))
    return f"connected — {n} models visible"


# --------------------------------------------------------------------------- #
# secret hygiene: a pasted key must never reach a model, a bus file, a log line
# or a sandboxed child process. Belt everywhere the text flows.
# --------------------------------------------------------------------------- #

SECRET_RE = re.compile(
    r"(sk-ant-[A-Za-z0-9_-]{10,}"          # anthropic
    r"|sk-[A-Za-z0-9_-]{16,}"              # openai and friends
    r"|gsk_[A-Za-z0-9]{16,}"               # groq
    r"|tfy-[A-Za-z0-9._-]{10,}"            # truefoundry
    r"|(?:api[_-]?key|apikey|token|secret|password)\s*[:=]\s*['\"]?[A-Za-z0-9._-]{12,})",
    re.I,
)

#: env vars that must never leak into a sandbox where model-generated code runs
SECRET_ENV_PREFIXES = ("ANTHROPIC_", "OPENAI_", "GROQ_", "MOONSHOT_", "TFY_", "BRIGHTDATA_", "AWS_", "GITHUB_")
SECRET_ENV_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")


def looks_like_secret(text: str) -> bool:
    return bool(SECRET_RE.search(text or ""))


def redact(text: str) -> str:
    """Replace anything key-shaped before a string is logged, bussed or echoed."""
    return SECRET_RE.sub("<redacted>", text or "")


def scrub_env(env: dict[str, str]) -> dict[str, str]:
    """The environment a sandbox gets: provider keys removed, because the code
    running in there is model-generated and `print(os.environ)` is one line."""
    return {
        k: v for k, v in env.items()
        if not k.startswith(SECRET_ENV_PREFIXES) and not k.endswith(SECRET_ENV_SUFFIXES)
    }


_TF_CACHE: dict[str, object] = {}


def trueforge_alive(*, ttl: float = 10.0) -> bool:
    """Is a TrueForge instance answering? Cached briefly -- the palette asks on
    every keystroke and a dead socket costs a timeout."""
    import time as _time

    now = _time.monotonic()
    if _TF_CACHE.get("at", 0) and now - _TF_CACHE["at"] < ttl:  # type: ignore[operator]
        return bool(_TF_CACHE["ok"])
    base = os.environ.get("TRUEFORGE_BASE_URL", "http://localhost:8790")
    try:
        ok = httpx.get(f"{base}/api/v1/models", timeout=1.5).status_code < 500
    except Exception:
        ok = False
    _TF_CACHE.update(at=now, ok=ok)
    return ok


def connected_providers() -> dict[str, bool]:
    load_saved_keys()
    out = {}
    for name, (_b, key_env, _m) in PROVIDERS.items():
        if name == "claude-code":
            out[name] = bool(shutil.which("claude"))
        elif name == "trueforge":
            out[name] = trueforge_alive()
        else:
            out[name] = not key_env or bool(os.environ.get(key_env))
    return out


class ChatProviderError(RuntimeError):
    pass


@dataclass
class ChatBackend:
    provider: str
    model: str

    @classmethod
    def from_env(cls) -> ChatBackend:
        # `.env` first. This line is the whole bug report: the key lived in .env,
        # `Settings.from_env` read it and this did not, so the selector below saw no
        # key anywhere and quietly chose `demo` -- a provider that fabricates a reply
        # without a network call. The user had configured everything correctly and
        # got a canned answer, which is indistinguishable from the tool being fake.
        from .config import load_dotenv

        load_dotenv()
        load_saved_keys()  # /connect persists here; a fresh session picks them up
        provider = os.environ.get("RATCHET_CHAT_PROVIDER", "").strip().lower()
        if not provider:
            # Preference order, and `demo` is the last resort rather than the second.
            # Claude Code first when it is installed: no key to configure, it edits
            # the tree itself and narrates every step, and ratchet still gates and
            # commits what it produced. Then TrueForge, which already holds the
            # provider credentials, then the gateway, then any single keyed provider.
            if shutil.which("claude"):
                provider = "claude-code"
            elif trueforge_alive():
                provider = "trueforge"
            elif os.environ.get("TFY_API_KEY"):
                provider = "truefoundry"
            else:
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

    def complete(self, prompt: str, *, max_tokens: int = 8192, timeout: float | None = None) -> str:
        # a request that hangs past this shows up as a visible error instead of a
        # frozen pane; RATCHET_HTTP_TIMEOUT overrides for slow local gateways
        timeout = timeout or float(os.environ.get("RATCHET_HTTP_TIMEOUT", "120"))
        return self._complete(prompt, max_tokens=max_tokens, timeout=timeout)

    def _complete(self, prompt: str, *, max_tokens: int, timeout: float) -> str:
        if self.provider == "demo":
            return _demo_reply(prompt)
        if self.provider == "claude-code":
            return self._claude_code_complete(prompt, timeout=timeout)
        if self.provider == "trueforge":
            return self._trueforge_complete(prompt, max_tokens=max_tokens)
        base, key_env, _default = PROVIDERS[self.provider]
        key = os.environ.get(key_env, "") if key_env else ""
        routed_model = self.model
        via_gateway = False
        if gateway_only() and self.provider != "truefoundry":
            base, key, routed_model = _gateway_route(self.provider, self.model)
            via_gateway = True
        elif not key:
            raise ChatProviderError(
                f"{key_env} is not set; export it, or `/model demo` for the offline provider"
            )
        from . import debuglog

        if self.provider == "anthropic" and not via_gateway:
            debuglog.log("info", f"POST {ANTHROPIC_URL} model={self.model} timeout={timeout}s")
            r = httpx.post(
                ANTHROPIC_URL,
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                json={"model": self.model, "max_tokens": max_tokens,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=timeout,
            )
            debuglog.log("info", f"← {r.status_code} in {_elapsed(r):.1f}s")
            _raise_for(r)
            return "".join(part.get("text", "") for part in r.json().get("content", []))
        base = base if via_gateway else _base_for(self.provider, base)
        model = routed_model
        if via_gateway:
            debuglog.log("info", f"gateway route: {self.provider}/{self.model} -> {base} as {model}")
        if model == "auto":
            # gateways (TrueFoundry) route many models; with none named, take the
            # first the gateway lists rather than guessing a name it may not have
            lr = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
            _raise_for(lr)
            data = lr.json().get("data", [])
            if not data:
                raise ChatProviderError(f"{self.provider} lists no models; add one in its console")
            model = str(data[0].get("id") or data[0].get("name") or "")
            if not model:
                raise ChatProviderError(f"{self.provider} returned a model with no id")
            self.model = model  # pin it, so the activity line names something real
        body: dict[str, object] = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        # OpenAI renamed the cap for its newer models (gpt-5.x, o-series) and
        # rejects the old name outright. Rather than keep a model list that rots,
        # send what this model is known to want, and learn from a rejection.
        body[_TOKEN_PARAM.get(model, "max_tokens")] = max_tokens

        for attempt in (1, 2):
            debuglog.log("info", f"POST {base}/chat/completions model={model} "
                                 f"cap={next(k for k in body if k.startswith('max'))} timeout={timeout}s")
            r = httpx.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=body,
                timeout=timeout,
            )
            debuglog.log("info", f"← {r.status_code} in {_elapsed(r):.1f}s")
            swapped = _swap_token_param(r, body, model)
            if swapped and attempt == 1:
                debuglog.log("warn", f"{model} wants max_completion_tokens; retrying")
                continue
            break

        _raise_for(r)
        try:
            return r.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError) as e:
            raise ChatProviderError(f"{self.provider} returned an unexpected payload: {e}") from e


    @property
    def agentic(self) -> bool:
        """True when the provider does the editing itself and ratchet's job is to
        watch, verify and commit -- rather than parse fences out of a reply."""
        return self.provider == "claude-code"

    def run_agentic(self, prompt: str, repo, on_event, *, timeout: float = 900.0) -> str:
        """Drive a Claude Code session in `repo`, narrating it as it goes.

        This is the communication channel the console was missing. `claude
        --output-format stream-json` emits one JSON object per step -- each tool
        call, each result, each thing it says -- so the activity pane can show the
        work as it happens instead of a spinner and a wall of silence. Claude Code
        edits the working tree directly; ratchet's contribution is that the result
        still goes through the cheat gate and lands as one reviewable commit.

        `on_event(kind, text)` is called from this thread for every step.
        """
        import json as _json
        import subprocess

        from . import debuglog

        exe = shutil.which("claude")
        if not exe:
            raise ChatProviderError("the `claude` CLI is not on PATH — install Claude Code")
        argv = [
            exe, "-p", prompt,
            "--output-format", "stream-json", "--verbose",
            "--permission-mode", "acceptEdits",   # it may edit; ratchet still gates the diff
            "--add-dir", str(repo),
        ]
        if self.model and self.model != "default":
            argv += ["--model", self.model]
        debuglog.log("info", f"claude session starting in {repo} (model={self.model})")

        said: list[str] = []
        last = ""
        proc = subprocess.Popen(argv, cwd=str(repo), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)
        try:
            for line in proc.stdout or ():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = _json.loads(line)
                except ValueError:
                    continue
                text = _describe(ev)
                if not text:
                    continue
                # the closing summary arrives twice -- once as the assistant's last
                # message, once as the result envelope. Say it once.
                if text == last:
                    continue
                last = text
                if ev.get("type") == "result":
                    said.append(text)
                on_event("step", text)
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise ChatProviderError(f"the Claude Code session ran past {timeout}s") from None
        finally:
            if proc.stdout:
                proc.stdout.close()
        if proc.returncode not in (0, None):
            err = (proc.stderr.read() if proc.stderr else "")[:200]
            raise ChatProviderError(f"claude exited {proc.returncode}: {err.strip()}")
        debuglog.log("info", "claude session finished")
        return said[-1] if said else ""

    def _claude_code_complete(self, prompt: str, *, timeout: float) -> str:
        """Ask the local Claude Code CLI, headless.

        The point is that there is nothing to connect: if `claude` runs on this
        machine, the user is already authenticated, and ratchet borrows that
        session. Tools are disallowed -- Claude Code is asked for the same fenced
        answer every other provider gives, so the reply still goes through the
        cheat gate and lands as one reviewable commit rather than editing the
        working tree behind ratchet's back.
        """
        import subprocess

        from . import debuglog

        exe = shutil.which("claude")
        if not exe:
            raise ChatProviderError(
                "the `claude` CLI is not on PATH — install Claude Code, or /model something else"
            )
        argv = [exe, "-p", prompt, "--output-format", "text", "--disallowed-tools",
                "Edit,Write,NotebookEdit,Bash"]
        if self.model and self.model != "default":
            argv += ["--model", self.model]
        debuglog.log("info", f"exec claude -p (model={self.model}) timeout={timeout}s")
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise ChatProviderError(f"claude -p timed out after {timeout}s") from e
        except OSError as e:
            raise ChatProviderError(f"could not run claude: {e}") from e
        debuglog.log("info", f"claude exited {r.returncode} · {len(r.stdout)} chars")
        if r.returncode != 0:
            raise ChatProviderError(f"claude exited {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")
        return r.stdout

    def _trueforge_complete(self, prompt: str, *, max_tokens: int) -> str:
        """Route the turn through the TrueForge agent harness -- the same machinery
        the run loop uses -- instead of a direct provider wire. Sessions are cached
        on this backend so a conversation stays one harness session."""
        from .harness.backend import HarnessBackend
        from .harness.client import TrueForgeClient, TrueForgeError

        if not trueforge_alive():
            base = os.environ.get("TRUEFORGE_BASE_URL", "http://localhost:8790")
            raise ChatProviderError(
                f"no TrueForge at {base} — start it with `npx @truefoundry/trueforge@latest`"
            )
        if not hasattr(self, "_tf"):
            self._tf = HarnessBackend(TrueForgeClient(os.environ.get("TRUEFORGE_BASE_URL", "http://localhost:8790")))
        model = self.model
        if model == "auto":
            try:
                models = self._tf.client.models()
            except Exception as e:
                raise ChatProviderError(f"trueforge model listing failed: {e}") from e
            model = str((models[0].get("id") or models[0].get("name")) or "") if models else ""
            if not model:
                raise ChatProviderError("TrueForge lists no models — add a provider in its Settings")
        try:
            text, _tokens, _cost = self._tf.complete(prompt, model=model, role="chat", max_tokens=max_tokens)
        except TrueForgeError as e:
            raise ChatProviderError(f"trueforge: {e}") from e
        return text


#: model -> which token-cap parameter it accepts. Learned at runtime from the
#: provider's own rejection, so a new model never needs a code change.
_TOKEN_PARAM: dict[str, str] = {}


def _swap_token_param(r: httpx.Response, body: dict[str, object], model: str) -> bool:
    """True when the provider rejected the token-cap parameter and `body` has been
    rewritten to use the other spelling.

    OpenAI's newer models answer `max_tokens` with a 400 naming
    `max_completion_tokens` (and vice versa on older deployments). One retry with
    the other name is cheaper and more durable than tracking model families.
    """
    if r.status_code != 400:
        return False
    try:
        message = json.dumps(r.json()).lower()
    except ValueError:
        message = r.text.lower()
    for wrong, right in (("max_tokens", "max_completion_tokens"), ("max_completion_tokens", "max_tokens")):
        if wrong in body and right in message and "unsupported" in message:
            body[right] = body.pop(wrong)
            _TOKEN_PARAM[model] = right
            return True
    return False


#: tool names worth a line of their own; everything else is noise in a console
_LOUD_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "Read", "Glob", "Grep", "WebFetch"}


def _describe(ev: dict) -> str:
    """One activity line for one stream event, or "" for the ones nobody needs."""
    kind = ev.get("type")
    if kind == "system":
        return "session started" if ev.get("subtype") == "init" else ""
    if kind == "assistant":
        out = []
        for block in ev.get("message", {}).get("content", []):
            if block.get("type") == "tool_use":
                name = block.get("name", "tool")
                if name not in _LOUD_TOOLS:
                    continue
                args = block.get("input") or {}
                target = args.get("file_path") or args.get("path") or args.get("pattern") or args.get("command", "")
                target = str(target).split("/")[-1] if "/" in str(target) else str(target)
                out.append(f"{name.lower()} {target}"[:100])
            elif block.get("type") == "text":
                said = " ".join(str(block.get("text", "")).split())
                if said:
                    out.append(said[:110])
        return " · ".join(out)
    if kind == "user":
        for block in ev.get("message", {}).get("content", []):
            if block.get("type") == "tool_result" and block.get("is_error"):
                return f"tool failed: {str(block.get('content', ''))[:80]}"
        return ""
    if kind == "result":
        return " ".join(str(ev.get("result", "")).split())[:140]
    return ""


def _elapsed(r) -> float:
    """Response timing, defensively: the debug channel never breaks the call."""
    try:
        return r.elapsed.total_seconds()
    except Exception:
        return 0.0


#: When a TrueFoundry key is configured, every wire call leaves through the
#: gateway -- that is the whole point of putting a gateway in front of the
#: providers, and a direct call that bypasses it is invisible to its budgets,
#: logs and rate limits. Set RATCHET_GATEWAY_ONLY=0 to allow direct calls again.
def gateway_only() -> bool:
    load_saved_keys()
    if os.environ.get("RATCHET_GATEWAY_ONLY", "") in ("0", "false", "no"):
        return False
    return bool(os.environ.get("TFY_API_KEY"))


def _gateway_route(provider: str, model: str) -> tuple[str, str, str]:
    """Rewrite a direct provider call into a gateway call.

    Returns (base, key, model). TrueFoundry addresses a routed model as
    `provider/model`, which is exactly how ratchet already names them, so the
    model string usually passes through untouched.
    """
    base = os.environ.get("TFY_BASE_URL") or PROVIDERS["truefoundry"][0] or ""
    key = os.environ.get("TFY_API_KEY", "")
    routed = model if "/" in model else f"{provider}/{model}"
    return base, key, routed


def _base_for(provider: str, table_base: str | None) -> str | None:
    """A provider's base URL, env-overridable for the gateway case."""
    if provider == "truefoundry":
        return os.environ.get("TFY_BASE_URL") or table_base
    return table_base


def _raise_for(r: httpx.Response) -> None:
    """Raise the provider's own sentence, not its JSON.

    Every provider buries the useful line in a different shape; a wall of escaped
    JSON in the activity pane tells a user nothing they can act on.
    """
    if r.status_code < 400:
        return
    detail = ""
    try:
        payload = r.json()
        err = payload.get("error", payload) if isinstance(payload, dict) else {}
        if isinstance(err, dict):
            detail = str(err.get("message") or err.get("detail") or "")
        elif isinstance(err, str):
            detail = err
    except ValueError:
        pass
    if not detail:
        detail = r.text[:200]
    hint = {401: "  (check the key with /connect)", 404: "  (is that model available on this account?)",
            429: "  (rate limited — wait, or /model something else)"}.get(r.status_code, "")
    raise ChatProviderError(f"{r.request.url.host} {r.status_code}: {detail.strip()[:240]}{hint}")


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
