#!/usr/bin/env python3
"""Draw Ratchet's dolphin and write `ratchet/tui/sprites.py`.

Pixel art typed into a source file drifts: one row loses a character, the sprite
shears, and you find out in a screenshot at the judging table. So the art is
described here as geometry -- two profile curves for the body, triangles for the
fins -- and the grid is filled from that. Every row is the declared width because
the grid allocates it that way, and the same description renders at three sizes.

    python scripts/make_mascot.py             # preview, with silhouettes
    python scripts/make_mascot.py --write     # regenerate ratchet/tui/sprites.py

The silhouette column in the preview is the thing to actually look at. If the
shape does not say "dolphin" in solid black, no amount of shading will save it.
"""

from __future__ import annotations

import sys
from pathlib import Path

TRANSPARENT = "."
SOLID = ("L", "M", "D", "P", "E", "K", "W")

#: Grid character -> hex colour. Counter-shading is the whole read on a dolphin:
#: dark slate back, mid flank, pale belly. Flat colour looks like a fish.
PALETTE = {
    "L": "#7fb8e6",  # lit flank
    "M": "#4d8fc9",  # mid body
    "D": "#2d5f94",  # dark back and fin leading edges
    "S": "#16324f",  # outline
    "P": "#d5e8f8",  # pale belly
    "E": "#3a6ea8",  # fins
    "K": "#0b1622",  # eye
    "W": "#f2f9ff",  # glint
}

# --------------------------------------------------------------------------- #
# geometry, in normalised 0..1 space so one description drives every size
# --------------------------------------------------------------------------- #

#: Back and belly profiles. Smoothstepped between control points, so the silhouette
#: has no visible kinks even at 22 pixels wide.
TOP = [(0.10, 0.505), (0.18, 0.465), (0.28, 0.395), (0.38, 0.335), (0.50, 0.305),
       (0.62, 0.305), (0.72, 0.345), (0.80, 0.410), (0.86, 0.465), (0.90, 0.492)]
BOT = [(0.10, 0.565), (0.18, 0.615), (0.28, 0.695), (0.40, 0.745), (0.52, 0.755),
       (0.64, 0.735), (0.74, 0.685), (0.82, 0.615), (0.87, 0.565), (0.90, 0.545)]

#: The rostrum is a stubby rectangle rather than a taper. A tapered beak is one
#: pixel tall at these sizes, which is to say invisible.
BEAK = (0.862, 0.470, 0.995, 0.560)

DORSAL = ((0.58, 0.310), (0.44, 0.305), (0.360, 0.055))
PECTORAL = ((0.710, 0.700), (0.620, 0.748), (0.520, 0.950))

#: Each fluke lobe is a quad -- two triangles -- because a single triangle comes
#: out as a sliver and reads as damage rather than as a tail.
FLUKE_UPPER = (((0.145, 0.475), (0.030, 0.215), (0.005, 0.340)),
               ((0.145, 0.475), (0.005, 0.340), (0.100, 0.545)))
FLUKE_LOWER = (((0.145, 0.595), (0.030, 0.855), (0.005, 0.730)),
               ((0.145, 0.595), (0.005, 0.730), (0.100, 0.545)))

EYE = (0.800, 0.440)
BLOWHOLE = (0.680, 0.318)
MOUTH = ((0.855, 0.537), (0.985, 0.515))  # rises toward the nose: the smile


