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


def test_typing_in_the_console_codes_in_the_background(repo, monkeypatch):
    """The whole feature, end to end and headless: type a prompt into the chat box,
    the turn runs on a worker, the activity pane gets the ultra-summary (never raw
    code), the file lands on disk as one commit, and /model switches providers."""
    import asyncio

    from textual.widgets import Input, RichLog

    from ratchet.tui.app import RatchetApp

    monkeypatch.setenv("RATCHET_CHAT_PROVIDER", "demo")
    (repo / ".ratchet").mkdir()
    bus = repo / ".ratchet" / "session.bus.jsonl"
    bus.touch()

    async def drive():
        app = RatchetApp(bus, repo)
        async with app.run_test(size=(150, 46)) as pilot:
            await pilot.pause(0.4)
            box = app.query_one("#chat", Input)
            box.focus()
            box.value = "make a website for my coffee shop"
            await pilot.press("enter")
            text = ""
            for _ in range(60):
                await pilot.pause(0.25)
                text = "\n".join(str(line.text) for line in app.query_one("#activity", RichLog).lines)
                if "done in" in text:
                    break
            box.focus()
            box.value = "/model kimi"
            await pilot.press("enter")
            await pilot.pause(0.3)
            text2 = "\n".join(str(line.text) for line in app.query_one("#activity", RichLog).lines)
            return text, text2

    text, text2 = asyncio.run(drive())
    assert "wrote index.html" in text          # the summary
    assert "<!doctype" not in text             # never the raw code
    assert "done in" in text and "commit" in text
    assert (repo / "index.html").exists()
    # the palette applied its highlighted row: /model kimi filtered the catalog and
    # Enter picked the top match -- a live provider/model switch from the dropdown
    assert "chat model ->" in text2 and "kimi" in text2


# ------------------------------------------------------------------ palette --


def test_palette_autocompletes_commands_and_models():
    from ratchet.tui.palette import COMMANDS, help_lines, rows_for

    assert [r.label for r in rows_for("/mo")] == ["/model"]
    assert len(rows_for("/")) == len(COMMANDS)
    models = rows_for("/model ")
    assert len(models) == 19 and all(r.kind == "model" for r in models)  # incl. claude-code, trueforge, truefoundry
    kimi = [r.label for r in rows_for("/model kimi")]
    assert kimi and all("kimi" in label for label in kimi)
    providers = [r.label for r in rows_for("/connect")]
    assert "groq" in providers and "demo" not in providers
    assert rows_for("just some prose") == []
    # /help IS the command dict: every command self-documents
    joined = "\n".join(help_lines())
    assert all(cmd in joined for cmd in COMMANDS)


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


def test_first_run_lands_in_the_connect_picker(repo, monkeypatch):
    import asyncio

    from textual.widgets import Input as _Input

    import ratchet.providers as prov
    from ratchet.tui.app import RatchetApp

    # a machine with nothing connected at all
    monkeypatch.setattr(prov, "connected_providers",
                        lambda: {k: (k == "demo") for k in prov.PROVIDERS})
    monkeypatch.setenv("RATCHET_CHAT_PROVIDER", "demo")
    (repo / ".ratchet").mkdir(exist_ok=True)
    bus = repo / ".ratchet" / "session.bus.jsonl"
    bus.touch()

    async def drive():
        app = RatchetApp(bus, repo)
        async with app.run_test(size=(150, 46)) as pilot:
            await pilot.pause(0.6)
            return app.query_one("#chat", _Input).value

    assert asyncio.run(drive()) == "/connect "


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


def test_restart_unsticks_a_hung_turn_and_resets_the_session(repo, monkeypatch):
    """/restart is the escape hatch for exactly the state where a turn is wedged
    and you cannot tell why: the worker is cancelled and the session rebuilt."""
    import asyncio
    import threading

    from textual.widgets import Input as _Input
    from textual.widgets import RichLog as _RichLog

    monkeypatch.setenv("RATCHET_CHAT_PROVIDER", "demo")
    (repo / ".ratchet").mkdir(exist_ok=True)
    bus = repo / ".ratchet" / "session.bus.jsonl"
    bus.touch()
    release = threading.Event()

    class Hanging:
        provider, model = "hang", "hang"

        def complete(self, prompt, **kw):
            release.wait(timeout=30)  # a model that never answers
            return "intent: never\n```file:x.txt\nx\n```"

    from ratchet.tui.app import RatchetApp

    async def drive():
        app = RatchetApp(bus, repo)
        async with app.run_test(size=(150, 46)) as pilot:
            await pilot.pause(0.4)
            app._chat_session().backend = Hanging()
            box = app.query_one("#chat", _Input)
            box.focus()
            box.value = "build something"
            await pilot.press("enter")
            await pilot.pause(0.8)
            assert app._chat_worker is not None and app._chat_worker.is_running
            box.focus()
            box.value = "/restart"
            await pilot.pause(0.2)
            app.query_one("#palette").display = False
            await pilot.press("enter")
            await pilot.pause(0.8)
            text = "\n".join(str(line.text) for line in app.query_one("#activity", _RichLog).lines)
            return app._chat_worker, app._chat, text

    worker, session, text = asyncio.run(drive())
    release.set()
    assert worker is None and session is not None   # cancelled, then rebuilt
    assert "restarted" in text


