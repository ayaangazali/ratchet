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
    assert "kimi/" in text2                    # provider switch, live