class Grid:
    def __init__(self, w: int, h: int) -> None:
        self.w, self.h = w, h
        self.px = [[TRANSPARENT] * w for _ in range(h)]

    def set(self, x: int, y: int, c: str) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            self.px[y][x] = c

    def get(self, x: int, y: int) -> str:
        return self.px[y][x] if 0 <= x < self.w and 0 <= y < self.h else TRANSPARENT

    def tri(self, p0, p1, p2, c: str) -> None:
        xs = [p[0] for p in (p0, p1, p2)]
        ys = [p[1] for p in (p0, p1, p2)]
        det = (p1[1] - p2[1]) * (p0[0] - p2[0]) + (p2[0] - p1[0]) * (p0[1] - p2[1])
        if abs(det) < 1e-9:
            return
        for y in range(max(0, int(min(ys))), min(self.h, int(max(ys)) + 2)):
            for x in range(max(0, int(min(xs))), min(self.w, int(max(xs)) + 2)):
                px, py = x + 0.5, y + 0.5
                a = ((p1[1] - p2[1]) * (px - p2[0]) + (p2[0] - p1[0]) * (py - p2[1])) / det
                b = ((p2[1] - p0[1]) * (px - p2[0]) + (p0[0] - p2[0]) * (py - p2[1])) / det
                if a >= -0.02 and b >= -0.02 and a + b <= 1.02:
                    self.set(x, y, c)

    def outline(self, c: str) -> None:
        adds = [
            (x, y)
            for y in range(self.h)
            for x in range(self.w)
            if self.get(x, y) == TRANSPARENT
            and any(self.get(x + dx, y + dy) in SOLID for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
        ]
        for x, y in adds:
            self.set(x, y, c)

    def rows(self) -> list[str]:
        return ["".join(r) for r in self.px]


def _profile(points, u: float) -> float | None:
    """Smoothstepped interpolation along a profile; None outside its span."""
    if u <= points[0][0] or u >= points[-1][0]:
        return None
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        if x0 <= u <= x1:
            t = 0.0 if x1 == x0 else (u - x0) / (x1 - x0)
            return y0 + (y1 - y0) * t * t * (3 - 2 * t)
    return None


def build(w: int, h: int) -> Grid:
    g = Grid(w, h)
    sc = lambda p: (p[0] * w, p[1] * h)  # noqa: E731

    # Fins first, so the body paints over their roots and they look attached.
    for quad in (FLUKE_UPPER, FLUKE_LOWER):
        for tri in quad:
            g.tri(*[sc(p) for p in tri], "E")
    g.tri(*[sc(p) for p in DORSAL], "E")
    g.tri(*[sc(p) for p in PECTORAL], "E")

    x0, y0, x1, y1 = BEAK
    for y in range(int(y0 * h), int(y1 * h) + 1):
        for x in range(int(x0 * w), int(x1 * w)):
            g.set(x, y, "M")

    for x in range(w):
        u = (x + 0.5) / w
        top, bottom = _profile(TOP, u), _profile(BOT, u)
        if top is None or bottom is None or bottom <= top:
            continue
        top, bottom = top * h, bottom * h
        for y in range(h):
            yc = y + 0.5
            if not top <= yc <= bottom:
                continue
            k = (yc - top) / max(1e-6, bottom - top)
            g.set(x, y, "D" if k < 0.22 else "M" if k < 0.50 else "L" if k < 0.72 else "P")

    # A darker leading edge keeps the fins from reading as flat cut-outs.
    for x in range(w):
        for y in range(h):
            if g.get(x, y) == "E" and g.get(x, y - 1) == TRANSPARENT:
                g.set(x, y, "D")

    ex, ey = int(EYE[0] * w), int(EYE[1] * h)
    for dx in (0, 1):
        for dy in (0, 1):
            g.set(ex + dx, ey + dy, "K")
    g.set(ex, ey, "W")
    g.set(int(BLOWHOLE[0] * w), int(BLOWHOLE[1] * h), "D")

    (mx0, my0), (mx1, my1) = sc(MOUTH[0]), sc(MOUTH[1])
    for x in range(int(mx0), int(mx1) + 1):
        t = (x - mx0) / max(1e-6, mx1 - mx0)
        g.set(x, int(my0 + (my1 - my0) * t), "D")

    g.outline("S")
    return g


#: name, width, height. Heights are even: two pixels per terminal row.
SIZES = (
    ("DOLPHIN", 44, 22),        # the splash and the dashboard hero
    ("DOLPHIN_SMALL", 30, 14),  # the header box
    ("DOLPHIN_TINY", 22, 10),   # the header on a narrow terminal
)

HEADER = '''"""Generated by `scripts/make_mascot.py`. Do not hand-edit -- edit the geometry there.

A sprite is a grid of palette characters, one per pixel, with "." for transparent.
`ratchet.tui.mascot` turns it into half-block text: two stacked pixels per terminal
cell, which is what makes the pixels come out square rather than stretched.
"""

from __future__ import annotations

from typing import NamedTuple


class Sprite(NamedTuple):
    width: int
    height: int
    rows: tuple[str, ...]


#: Grid character -> hex colour.
PALETTE: dict[str, str] = {
'''


def emit() -> str:
    out = [HEADER]
    for ch, hexv in PALETTE.items():
        out.append(f'    "{ch}": "{hexv}",\n')
    out.append("}\n\n")
    for name, w, h in SIZES:
        g = build(w, h)
        out.append(f"\n{name} = Sprite(\n    width={w},\n    height={h},\n    rows=(\n")
        for row in g.rows():
            out.append(f'        "{row}",\n')
        out.append("    ),\n)\n")
    return "".join(out)


def preview() -> None:
    for name, w, h in SIZES:
        g = build(w, h)
        print(f"\n{name}  ({w}x{h} px -> {w} cols x {h // 2} rows)")
        for y, row in enumerate(g.rows()):
            sil = "".join("#" if c != TRANSPARENT else " " for c in row)
            print(f"  {y:2} |{row}|  |{sil}|")


if __name__ == "__main__":
    if "--write" in sys.argv:
        target = Path(__file__).resolve().parents[1] / "ratchet" / "tui" / "sprites.py"
        target.write_text(emit())
        print(f"wrote {target}")
    elif "--emit" in sys.argv:
        # print exactly what --write would write, so the test can diff the shipped
        # file against the generator without touching the tree
        sys.stdout.write(emit())
    else:
        preview()
