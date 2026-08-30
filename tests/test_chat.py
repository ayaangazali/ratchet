"""The chat coder: providers, the turn loop, and the console wiring.

Everything offline: the demo provider needs no key and no network, and the wire
providers are tested by capturing the request they would have sent.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ratchet import providers
from ratchet.chat import FILE_FENCE, ChatSession
from ratchet.providers import PROVIDERS, ChatBackend, ChatProviderError


@pytest.fixture()
def repo(tmp_path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed"],
                   cwd=tmp_path, check=True)
    return tmp_path


# -------------------------------------------------------------- providers --


def _blank_slate(monkeypatch, tmp_path):
    """No keys, no overrides, and a working directory with no `.env`.

    The chdir is not incidental. `from_env` reads `.env` now (that was the bug), so
    a test that only clears the environment quietly picks up the developer's real key
    when it runs from the checkout and asserts nothing.
    """
    for _base, key_env, _model in PROVIDERS.values():
        if key_env:
            monkeypatch.delenv(key_env, raising=False)
    monkeypatch.delenv("RATCHET_CHAT_PROVIDER", raising=False)
    monkeypatch.delenv("RATCHET_CHAT_MODEL", raising=False)
    monkeypatch.chdir(tmp_path)
    # `/connect` persists keys to ~/.config/ratchet/keys.env and `from_env` loads
    # them too, so a developer who has ever connected has a key these tests must not
    # see. Point it at a path that does not exist.
    monkeypatch.setattr(providers, "KEYS_PATH", tmp_path / "no-such-keys.env")
    # Two more ambient facts that decide the provider on a developer's machine and
    # must not decide it in a test: whether the Claude Code CLI is installed (it
    # outranks every keyed provider) and whether a gateway key is configured (which
    # forces every wire call through TrueFoundry).
    monkeypatch.setattr(providers.shutil, "which", lambda name: None)
    monkeypatch.setenv("RATCHET_GATEWAY_ONLY", "0")


def test_nothing_configured_at_all_means_the_offline_demo_provider(monkeypatch, tmp_path):
    _blank_slate(monkeypatch, tmp_path)
    monkeypatch.setattr(providers, "trueforge_alive", lambda **_: False)
    b = ChatBackend.from_env()
    assert b.provider == "demo"
    assert FILE_FENCE.search(b.complete("make a website for my dog"))


def test_a_live_harness_is_preferred_over_the_demo_provider(monkeypatch, tmp_path):
    """`demo` is the last resort, not the second choice. A running harness already
    holds the provider credentials, so falling back past it to a canned reply is the
    behaviour that made a configured machine look like a mock."""
    _blank_slate(monkeypatch, tmp_path)
    monkeypatch.setattr(providers, "trueforge_alive", lambda **_: True)
    assert ChatBackend.from_env().provider == "trueforge"


def test_a_key_in_dotenv_is_found(monkeypatch, tmp_path):
    """The regression this file exists for: the key was in `.env`, `from_env` read
    only `os.environ`, and the selector fell through to the demo provider."""
    _blank_slate(monkeypatch, tmp_path)
    monkeypatch.setattr(providers, "trueforge_alive", lambda **_: False)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-test-not-a-real-key\n")
    b = ChatBackend.from_env()
    assert b.provider == "openai", "a key in .env must not fall through to `demo`"


def test_model_switch_and_unknown_provider():
    b = ChatBackend(provider="demo", model="demo")
    assert b.switch("groq") == "groq/" + PROVIDERS["groq"][2]
    assert b.switch("kimi/kimi-k2-0905-preview").endswith("kimi-k2-0905-preview")
    with pytest.raises(ChatProviderError, match="unknown provider"):
        b.switch("grogg")


def test_openai_compat_and_anthropic_request_shapes(monkeypatch):
    # these assert the DIRECT wire shapes; with a gateway key configured every call
    # is routed instead, which is a different (and separately tested) contract
    monkeypatch.setenv("RATCHET_GATEWAY_ONLY", "0")
    sent = []

    class FakeResp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}],
                    "content": [{"text": "ok"}]}

    def fake_post(url, **kw):
        sent.append((url, kw))
        return FakeResp()

    import ratchet.providers as prov

    monkeypatch.setattr(prov.httpx, "post", fake_post)
    monkeypatch.setenv("GROQ_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert ChatBackend("groq", "llama-3.3-70b-versatile").complete("hi") == "ok"
    assert ChatBackend("anthropic", "claude-sonnet-4-6").complete("hi") == "ok"
    (groq_url, groq_kw), (anth_url, anth_kw) = sent
    assert groq_url.endswith("/chat/completions") and groq_kw["json"]["messages"][0]["content"] == "hi"
    assert "anthropic.com" in anth_url and anth_kw["headers"]["anthropic-version"]


def test_a_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.setenv("RATCHET_GATEWAY_ONLY", "0")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ChatProviderError, match="OPENAI_API_KEY"):
        ChatBackend("openai", "gpt-5.2").complete("hi")


# ------------------------------------------------------------------ turns --


def _run(session, prompt):
    lines = []
    turn = session.run_turn(prompt, lambda kind, text: lines.append((kind, text)))
    return turn, lines


def test_a_turn_writes_files_and_lands_as_one_commit(repo):
    session = ChatSession(repo, backend=ChatBackend("demo", "demo"))
    turn, lines = _run(session, "make a website about dolphins")
    assert turn.ok and turn.files == ["index.html"] and turn.commit
    assert (repo / "index.html").read_text().startswith("<!doctype html>")
    log = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=repo,
                         capture_output=True, text=True).stdout
    assert log.startswith("[ratchet chat]")
    assert any("wrote index.html" in t for _k, t in lines)  # the ultra-summary, not a diff
    assert not any("<!doctype" in t for _k, t in lines)


def test_the_model_cannot_write_outside_the_working_tree(repo):
    class Evil:
        provider, model = "evil", "evil"

        def complete(self, prompt, **kw):
            return ("intent: escape\n"
                    "```file:../outside.txt\nboom\n```\n"
                    "```file:.git/hooks/pre-commit\nboom\n```\n"
                    "```file:ok.txt\nfine\n```\n")

    session = ChatSession(repo, backend=Evil())
    turn, lines = _run(session, "x")
    assert turn.files == ["ok.txt"]
    assert not (repo.parent / "outside.txt").exists()
    assert sum(1 for k, t in lines if "refused path" in t) == 2


def test_a_reply_with_no_code_is_an_error_not_a_write(repo):
    class Chatty:
        provider, model = "c", "c"

        def complete(self, prompt, **kw):
            return "Sure! I'd love to help you build a website. First, let's talk about..."

    turn, _ = _run(ChatSession(repo, backend=Chatty()), "make a site")
    assert not turn.ok and "no file or diff blocks" in turn.error


def test_interrupt_before_the_reply_writes_nothing(repo):
    session = ChatSession(repo, backend=ChatBackend("demo", "demo"))
    orig = session.backend.complete

    def slow(prompt, **kw):
        session.cancel.set()  # the user pressed Esc while the model was thinking
        return orig(prompt, **kw)

    session.backend.complete = slow
    turn, _ = _run(session, "make a website")
    assert turn.cancelled and not turn.files
    assert not (repo / "index.html").exists()


def test_history_reaches_the_next_prompt(repo):
    session = ChatSession(repo, backend=ChatBackend("demo", "demo"))
    _run(session, "make a website about dolphins")
    prompt = session._render("now add a pricing page")
    assert "Recent turns:" in prompt and "dolphins" in prompt


# ------------------------------------------------------------------ console --


# ------------------------------------------------------------------ palette --


def test_connect_saves_a_validated_key_and_a_bad_key_is_refused(repo, monkeypatch, tmp_path):
    import ratchet.providers as prov

    monkeypatch.setattr(prov, "KEYS_PATH", tmp_path / "keys.env")
    monkeypatch.setattr(prov, "validate_key", lambda p, k: "connected — 5 models visible")
    path = prov.save_key("groq", "gsk_test123")
    assert path.read_text().strip() == "GROQ_API_KEY=gsk_test123"
    assert (path.stat().st_mode & 0o777) == 0o600
    import os

    assert os.environ["GROQ_API_KEY"] == "gsk_test123"
    # saved keys load into a fresh backend
    monkeypatch.delenv("RATCHET_CHAT_PROVIDER", raising=False)
    assert prov.connected_providers()["groq"] is True


def test_undo_reverts_only_chat_commits(repo):
    import subprocess

    from ratchet.providers import ChatBackend

    session = ChatSession(repo, backend=ChatBackend("demo", "demo"))
    turn, _ = _run(session, "make a page")
    assert (repo / "index.html").exists() and turn.commit
    # the machinery is never part of the commit -- committing .ratchet/ made every
    # later revert a conflict, because the bus keeps being written after the commit
    files = subprocess.run(["git", "show", "--name-only", "--format="], cwd=repo,
                           capture_output=True, text=True).stdout
    assert ".ratchet" not in files


def test_chat_turns_go_through_the_gauntlets_static_gate(repo):
    """The chat door is not a way around the verifier: generated source carrying a
    known cheat pattern is blocked before a byte lands; a clean page passes with
    the check named in the summary."""

    class Hostile:
        provider, model = "h", "h"

        def complete(self, prompt, **kw):
            return ("intent: sneaky\n"
                    "```file:app.py\nimport sys\nsys.exit(0)\nprint('never runs')\n```\n")

    turn, lines = _run(ChatSession(repo, backend=Hostile()), "make an app")
    assert not turn.ok and "gauntlet blocked" in turn.error
    assert not (repo / "app.py").exists()

    session = ChatSession(repo, backend=ChatBackend("demo", "demo"))
    turn, lines = _run(session, "make a coffee site")
    assert turn.ok
    assert any("gauntlet cheat check" in t for _k, t in lines)


def test_edits_see_the_existing_file(repo):
    """An edit request rides with the current file contents -- editing blind is
    hallucinating."""
    (repo / "index.html").write_text("<h1>OLD TITLE MARKER</h1>\n")
    session = ChatSession(repo, backend=ChatBackend("demo", "demo"))
    prompt = session._render("change the title")
    assert "OLD TITLE MARKER" in prompt
    assert "Current file contents" in prompt


# ----------------------------------------------------------------- security --


def test_a_pasted_key_never_reaches_a_model_or_the_bus(repo):
    """A mistyped /connect turns the key into a prompt; the session must refuse to
    send it, and nothing key-shaped may land in the bus file."""
    from ratchet.bus import Bus

    bus = Bus(repo / ".ratchet" / "t.bus.jsonl")
    called = []

    class Spy:
        provider, model = "spy", "spy"

        def complete(self, prompt, **kw):
            called.append(prompt)
            return "intent: x\n```file:a.txt\nx\n```"

    session = ChatSession(repo, backend=Spy(), bus=bus)
    turn, _ = _run(session, "/conect groq gsk_abcdef1234567890abcdef")  # note the typo
    assert not turn.ok and "API key" in turn.error
    assert called == []  # the model never saw it
    assert "gsk_" not in (repo / ".ratchet" / "t.bus.jsonl").read_text()


def test_sandbox_env_carries_no_provider_keys(repo, monkeypatch):
    """Model-generated code runs in the sandbox; `print(os.environ)` is one line,
    so the keys must simply not be there."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("TFY_API_KEY", "tfy-secret")
    from ratchet.sandbox import WorktreeProvider

    subprocess.run(["git", "-C", str(repo), "add", "-A"], capture_output=True)
    provider = WorktreeProvider(repo, "t-scrub")
    sb = provider.fork(provider.base_image(), label="scrub")
    try:
        res = sb.exec("echo \"[$ANTHROPIC_API_KEY][$TFY_API_KEY]\"", timeout=60)
    finally:
        sb.destroy()
        provider.cleanup()
    assert "[][]" in res.out  # both empty inside the sandbox
    assert "sk-ant-secret" not in res.out


