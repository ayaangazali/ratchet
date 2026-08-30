#!/usr/bin/env python3
"""Point TrueForge at a model provider, in one command.

Setup friction is what kills a demo. The harness ships with no provider configured,
and configuring one by hand means finding the settings page, picking models out of a
list and pasting a key -- three chances to get it wrong with an audience watching.
This does it from the catalog the harness already publishes, so the model list is
whatever that version of TrueForge actually supports rather than whatever was true
when this script was written.

    export OPENAI_API_KEY=sk-...
    python scripts/setup_harness.py openai

    python scripts/setup_harness.py anthropic --key sk-ant-...
    python scripts/setup_harness.py --list          # what is configured now

The key is sent to localhost and stored by the harness. It is never written to this
repository, and it is never echoed -- the harness redacts it on read, and so do we.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("TRUEFORGE_BASE_URL", "http://localhost:8790")

#: Where each provider's key usually lives already, so the common case needs no flag.
KEY_ENV = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "google-gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "fireworks": ("FIREWORKS_API_KEY",),
    "together": ("TOGETHER_API_KEY",),
    "moonshot": ("MOONSHOT_API_KEY",),
    "zai": ("ZAI_API_KEY",),
    "alibaba": ("ALIBABA_API_KEY", "DASHSCOPE_API_KEY"),
    "truefoundry": ("TRUEFOUNDRY_API_KEY", "TFY_API_KEY"),
}

#: An OpenAI-compatible gateway registers as a `custom` provider: TrueForge's
#: catalog cannot know what models sit behind somebody's gateway, so we ask the
#: gateway itself. TrueFoundry's is the one we use, hence the default.
DEFAULT_GATEWAY = "https://default.truefoundry.cloud/api/llm/api/inference/openai"


def call(path: str, method: str = "GET", body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - localhost
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:600]
        raise SystemExit(f"{method} {path} -> {e.code}\n{detail}") from e
    except urllib.error.URLError as e:
        raise SystemExit(
            f"cannot reach TrueForge at {BASE}: {e.reason}\n"
            "  start it with:  npx @truefoundry/trueforge@latest"
        ) from e


def _key_from_env(provider: str) -> str:
    return next((os.environ[v] for v in KEY_ENV.get(provider, ()) if os.environ.get(v)), "")


def _gateway(key: str, base_url: str) -> int:
    """Register an OpenAI-compatible gateway as a `custom` provider.

    The model list comes from the gateway rather than from TrueForge's catalog,
    because the catalog cannot know what somebody has put behind their own endpoint.
    """
    if not key:
        raise SystemExit("no API key: pass --key, or set TRUEFOUNDRY_API_KEY")
    req = urllib.request.Request(f"{base_url}/models",
                                 headers={"Authorization": f"Bearer {key}", "User-Agent": "ratchet/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - user-supplied gateway
            models = json.loads(r.read())["data"]
    except Exception as e:
        raise SystemExit(f"cannot list models at {base_url}: {str(e)[:200]}") from e
    if not models:
        raise SystemExit(f"{base_url} exposes no models to this key")

    manifest = {
        "type": "custom",
        "name": "truefoundry",
        "base_url": base_url,
        "auth": {"api_key": key},
        "models": [
            {"model_id": m["id"], "name": m["id"].replace("/", "-").replace(".", "-"),
             "properties": {"context_length": 400000, "max_output_tokens": 32000}}
            for m in models
        ],
    }
    existing = {p.get("name") for p in (call("/api/v1/settings/model-providers").get("data") or [])}
    call("/api/v1/settings/model-providers", "PUT" if "truefoundry" in existing else "POST",
         {"manifest": manifest})
    names = [m["name"] for m in manifest["models"]]
    print(f"\n  truefoundry gateway registered: {len(names)} model(s)")
    for n in names:
        print(f"    - truefoundry/{n}")
    print("\n  route Ratchet through it:")
    print(f"    RATCHET_GENERATORS=truefoundry/{names[0]}")
    print(f"    RATCHET_MODEL_CARTOGRAPHER=truefoundry/{names[0]}\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="configure a TrueForge model provider")
    ap.add_argument("provider", nargs="?", help="openai | anthropic | google-gemini | ...")
    ap.add_argument("--key", help="API key; defaults to the provider's usual env var")
    ap.add_argument("--list", action="store_true", help="show what is configured and stop")
    ap.add_argument("--base-url", help=f"OpenAI-compatible gateway (default {DEFAULT_GATEWAY})")
    args = ap.parse_args()

    if args.list or not args.provider:
        configured = call("/api/v1/settings/model-providers").get("data") or []
        models = call("/api/v1/models").get("data") or []
        print(f"\n  {BASE}")
        print(f"  providers configured : {[p.get('type') or p.get('name') for p in configured] or 'none'}")
        print(f"  models available     : {len(models)}")
        for m in models[:12]:
            print(f"    - {m.get('id') or m.get('model_id') or m.get('name')}")
        if not configured:
            print("\n  nothing configured. Add one:  python scripts/setup_harness.py openai --key sk-...")
        print()
        return 0

    if args.provider == "truefoundry":
        return _gateway(args.key or _key_from_env("truefoundry"), args.base_url or DEFAULT_GATEWAY)

    catalog = {c["type"]: c for c in call("/api/v1/catalogs/model-providers").get("data") or []}
    entry = catalog.get(args.provider)
    if not entry:
        raise SystemExit(f"unknown provider {args.provider!r}; catalog has {sorted(catalog)}")
    if not entry.get("models"):
        raise SystemExit(f"{args.provider} publishes no models in this harness version")

    key = args.key or _key_from_env(args.provider)
    if not key:
        envs = " or ".join(KEY_ENV.get(args.provider, ("<PROVIDER>_API_KEY",)))
        raise SystemExit(f"no API key: pass --key, or set {envs}")

    manifest = {
        "type": args.provider,
        "auth": {"api_key": key},
        # Every model the catalog offers. Ratchet fans out across providers on
        # purpose, and a provider configured with one model silently collapses that
        # fan-out into a best-of-1.
        "models": [
            {"model_id": m["model_id"], "name": m["name"], "properties": m.get("properties") or {}}
            for m in entry["models"]
        ],
    }

    existing = {p.get("type") for p in (call("/api/v1/settings/model-providers").get("data") or [])}
    method = "PUT" if args.provider in existing else "POST"
    call("/api/v1/settings/model-providers", method, {"manifest": manifest})

    models = call("/api/v1/models").get("data") or []
    print(f"\n  {args.provider}: {len(manifest['models'])} model(s) registered "
          f"({'updated' if method == 'PUT' else 'created'})")
    print(f"  harness now exposes {len(models)} model(s) in total\n")
    for m in models[:12]:
        print(f"    - {m.get('id') or m.get('model_id') or m.get('name')}")
    print("\n  next:  ratchet run --repo demo-repo\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
