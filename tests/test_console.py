"""The console's invariants.

Generated art and injected templates are exactly the things that break quietly: a
sprite row loses a character and the dolphin shears, a colour is edited in the
stylesheet but not in the module and the browser slowly stops matching the terminal.
None of this needs a model, a key, a network or a terminal.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from ratchet.dashboard.server import SAFE_ID, _page
from ratchet.tui import mascot as m
from ratchet.tui.sprites import PALETTE, Sprite

ROOT = Path(__file__).resolve().parents[1]
SPRITES = [m.FIN, m.FIN_SMALL, m.FIN_TINY]


@pytest.mark.parametrize("sprite", SPRITES, ids=lambda s: f"{s.width}x{s.height}")
def test_sprite_grid_is_rectangular(sprite: Sprite) -> None:
    assert len(sprite.rows) == sprite.height
    assert [len(r) for r in sprite.rows] == [sprite.width] * sprite.height


@pytest.mark.parametrize("sprite", SPRITES, ids=lambda s: f"{s.width}x{s.height}")
def test_sprite_height_is_even(sprite: Sprite) -> None:
    """Half-block rendering puts two pixel rows in one cell; an odd height would
    silently drop the last row."""
    assert sprite.height % 2 == 0


@pytest.mark.parametrize("sprite", SPRITES, ids=lambda s: f"{s.width}x{s.height}")
def test_every_pixel_has_a_colour(sprite: Sprite) -> None:
    used = {c for row in sprite.rows for c in row} - {"."}
    assert used <= set(PALETTE), f"undefined palette characters: {sorted(used - set(PALETTE))}"


@pytest.mark.parametrize("sprite", SPRITES, ids=lambda s: f"{s.width}x{s.height}")
def test_render_fills_exactly_the_declared_box(sprite: Sprite) -> None:
    lines = m.render_lines(sprite)
    assert len(lines) == sprite.height // 2
    assert {len(line.plain) for line in lines} == {sprite.width}


def test_sprites_match_their_geometry() -> None:
    """`sprites.py` is generated. If somebody hand-edits the pixels, the file and
    the script that claims to produce it have parted company -- and the next person
    to run `make mascot` silently reverts their work."""
    generated = subprocess.run(
        [sys.executable, "scripts/make_mascot.py", "--emit"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    assert generated == (ROOT / "ratchet" / "tui" / "sprites.py").read_text()


def test_svg_uses_only_palette_colours() -> None:
    svg = m.to_svg(m.FIN)
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    fills = set(re.findall(r'fill="([^"]+)"', svg))
    assert fills and fills <= set(PALETTE.values())


def test_stylesheet_and_module_agree_on_colours() -> None:
    """The stylesheet cannot import Python, so a handful of hex codes are written
    twice. Every one of them still has to name a colour the module defines."""
    css = (ROOT / "ratchet" / "tui" / "theme.tcss").read_text()
    known = {v.lower() for v in m.COLOURS.values()}
    used = {c.lower() for c in re.findall(r"#[0-9a-fA-F]{6}", css)}
    assert used <= known, f"colours in theme.tcss that mascot.py does not define: {sorted(used - known)}"


def test_dashboard_page_leaves_no_placeholder() -> None:
    page = _page(Path(".ratchet/demo.bus.jsonl"), Path("demo-repo")).decode()
    assert "__PALETTE__" not in page
    assert "__DOLPHIN__" not in page
    assert "__RUN__" not in page and "__REPO__" not in page
    for name, value in m.COLOURS.items():
        assert f"--{name}: {value};" in page, f"palette entry {name} never reached the page"
    assert "<svg" in page


def test_bare_ratchet_opens_the_console(monkeypatch, tmp_path):
    """`ratchet` with no arguments starts the TUI, the way `claude` starts a
    session -- never a usage error."""
    from ratchet import cli

    calls = {}
    monkeypatch.setattr(cli, "cmd_console", lambda args: (calls.setdefault("args", args), 0)[1])
    assert cli.main([]) == 0
    assert calls["args"].repo is None and calls["args"].bus is None


def test_console_with_no_run_opens_on_an_empty_bus(monkeypatch, tmp_path):
    """No run yet is not an error: the console opens on a fresh bus and the idle
    splash says what to do next."""
    from argparse import Namespace

    from ratchet import cli

    opened = {}

    class FakeApp:
        def __init__(self, bus_path, repo):
            opened["bus"] = Path(bus_path)

        def run(self):
            pass

    import ratchet.tui.app as app_mod

    monkeypatch.setattr(app_mod, "RatchetApp", FakeApp)
    monkeypatch.chdir(tmp_path)
    rc = cli.cmd_console(Namespace(repo=str(tmp_path), bus=None, run=None))
    assert rc == 0
    assert opened["bus"].exists() and opened["bus"].stat().st_size == 0


@pytest.mark.parametrize("bad", ["../../etc/passwd", "a1b2/../x", "", "A1B2", "a" * 33, "a1b2.json"])
def test_approval_id_guard_rejects_anything_that_is_not_an_id(bad: str) -> None:
    """The id becomes a filename, so it does not get the benefit of the doubt."""
    assert not SAFE_ID.match(bad)


def test_approval_id_guard_accepts_a_real_one() -> None:
    assert SAFE_ID.match("a1b2c3d4")


def test_no_pane_disappears_on_an_ordinary_terminal(tmp_path):
    """The console was reported dead: "search tree doesn't work, the gauntlet
    doesn't work, waiting-on doesn't work". None of them were broken -- the layout
    hid the entire right column below 104 columns and the tree below 76, silently,
    on what is a perfectly ordinary 80-column window. Panes may stack. They may not
    vanish."""
    import asyncio
    import os

    from ratchet.tui.app import RatchetApp

    (tmp_path / ".ratchet").mkdir()
    bus = tmp_path / ".ratchet" / "s.bus.jsonl"
    bus.touch()
    os.chdir(tmp_path)

    async def widths():
        out = {}
        for w in (60, 80, 100, 120, 150):
            app = RatchetApp(bus, tmp_path)
            async with app.run_test(size=(w, 40)) as pilot:
                await pilot.pause(0.3)
                out[w] = (
                    app.query_one("#left").display,
                    app.query_one("#activity-box").display,
                    app.query_one("#right").display,
                    app.query_one("#main").has_class("stacked"),
                )
        return out

    for width, (left, activity, right, stacked) in asyncio.run(widths()).items():
        assert activity, f"the activity pane vanished at {width} columns"
        assert right, f"the gauntlet and waiting-on vanished at {width} columns"
        assert left, f"the search tree vanished at {width} columns"
        assert stacked == (width < 104), f"{width} columns should {'stack' if width < 104 else 'not stack'}"