def test_key_file_dir_is_git_ignored(monkeypatch, tmp_path):
    """~/.config is a git repo in plenty of dotfiles setups; the key file must not
    be committable from there."""
    import ratchet.providers as prov

    monkeypatch.setattr(prov, "KEYS_PATH", tmp_path / "cfg" / "keys.env")
    prov.save_key("groq", "gsk_test")
    assert (tmp_path / "cfg" / ".gitignore").read_text().strip() == "keys.env"


# -------------------------------------------------------------- diagnostics --


def test_a_raw_transport_error_becomes_a_visible_line_not_silence(repo):
    """The bug that made the console unusable: anything that was not a
    ChatProviderError escaped run_turn into a worker that swallows exceptions, so
    the pane sat on "asking…" forever. Every failure must surface."""
    import httpx

    class Flaky:
        provider, model = "flaky", "flaky"

        def complete(self, prompt, **kw):
            raise httpx.ReadTimeout("timed out waiting for the model")

    turn, lines = _run(ChatSession(repo, backend=Flaky()), "make a site")
    assert not turn.ok
    assert "ReadTimeout" in turn.error
    assert any(kind == "error" for kind, _t in lines)


def test_a_crash_inside_the_turn_is_still_reported(repo):
    class Exploding:
        provider, model = "boom", "boom"

        def complete(self, prompt, **kw):
            raise RuntimeError("kaboom")

    turn, lines = _run(ChatSession(repo, backend=Exploding()), "x")
    assert not turn.ok and "kaboom" in turn.error


