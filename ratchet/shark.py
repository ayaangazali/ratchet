"""The shark, and the animation that runs while work is happening.

A CLI that sits silent for thirty seconds feels broken even when it is not --
that lesson cost a whole evening. So every stage that takes time animates: the
shark swims, the spinner turns, the elapsed clock runs, and when the stage lands
the animation is replaced by one permanent line. Nothing is painted into a fixed
box, so nothing can be hidden by a narrow terminal.

Blue throughout, on purpose: one accent colour, used to mean "look here".
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from rich.console import Group, RenderableType
from rich.text import Text

# --------------------------------------------------------------------------- #
# the palette: one blue, three weights, and the three verdict colours
# --------------------------------------------------------------------------- #

DEEP = "#0b1a2b"
FIN = "#1f4e79"
BODY = "#2a6fa8"
BRIGHT = "#4d9fe8"
GLINT = "#8fd0ff"
TEXT = "#dce8f5"
MUTED = "#93a7bd"
DIM = "#5f7186"
GREEN = "#5cbf8a"
RED = "#e5675c"
AMBER = "#e0a44a"

#: the shark, in half-block rows. Drawn rather than generated: a hand-placed
#: silhouette reads at 12 rows where a downsampled photo does not.
SHARK = [
    "                    ▗▟▙                          ",
    "                   ▟███▖                    ▗▄▖  ",
    "      ▁▁▁▁▁▁▁▁▁▁▁▁▟█████▙▁▁▁▁▁▁▁▁▁▁▁▁▁▁▖   ▗███  ",
    "   ▄▟█████████████████████████████████████▙▄████ ",
    " ▄███████████████████████████████████████████████",
    "▟████████████████████████████████████████████████",
    "▜████████████████████████████████████████████████",
    " ▀███████████████████████████████████████████████",
    "   ▀▜████████▛▀▀▘   ▀▀▜████████████████▛▀▘ ▝████ ",
    "        ▜███▙▖          ▀▀▀▀▀▀▀▀▀▀▀▀▀       ▝▀▀  ",
    "          ▀▀▀                                    ",
]

#: where the eye and the gill slits sit, so they can be lit separately
EYE = (6, 5)
GILLS = ((11, 5), (13, 5), (15, 5))

SPINNER = ("◐", "◓", "◑", "◒")
WAVE = ("∼", "≈", "∽", "≈")

#: what the harness is doing, phrased as something a person would say
VERBS = (
    "swimming the dependency graph", "circling the failure", "reading the tree",
    "hunting the regression", "tasting the diff", "shadowing the test suite",
    "diving the call stack", "cruising the changelog",
)


def shark_lines(*, dim: float = 1.0, phase: int = 0) -> list[Text]:
    """The shark, shaded front-to-back so it reads as a body rather than a blob.
    `phase` moves the glint along its flank, which is what makes it look alive."""
    out: list[Text] = []
    width = max(len(r) for r in SHARK)
    for y, row in enumerate(SHARK):
        line = Text()
        for x, ch in enumerate(row):
            if ch == " ":
                line.append(" ")
                continue
            depth = x / width                     # nose bright, tail deep
            colour = BRIGHT if depth < 0.22 else BODY if depth < 0.62 else FIN
            if (x, y) == EYE:
                colour = GLINT
            elif (x, y) in GILLS:
                colour = DEEP
            elif abs((x + phase * 3) % width - width * 0.35) < 2 and 3 < y < 9:
                colour = GLINT                    # the moving glint
            line.append(ch, style=_dimmed(colour, dim))
        out.append(line)
    return out


def _dimmed(hex_colour: str, amount: float) -> str:
    if amount >= 0.999:
        return hex_colour
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    bg = (11, 26, 43)
    mr, mg, mb = (int(c * amount + b0 * (1 - amount)) for c, b0 in zip((r, g, b), bg, strict=True))
    return f"#{mr:02x}{mg:02x}{mb:02x}"


def banner(subtitle: str = "") -> Group:
    """The splash: shark, name, and one line saying what the thing believes."""
    lines = shark_lines(dim=0.85)
    title = Text()
    title.append("\n   ratchet", style=f"bold {BRIGHT}")
    title.append("   the agent doesn't decide it's done — the tests do\n", style=DIM)
    if subtitle:
        title.append(f"   {subtitle}\n", style=MUTED)
    return Group(*lines, title)


@dataclass
class Swimmer:
    """One animated line, for one stage that takes time."""

    label: str
    detail: str = ""
    started: float = 0.0
    tick: int = 0

    def __post_init__(self) -> None:
        self.started = self.started or time.time()

    def frame(self) -> RenderableType:
        self.tick += 1
        secs = int(time.time() - self.started)
        t = Text()
        t.append(f"  {SPINNER[self.tick % len(SPINNER)]} ", style=f"bold {BRIGHT}")
        t.append(self.label, style=f"bold {TEXT}")
        if self.detail:
            t.append(f"  {self.detail}", style=MUTED)
        t.append(f"   {secs}s ", style=DIM)
        # a little water trailing behind, so the line is visibly alive
        wake = "".join(WAVE[(self.tick + i) % len(WAVE)] for i in range(6))
        t.append(wake, style=_dimmed(BODY, 0.55))
        return t
