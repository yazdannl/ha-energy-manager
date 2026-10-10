#!/usr/bin/env python3
"""Generate the add-on icon and logo as PNGs, standard library only.

    python3 tools/make_icons.py

Writes (relative to the repository root):
    energy_manager/icon.png   128x128
    energy_manager/logo.png   250x100

Output is deterministic: same script, same bytes — no timestamp or other metadata is embedded, and
nothing is read from the environment. Re-running the script is the intended way to refresh the
assets after a design edit.

ponytail: a small rasteriser (rounded rectangles, discs, 4x supersampling) instead of an image
library, because SPEC.md R3 allows no third-party runtime dependency and these assets change about
once a year. Ceiling: no text, no gradients, no image import. Upgrade path: draw the assets in a
vector tool, export PNGs, commit them and delete this file.
"""

from __future__ import annotations

import os
import struct
import zlib

Rgba = tuple[int, int, int, int]

# Palette (chosen to read on both light and dark Home Assistant themes).
BG = (11, 38, 54, 255)  # deep petrol, icon background
SHELL = (233, 244, 240, 255)  # battery shell
BAR = (103, 196, 106, 255)  # charge bars, green
BAR_DIM = (74, 152, 92, 255)  # last bar, to read as headroom rather than a full pack
SUN = (242, 193, 78, 255)  # small rising sun
BLUE = (31, 111, 139, 255)
TEAL = (46, 158, 126, 255)
LIME = (111, 191, 63, 255)

SUPERSAMPLE = 4  # render 4x and average down for anti-aliasing


class Canvas:
    """Tiny RGBA raster canvas. Coordinates are in final-image pixels and scaled on use."""

    def __init__(self, width: int, height: int, scale: int = 1) -> None:
        self.w = width * scale
        self.h = height * scale
        self.s = scale
        self.px = bytearray(self.w * self.h * 4)

    def put(self, x: int, y: int, color: Rgba) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 4
            self.px[i : i + 4] = bytes(color)

    def rrect(self, x0: float, y0: float, x1: float, y1: float, r: float, color: Rgba) -> None:
        """Filled rounded rectangle; r=0 gives a plain rectangle."""
        s = self.s
        ax0, ay0, ax1, ay1, ar = int(x0 * s), int(y0 * s), int(x1 * s), int(y1 * s), int(r * s)
        for y in range(max(0, ay0), min(self.h, ay1)):
            for x in range(max(0, ax0), min(self.w, ax1)):
                cx = min(max(x, ax0 + ar), ax1 - ar - 1)
                cy = min(max(y, ay0 + ar), ay1 - ar - 1)
                if (x - cx) ** 2 + (y - cy) ** 2 <= ar * ar:
                    self.put(x, y, color)

    def disc(self, cx: float, cy: float, r: float, color: Rgba) -> None:
        s = self.s
        acx, acy, ar = int(cx * s), int(cy * s), int(r * s)
        for y in range(max(0, acy - ar), min(self.h, acy + ar + 1)):
            for x in range(max(0, acx - ar), min(self.w, acx + ar + 1)):
                if (x - acx) ** 2 + (y - acy) ** 2 <= ar * ar:
                    self.put(x, y, color)

    def downsample(self, factor: int) -> "Canvas":
        """Average factor x factor blocks. Alpha-weighted so edges stay the right colour."""
        out = Canvas(self.w // factor, self.h // factor)
        n = factor * factor
        for y in range(out.h):
            for x in range(out.w):
                r = g = b = a = 0
                for dy in range(factor):
                    row = ((y * factor + dy) * self.w + x * factor) * 4
                    for dx in range(factor):
                        i = row + dx * 4
                        alpha = self.px[i + 3]
                        r += self.px[i] * alpha
                        g += self.px[i + 1] * alpha
                        b += self.px[i + 2] * alpha
                        a += alpha
                j = (y * out.w + x) * 4
                if a:
                    out.px[j] = r // a
                    out.px[j + 1] = g // a
                    out.px[j + 2] = b // a
                out.px[j + 3] = a // n
        return out


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def encode_png(canvas: Canvas) -> bytes:
    """8-bit RGBA, no interlace, filter type 0, zlib level 9 (stable output)."""
    stride = canvas.w * 4
    raw = bytearray()
    for y in range(canvas.h):
        raw.append(0)  # per-scanline filter: none
        raw += canvas.px[y * stride : (y + 1) * stride]
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", canvas.w, canvas.h, 8, 6, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )


def build_icon() -> bytes:
    """128x128: rounded petrol tile, a battery shell with three charge bars, a rising sun."""
    c = Canvas(128, 128, SUPERSAMPLE)
    c.rrect(0, 0, 128, 128, 26, BG)
    c.disc(33, 29, 11, SUN)
    c.rrect(16, 44, 104, 84, 9, SHELL)  # shell
    c.rrect(21, 49, 99, 79, 5, BG)  # hollow it out, leaving a 5 px outline
    c.rrect(104, 56, 114, 72, 4, SHELL)  # terminal
    c.rrect(27, 56, 42, 72, 2, BAR)
    c.rrect(45, 56, 60, 72, 2, BAR)
    c.rrect(63, 56, 78, 72, 2, BAR_DIM)
    return encode_png(c.downsample(SUPERSAMPLE))


def build_logo() -> bytes:
    """250x100, transparent: a battery glyph and an ascending three-bar chart."""
    c = Canvas(250, 100, SUPERSAMPLE)
    c.rrect(4, 26, 92, 74, 10, BLUE)  # battery shell
    c.rrect(10, 32, 86, 68, 6, (0, 0, 0, 0))  # hollow
    c.rrect(92, 39, 101, 61, 4, BLUE)  # terminal
    c.rrect(24, 60, 40, 66, 2, SHELL)
    c.rrect(44, 52, 60, 66, 2, SHELL)
    c.rrect(64, 44, 80, 66, 2, SHELL)
    for x, height, color in ((124, 26, BLUE), (160, 42, TEAL), (196, 58, LIME)):
        c.rrect(x, 80 - height, x + 24, 80, 3, color)
    return encode_png(c.downsample(SUPERSAMPLE))


def main() -> int:
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "energy_manager")
    out_dir = os.path.normpath(out_dir)
    for name, data in (("icon.png", build_icon()), ("logo.png", build_logo())):
        path = os.path.join(out_dir, name)
        with open(path, "wb") as handle:
            handle.write(data)
        print(f"wrote {path} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