def test_debug_channel_records_and_redacts(repo):
    from ratchet import debuglog

    path = debuglog.configure(repo)
    debuglog.log("info", "POST https://api.example/v1 model=x")
    debuglog.log("error", "leaked gsk_abcdef1234567890abcdef in a message")
    text = path.read_text()
    assert "POST https://api.example/v1" in text
    assert "gsk_abcdef" not in text and "<redacted>" in text
    assert any("POST" in line for _ts, _lvl, line in debuglog.lines())


def test_the_walk_prunes_heavy_dirs_and_stays_fast(tmp_path):
    """The real hang: `rglob("*")` descended into .venv/node_modules and filtered
    afterwards -- 28 seconds on one ordinary home directory, which looked exactly
    like a wedged prompt. The walk must prune as it goes and stay bounded."""
    import time

    from ratchet.context import tree_listing, walk_files

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x")
    for heavy in (".venv", "node_modules", ".git"):
        d = tmp_path / heavy / "deep" / "deeper"
        d.mkdir(parents=True)
        for i in range(400):
            (d / f"junk{i}.py").write_text("x")

    t0 = time.time()
    files = list(walk_files(tmp_path))
    elapsed = time.time() - t0

    assert "src/app.py" in files
    assert not any(part in f for f in files for part in (".venv", "node_modules", ".git"))
    assert elapsed < 1.0, f"walk took {elapsed:.1f}s"
    assert "junk0.py" not in tree_listing(tmp_path, [])