def test_debug_command_toggles_the_panel(repo, monkeypatch):
    import asyncio

    from textual.widgets import Input as _Input
    from textual.widgets import RichLog as _RichLog

    monkeypatch.setenv("RATCHET_CHAT_PROVIDER", "demo")
    (repo / ".ratchet").mkdir(exist_ok=True)
    bus = repo / ".ratchet" / "session.bus.jsonl"
    bus.touch()

    from ratchet.tui.app import RatchetApp

    async def drive():
        app = RatchetApp(bus, repo)
        async with app.run_test(size=(150, 46)) as pilot:
            await pilot.pause(0.4)
            panel = app.query_one("#debug", _RichLog)
            before = panel.display
            box = app.query_one("#chat", _Input)
            box.focus()
            box.value = "/debug"
            await pilot.pause(0.2)
            app.query_one("#palette").display = False
            await pilot.press("enter")
            await pilot.pause(0.4)
            return before, panel.display, len(panel.lines)

    before, after, n_lines = asyncio.run(drive())
    assert after is not before
    assert n_lines > 0  # the panel replays what already happened


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


def test_the_clock_counts_work_not_session_age():
    """The reported bug: a console open for an hour claimed an hour of work on a
    one-second turn. The clock must only run while something is running."""
    import time as _t

    from ratchet.tui.app import StatusLine

    s = StatusLine()
    s.started = _t.time() - 3600          # console has been open an hour
    assert s.work_seconds == 0.0          # ...but nothing has been done
    assert s._clock() == "0s"

    s.begin_work()
    _t.sleep(0.05)
    s.end_work()
    banked = s.work_seconds
    assert 0.0 < banked < 5.0             # a fraction of a second, not an hour

    _t.sleep(0.05)
    assert s.work_seconds == banked       # idle time does not accrue


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


def test_an_agentic_turn_commits_only_what_it_touched(repo):
    """A turn's commit is the turn's work and nothing else -- decided by content.

    Two failures meet here. Every dirty path used to count as the session's, so an
    edit the user already had in flight was staged, committed under the turn's
    intent and -- in `qodo-fix` -- pushed at the gate as a Qodo remediation nobody
    wrote. Filtering those out by modification time then lost the opposite case: an
    edit that keeps the old timestamp (an editor restoring stat, as `theirs.py` does
    below) is a real change with an unmoved clock, and it fell out of the turn.
    """
    import os

    (repo / "mine.py").write_text("user was here\n")
    (repo / "theirs.py").write_text("old\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed2"],
                   cwd=repo, check=True)
    (repo / "mine.py").write_text("user was here, twice\n")   # tracked and dirty before the turn
    stat_before = (repo / "theirs.py").stat()

    class Mixed:
        provider, model, agentic = "claude-code", "sonnet", True

        def run_agentic(self, prompt, r, on_event, **kw):
            (Path(r) / "theirs.py").write_text("new\n")   # edits, timestamp and all
            os.utime(Path(r) / "theirs.py", (stat_before.st_atime, stat_before.st_mtime))
            (Path(r) / "added.py").write_text("added\n")  # and creates
            return "done"

    turn, _ = _run(ChatSession(repo, backend=Mixed()), "fix it")
    assert turn.ok, turn.error
    assert turn.files == ["added.py", "theirs.py"]
    committed = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                               cwd=repo, capture_output=True, text=True).stdout.split()
    assert committed == ["added.py", "theirs.py"]
    assert "mine.py" in subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                                       capture_output=True, text=True).stdout


def test_a_chat_turn_drives_every_pane(repo, monkeypatch):
    """The complaint, as a test: the tree, the gauntlet rail, the counters and the
    waiting-on panel all sat dead through a whole session, because they are fed by
    run events and a chat turn only ever emitted chat.*, which the renderer skips.
    A turn is a node -- it has an intent, it is graded, it produces files -- so it
    has to say so in the language the console reads."""
    import asyncio

    from textual.widgets import Input as _Input
    from textual.widgets import RichLog as _RichLog

    from ratchet.tui.app import Counters, GauntletRail, RatchetApp, TreePane

    monkeypatch.setenv("RATCHET_CHAT_PROVIDER", "demo")
    (repo / ".ratchet").mkdir(exist_ok=True)
    bus = repo / ".ratchet" / "session.bus.jsonl"
    bus.touch()

    async def drive():
        app = RatchetApp(bus, repo)
        async with app.run_test(size=(150, 46)) as pilot:
            await pilot.pause(0.4)
            box = app.query_one("#chat", _Input)
            box.focus()
            box.value = "make a page"
            await pilot.press("enter")
            for _ in range(80):
                await pilot.pause(0.25)
                text = "\n".join(str(line.text) for line in app.query_one("#activity", _RichLog).lines)
                if "done in" in text or "error" in text.lower():
                    break
            await pilot.pause(0.8)
            return (
                dict(app.query_one(TreePane).nodes),
                {k: v[0] for k, v in app.query_one(GauntletRail).state.items()},
                app.query_one(Counters).subagents,
                text,
            )

    nodes, rail, subagents, text = asyncio.run(drive())

    assert nodes, "the tree stayed empty through a whole turn"
    node = next(iter(nodes.values()))
    assert node.get("green") and node.get("intent")
    assert rail["cheat"] == "pass", "the gauntlet rail never showed the stage that ran"
    # honest about the stages a chat turn does not run, rather than blank
    assert rail["f2p"] == "skip" and rail["build"] == "skip"
    assert subagents >= 1, "the counters never noticed the session"
    # and the session's own narration survives the bus events that follow it
    assert "wrote index.html" in text
