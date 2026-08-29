"""The chat coder: providers, the turn loop, and the console wiring.

Everything offline: the demo provider needs no key and no network, and the wire
providers are tested by capturing the request they would have sent.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

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


def test_no_keys_means_the_offline_demo_provider(monkeypatch):
    for _base, key_env, _model in PROVIDERS.values():
        if key_env:
            monkeypatch.delenv(key_env, raising=False)
    monkeypatch.delenv("RATCHET_CHAT_PROVIDER", raising=False)
    monkeypatch.delenv("RATCHET_CHAT_MODEL", raising=False)
    b = ChatBackend.from_env()
    assert b.provider == "demo"
    assert FILE_FENCE.search(b.complete("make a website for my dog"))


def test_model_switch_and_unknown_provider():
    b = ChatBackend(provider="demo", model="demo")
    assert b.switch("groq") == "groq/" + PROVIDERS["groq"][2]
    assert b.switch("kimi/kimi-k2-0905-preview").endswith("kimi-k2-0905-preview")
    with pytest.raises(ChatProviderError, match="unknown provider"):
        b.switch("grogg")


def test_openai_compat_and_anthropic_request_shapes(monkeypatch):
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
    assert len(models) == 15 and all(r.kind == "model" for r in models)  # +trueforge, +truefoundry
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