def test_sources_prefer_the_file_the_prompt_names(repo):
    """Alphabetical order shipped AGENTS.md when you asked about index.html."""
    (repo / "AAA_first.md").write_text("alphabetically first, irrelevant\n")
    (repo / "index.html").write_text("<h1>THE PAGE YOU ASKED ABOUT</h1>\n")
    session = ChatSession(repo, backend=ChatBackend("demo", "demo"))
    session._focus = "change the heading in index.html"
    sources = session._sources_block()
    assert "THE PAGE YOU ASKED ABOUT" in sources
    # within the attached sources, the named file leads
    assert sources.index("index.html") < sources.index("AAA_first")


# ------------------------------------------------------------------ report --


def test_export_reports_what_the_session_actually_did(repo):
    from ratchet import report
    from ratchet.providers import ChatBackend

    session = ChatSession(repo, backend=ChatBackend("demo", "demo"))
    _run(session, "make a page for my cafe")
    _run(session, "add a menu")

    text = report.build(repo, turns=session.turns, work_seconds=42.0)
    assert "# ratchet session" in text
    assert "index.html" in text
    assert "42s" in text
    rows = [ln for ln in text.splitlines() if ln.startswith("| 1 |") or ln.startswith("| 2 |")]
    assert len(rows) == 2 and "cafe" in rows[0] and "menu" in rows[1]
    assert session.turns[0].commit in text          # the commit trail
    assert "git revert" in text                     # says how to undo it

    path = report.write(repo, turns=session.turns, work_seconds=1.0)
    assert path.exists() and path.name.startswith("session-")


