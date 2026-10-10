"""WP4: the icon generator is deterministic and produces valid PNGs (SPEC.md section 15).

    python3 -m pytest tests/test_wp4_icons.py -q
"""

from __future__ import annotations

import importlib.util
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_generator():
    spec = importlib.util.spec_from_file_location("sem_make_icons", ROOT / "tools" / "make_icons.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


make_icons = _load_generator()


def _png_size(png: bytes) -> tuple[int, int]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return struct.unpack(">II", png[16:24])


def _pixels(png: bytes) -> tuple[int, int, bytes]:
    """Decode our own PNGs: 8-bit RGBA, filter type 0, one IDAT."""
    width, height = _png_size(png)
    assert png[24] == 8 and png[25] == 6, "expected 8-bit RGBA"
    idat = bytearray()
    offset = 8
    while offset < len(png):
        length = struct.unpack(">I", png[offset : offset + 4])[0]
        tag = png[offset + 4 : offset + 8]
        if tag == b"IDAT":
            idat += png[offset + 8 : offset + 8 + length]
        offset += 12 + length
    raw = zlib.decompress(bytes(idat))
    stride = 1 + width * 4
    assert len(raw) == height * stride, "scanline filter bytes missing"
    body = b"".join(raw[y * stride + 1 : (y + 1) * stride] for y in range(height))
    return width, height, body


def test_icon_is_128x128_rgba():
    assert _png_size(make_icons.build_icon()) == (128, 128)


def test_logo_is_250x100():
    assert _png_size(make_icons.build_logo()) == (250, 100)


def test_pixels_round_trip_and_have_visible_content():
    for png in (make_icons.build_icon(), make_icons.build_logo()):
        width, height, body = _pixels(png)
        assert width * height * 4 == len(body)
        colours = {body[i : i + 4] for i in range(0, len(body), 4)}
        assert len(colours) >= 4, "flat image: the drawing did not land on the canvas"
        assert any(body[i + 3] == 255 for i in range(0, len(body), 4)), "nothing fully opaque"


def test_generation_is_deterministic():
    assert make_icons.build_icon() == make_icons.build_icon()
    assert make_icons.build_logo() == make_icons.build_logo()


def test_committed_assets_match_the_generator():
    assert (ROOT / "energy_manager" / "icon.png").read_bytes() == make_icons.build_icon()
    assert (ROOT / "energy_manager" / "logo.png").read_bytes() == make_icons.build_logo()
