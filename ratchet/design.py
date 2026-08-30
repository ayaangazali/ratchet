"""Ratchet's design tokens: the palette, the dolphin, and the helpers that render
them.

Not a terminal concern any more. The stream console, the web dashboard and the
generated SVG all read their colours from this one module, which is the only
reason the browser and the terminal cannot drift apart.
"""

from __future__ import annotations

import time

from rich.style import Style
from rich.text import Text

from .sprites import DOLPHIN, DOLPHIN_SMALL, DOLPHIN_TINY, PALETTE, Sprite

# --------------------------------------------------------------------------- #
# palette
# --------------------------------------------------------------------------- #

#: Cool near-black rather than pure black: the mascot is a blue dolphin, and blue
#: on a pure black ground loses its shadows. Keep these in step with `theme.tcss`.
BG = "#0d1219"
PANEL = "#141c26"
PANEL_WARM = "#1a2532"  # the approval card: a shade lighter, so the gate reads as raised
BORDER = "#26323f"

TEXT = "#dce8f5"
MUTED = "#93a7bd"
DIM = "#5f7186"

ACCENT = "#4d9fe8"  # the one bright colour; it means "this is where to look"
ACCENT_DIM = "#2a5f8f"

GREEN = "#5cbf8a"
RED = "#e5675c"
AMBER = "#e0a44a"
BLUE = "#7fb8e6"
VIOLET = "#a795d6"

#: Exported to the dashboard so the browser and the terminal cannot drift apart.
COLOURS: dict[str, str] = {
    "bg": BG,
    "panel": PANEL,
    "panelWarm": PANEL_WARM,
    "border": BORDER,
    "text": TEXT,
    "muted": MUTED,
    "dim": DIM,
    "accent": ACCENT,
    "accentDim": ACCENT_DIM,
    "green": GREEN,
    "red": RED,
    "amber": AMBER,
    "blue": BLUE,
    "violet": VIOLET,
}

# --------------------------------------------------------------------------- #
# sprites
# --------------------------------------------------------------------------- #

#: Short aliases, because these are referenced from layout code where the line is
#: already long.
FIN = DOLPHIN  # 44x22 -- the whole animal, for the splash and the dashboard
FIN_SMALL = DOLPHIN_SMALL  # 30x14 -- the header box
FIN_TINY = DOLPHIN_TINY  # 22x10 -- the header on a narrow terminal

UPPER, LOWER = "▀", "▄"

#: The idle animation. Six frames, deliberately quiet: a spinner in a monitoring UI
#: is ambient, and anything busier reads as an error state.
SPINNER = ("·", "✢", "✳", "∗", "✻", "✽")

#: What the status line says while the search is moving. They rotate slowly. A run
#: that takes ten minutes should not spend ten minutes saying the same word.
VERBS = (
    "Ratcheting",
    "Sounding",
    "Cruising",
    "Porpoising",
    "Echolocating",
    "Surfacing",
    "Gliding",
    "Diving",
    "Breaching",
    "Circling",
)

__all__ = [
    "ACCENT", "ACCENT_DIM", "AMBER", "BG", "BLUE", "BORDER", "FIN", "FIN_SMALL",
    "FIN_TINY", "COLOURS", "DIM", "GREEN", "MUTED", "PALETTE", "PANEL", "PANEL_WARM", "RED",
    "SPINNER", "Sprite", "TEXT", "VERBS", "VIOLET", "beside", "elapsed", "render",
    "duration", "render_lines", "spinner_glyph", "to_svg", "verb",
]


def spinner_glyph(tick: int) -> str:
    return SPINNER[tick % len(SPINNER)]


def verb(seed: int) -> str:
    return VERBS[seed % len(VERBS)]


def elapsed(since: float) -> str:
    return duration(time.time() - since)


def duration(seconds: float) -> str:
    """A span, formatted. Separate from `elapsed` because the status clock counts
    accumulated work time, which is not "time since a timestamp"."""
    s = max(0, int(seconds))
    return f"{s}s" if s < 60 else f"{s // 60}m{s % 60:02d}s"


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def _blend(hex_colour: str, other: str, amount: float) -> str:
    """Mix `hex_colour` toward `other`. Used to fade the splash mascot back so she
    reads as decoration rather than as data."""
    a = [int(hex_colour[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(other[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(a, b, strict=True))


def _colour(ch: str, dim: float) -> str | None:
    hexv = PALETTE.get(ch)
    if hexv is None:
        return None
    return hexv if dim >= 1.0 else _blend(hexv, PANEL, 1.0 - dim)


def render_lines(sprite: Sprite = FIN, *, dim: float = 1.0) -> list[Text]:
    """One `Text` per terminal row: two sprite rows to a line."""
    out: list[Text] = []
    for y in range(0, sprite.height, 2):
        top, bottom = sprite.rows[y], sprite.rows[y + 1]
        line = Text()
        for x in range(sprite.width):
            t, b = _colour(top[x], dim), _colour(bottom[x], dim)
            if t and b:
                line.append(UPPER, Style(color=t, bgcolor=b))
            elif t:
                line.append(UPPER, Style(color=t))
            elif b:
                line.append(LOWER, Style(color=b))
            else:
                line.append(" ")
        out.append(line)
    return out


def render(sprite: Sprite = FIN, *, indent: int = 0, dim: float = 1.0) -> Text:
    pad = " " * indent
    body = Text()
    for i, line in enumerate(render_lines(sprite, dim=dim)):
        if i:
            body.append("\n")
        body.append(pad)
        body.append_text(line)
    return body


def beside(sprite: Sprite, lines: list[Text], *, gap: int = 3, indent: int = 1,
           dim: float = 1.0) -> Text:
    """Mascot on the left, a block of text on the right, vertically centred against
    her. Rich has no column layout that survives half-block backgrounds intact, so
    the columns are laid out by hand."""
    art = render_lines(sprite, dim=dim)
    height = max(len(art), len(lines))
    top_pad = max(0, (height - len(lines)) // 2)
    padded: list[Text | None] = [None] * top_pad + list(lines)

    out = Text()
    for i in range(height):
        if i:
            out.append("\n")
        out.append(" " * indent)
        out.append_text(art[i] if i < len(art) else Text(" " * sprite.width))
        out.append(" " * gap)
        if i < len(padded) and padded[i] is not None:
            out.append_text(padded[i])  # type: ignore[arg-type]
    return out


def to_svg(sprite: Sprite = FIN, *, scale: int = 6) -> str:
    """The same sprite as SVG, one rect per run of same-coloured pixels.

    Runs rather than pixels because a 44x22 grid is 968 rects unmerged and the
    dashboard inlines this into the page."""
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {sprite.width} {sprite.height}" '
        f'width="{sprite.width * scale}" height="{sprite.height * scale}" '
        f'shape-rendering="crispEdges" role="img" aria-label="a pixel-art dolphin">'
    ]
    for y, row in enumerate(sprite.rows):
        x = 0
        while x < sprite.width:
            colour = PALETTE.get(row[x])
            if colour is None:
                x += 1
                continue
            run = 1
            while x + run < sprite.width and row[x + run] == row[x]:
                run += 1
            parts.append(f'<rect x="{x}" y="{y}" width="{run}" height="1" fill="{colour}"/>')
            x += run
    parts.append("</svg>")
    return "".join(parts)