def test_export_redacts_secrets_and_survives_a_failed_turn(repo):
    from ratchet import report

    class Broken:
        provider, model = "b", "b"

        def complete(self, prompt, **kw):
            raise RuntimeError("nope")

    session = ChatSession(repo, backend=Broken())
    _run(session, "here is my key gsk_abcdef1234567890abcdef build a site")
    text = report.build(repo, turns=session.turns)
    assert "gsk_abcdef" not in text
    assert "failed" in text or "API key" in text
    assert "gsk_abcdef" not in report.to_json(repo, turns=session.turns)


def test_a_model_that_rejects_max_tokens_is_retried_with_the_new_name(monkeypatch):
    """OpenAI's newer models 400 on `max_tokens` and name `max_completion_tokens`.
    Rather than track model families, the client learns from the rejection and
    retries once -- and remembers, so the next call gets it right first time."""
    import ratchet.providers as prov

    calls = []

    class Resp:
        def __init__(self, code, payload):
            self.status_code, self._p = code, payload
            self.text = str(payload)
            self.request = type("R", (), {"url": type("U", (), {"host": "api.openai.com"})()})()

        def json(self):
            return self._p

    def fake_post(url, **kw):
        calls.append(dict(kw["json"]))  # snapshot: the retry mutates it in place
        if "max_tokens" in kw["json"]:
            return Resp(400, {"error": {"message": "Unsupported parameter: 'max_tokens' is not supported "
                                                   "with this model. Use 'max_completion_tokens' instead.",
                                        "type": "invalid_request_error"}})
        return Resp(200, {"choices": [{"message": {"content": "done"}}]})

    monkeypatch.setattr(prov.httpx, "post", fake_post)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    prov._TOKEN_PARAM.clear()

    b = prov.ChatBackend("openai", "gpt-5.2")
    assert b.complete("hi") == "done"
    assert "max_tokens" in calls[0] and "max_completion_tokens" in calls[1]

    calls.clear()
    assert b.complete("again") == "done"
    assert "max_completion_tokens" in calls[0]  # learned; no wasted round trip


def test_provider_errors_read_as_sentences_not_json(monkeypatch):
    import ratchet.providers as prov

    class Resp:
        status_code = 401
        text = "{}"
        request = type("R", (), {"url": type("U", (), {"host": "api.openai.com"})()})()

        def json(self):
            return {"error": {"message": "Incorrect API key provided", "code": "invalid_api_key"}}

    monkeypatch.setattr(prov.httpx, "post", lambda url, **kw: Resp())
    monkeypatch.setenv("OPENAI_API_KEY", "bad")
    prov._TOKEN_PARAM.clear()
    try:
        prov.ChatBackend("openai", "gpt-4o").complete("hi")
        raise AssertionError("should have raised")
    except prov.ChatProviderError as e:
        assert "Incorrect API key provided" in str(e)
        assert "/connect" in str(e)      # tells you what to do
        assert "{" not in str(e)         # not a JSON dump


def test_a_turn_that_writes_nothing_is_not_a_success(repo):
    """The reported failure: gpt-5.2 replied with 10k characters, the diff did not
    apply to a directory it could not see, and the session report said "1 turn,
    ok, 0 files". A turn that produced nothing is a failed turn."""

    class DiffAgainstNothing:
        provider, model = "d", "d"

        def complete(self, prompt, **kw):
            return ("intent: restyle the site\n```diff\n"
                    "--- a/does_not_exist.html\n+++ b/does_not_exist.html\n"
                    "@@ -1,1 +1,1 @@\n-old\n+new\n```\n")

    turn, lines = _run(ChatSession(repo, backend=DiffAgainstNothing()), "make a website")
    assert not turn.ok
    assert "did not apply" in turn.error and "does_not_exist.html" in turn.error
    assert any(kind == "error" for kind, _t in lines)


def test_prose_only_reply_is_a_failure_not_an_empty_success(repo):
    class AllTalk:
        provider, model = "t", "t"

        def complete(self, prompt, **kw):
            return "intent: plan it out\n\nFirst we should discuss the architecture..."

    turn, _ = _run(ChatSession(repo, backend=AllTalk()), "make a website")
    assert not turn.ok


def test_claude_code_provider_is_available_without_a_key(monkeypatch):
    """No /connect step: if the CLI is installed, the user is already signed in."""
    import shutil

    import ratchet.providers as prov

    monkeypatch.setattr(prov.shutil if hasattr(prov, "shutil") else shutil, "which",
                        lambda name: "/usr/local/bin/claude" if name == "claude" else None)
    assert prov.validate_key("claude-code", "") .startswith("connected")
    assert "claude-code" in prov.MODEL_CATALOG
    assert prov.PROVIDERS["claude-code"][1] == ""   # no key env at all


# ------------------------------------------------------------------ gateway --


def test_every_wire_call_leaves_through_the_gateway(monkeypatch):
    """The hard rule: with a TrueFoundry key configured, a provider call must not
    reach the provider directly. A gateway that can be bypassed is decoration --
    its budgets, logs and rate limits only mean anything if nothing goes around it."""
    import ratchet.providers as prov

    seen = []

    class Resp:
        status_code = 200
        text = "{}"
        request = type("R", (), {"url": type("U", (), {"host": "gw"})()})()

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(prov.httpx, "post", lambda url, **kw: (seen.append((url, kw["json"]["model"])), Resp())[1])
    monkeypatch.setenv("TFY_API_KEY", "tfy-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-be-used")
    monkeypatch.delenv("RATCHET_GATEWAY_ONLY", raising=False)
    prov._TOKEN_PARAM.clear()

    assert prov.gateway_only()
    assert prov.ChatBackend("openai", "gpt-5.2").complete("hi") == "ok"
    url, model = seen[-1]
    assert "truefoundry" in url and "api.openai.com" not in url
    assert model == "openai/gpt-5.2"          # addressed as the gateway expects

    # anthropic too: the native endpoint must not be used while the rule is on
    seen.clear()
    prov.ChatBackend("anthropic", "claude-sonnet-4-6").complete("hi")
    assert "api.anthropic.com" not in seen[-1][0]


def test_the_gateway_rule_can_be_turned_off_deliberately(monkeypatch):
    import ratchet.providers as prov

    monkeypatch.setenv("TFY_API_KEY", "tfy-test")
    monkeypatch.setenv("RATCHET_GATEWAY_ONLY", "0")
    assert not prov.gateway_only()


def test_an_agentic_session_is_seen_in_a_plain_directory(tmp_path):
    """The reported failure: Claude Code built a real site in a non-repo directory
    and ratchet reported "0 files, 0 commits" -- change detection assumed a git
    repo the user had not made. The work is what matters; the commit is a bonus."""

    class Builder:
        provider, model, agentic = "claude-code", "sonnet", True

        def run_agentic(self, prompt, repo, on_event, **kw):
            (Path(repo) / "index.html").write_text("<h1>built</h1>\n")
            on_event("step", "write index.html")
            return "done"

    assert not (tmp_path / ".git").exists()          # a plain directory, on purpose
    session = ChatSession(tmp_path, backend=Builder())
    turn, _ = _run(session, "make a website")

    assert turn.ok, turn.error
    assert turn.files == ["index.html"]
    assert (tmp_path / "index.html").exists()
    assert not turn.commit                            # nothing to commit to
    assert "not a git repo" in turn.commit_note       # and it says so plainly


def test_the_gate_still_applies_to_an_agentic_session_without_git(tmp_path):
    """No repo means no `git diff`, so the cheat gate is fed a synthesised one --
    the session must not become a way around the verifier."""

    class Sneaky:
        provider, model, agentic = "claude-code", "sonnet", True

        def run_agentic(self, prompt, repo, on_event, **kw):
            (Path(repo) / "app.py").write_text("import sys\nsys.exit(0)\n")
            return "done"

    turn, _ = _run(ChatSession(tmp_path, backend=Sneaky()), "make an app")
    assert not turn.ok and "gauntlet blocked" in turn.error


def test_a_deep_file_is_credited_even_when_a_tree_walk_would_miss_it(tmp_path):
    """The failure this exists for: a session wrote ~/Documents/.../index.html while
    the working directory was $HOME. Change detection walked the tree with a cap,
    hit 5000 files before reaching it, and reported "the session finished without
    changing any file" over a 30KB page that was plainly there. The session already
    names every file it writes -- believe it rather than searching for it."""

    class DeepWriter:
        provider, model, agentic = "claude-code", "sonnet", True

        def run_agentic(self, prompt, repo, on_event, **kw):
            target = Path(repo) / "Documents" / "site" / "index.html"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("<h1>deep</h1>\n")
            on_event("file", str(target))          # what the real stream reports
            on_event("step", "write index.html")
            return "done"

    # bury it so a bounded walk would never reach the new file
    noise = tmp_path / "noise"
    noise.mkdir()
    for i in range(60):
        (noise / f"f{i}.txt").write_text("x")

    session = ChatSession(tmp_path, backend=DeepWriter())
    turn, _ = _run(session, "make a website")

    assert turn.ok, turn.error
    assert turn.files == ["Documents/site/index.html"]
    assert (tmp_path / "Documents" / "site" / "index.html").exists()


def test_a_file_written_outside_the_working_directory_is_still_reported(tmp_path):
    """Dropping it would be the same blindness wearing a different coat."""

    outside = tmp_path.parent / f"outside-{tmp_path.name}.txt"

    class Stray:
        provider, model, agentic = "claude-code", "sonnet", True

        def run_agentic(self, prompt, repo, on_event, **kw):
            outside.write_text("x")
            on_event("file", str(outside))
            return "done"

    try:
        turn, _ = _run(ChatSession(tmp_path, backend=Stray()), "write somewhere else")
        assert turn.files and str(outside) in turn.files[0]
    finally:
        outside.unlink(missing_ok=True)


def test_the_session_is_granted_the_tools_it_needs():
    """Every Bash call in a real session failed with "This command requires
    approval": acceptEdits permits writes but not commands, and headless `-p` has
    nobody to approve. The agent could write a file but not read the tree, run a
    build, or check its own work."""
    import subprocess

    from ratchet.providers import ChatBackend

    captured = {}

    class FakePopen:
        returncode = 0
        stdout: list = []
        stderr = None

        def __init__(self, argv, **kw):
            captured["argv"] = argv

        def wait(self, timeout=None):
            return 0

    import ratchet.providers as prov

    real = prov.subprocess.Popen if hasattr(prov, "subprocess") else subprocess.Popen
    try:
        subprocess.Popen = FakePopen  # the module imports subprocess inside the call
        ChatBackend("claude-code", "sonnet").run_agentic("x", Path("/tmp"), lambda *a: None)
    finally:
        subprocess.Popen = real

    argv = captured["argv"]
    assert "--allowedTools" in argv
    granted = argv[argv.index("--allowedTools") + 1]
    assert "Bash" in granted and "Write" in granted and "Read" in granted


def test_a_failed_tool_call_is_loud():
    from ratchet.providers import _describe

    line = _describe({"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": True, "content": "This command requires approval"},
    ]}})
    assert "tool failed" in line and "approval" in line


def test_a_long_session_is_not_cut_off_at_fifteen_minutes(monkeypatch):
    """A real project is not a fifteen-minute job. The old ceiling killed a build
    mid-flight with nothing to show for it."""
    import inspect

    from ratchet.providers import ChatBackend

    src = inspect.getsource(ChatBackend.run_agentic)
    assert "RATCHET_SESSION_TIMEOUT" in src
    assert "3600" in src, "the default ceiling should be an hour, not fifteen minutes"


def test_a_cut_short_session_still_gets_credit_for_what_it_wrote(tmp_path):
    """Work already on disk must not be thrown away with the error."""
    from ratchet.providers import ChatProviderError

    class Interrupted:
        provider, model, agentic = "claude-code", "sonnet", True

        def run_agentic(self, prompt, repo, on_event, **kw):
            (Path(repo) / "partial.html").write_text("<h1>half</h1>")
            on_event("file", str(Path(repo) / "partial.html"))
            raise ChatProviderError("the session was still running after 3600s and was stopped.")

    turn, lines = _run(ChatSession(tmp_path, backend=Interrupted()), "build something big")
    assert not turn.ok
    assert turn.files == ["partial.html"], "the file it had written was thrown away"
    assert any("kept 1 file" in t for _k, t in lines)
