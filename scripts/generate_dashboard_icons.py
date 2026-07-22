#!/usr/bin/env python3
"""Generate the dashboard's small, dependency-free PNG icon set."""

from __future__ import annotations

import argparse
import binascii
import struct
import zlib
from pathlib import Path


NAVY = (15, 19, 24, 255)
TEAL = (53, 196, 160, 255)
AMBER = (245, 168, 42, 255)
WHITE = (255, 255, 255, 255)
TRANSPARENT = (0, 0, 0, 0)


def _inside_rounded_rect(
    x: float,
    y: float,
    left: float,
    top: float,
    right: float,
    bottom: float,
    radius: float,
) -> bool:
    cx = min(max(x, left + radius), right - radius)
    cy = min(max(y, top + radius), bottom - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def _inside_polygon(x: float, y: float, points: tuple[tuple[float, float], ...]) -> bool:
    inside = False
    previous = points[-1]
    for current in points:
        x1, y1 = previous
        x2, y2 = current
        if (y1 > y) != (y2 > y):
            crossing = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < crossing:
                inside = not inside
        previous = current
    return inside


def _pixel_at(x: float, y: float, *, maskable: bool) -> tuple[int, int, int, int]:
    if maskable:
        color = NAVY
    elif _inside_rounded_rect(x, y, 24, 24, 488, 488, 104):
        color = NAVY
    else:
        color = TRANSPARENT

    # A sun and battery form the same compact energy mark used by the dashboard.
    if (x - 160) ** 2 + (y - 147) ** 2 <= 54 ** 2:
        color = AMBER

    outer_battery = _inside_rounded_rect(x, y, 104, 190, 398, 372, 38)
    inner_battery = _inside_rounded_rect(x, y, 130, 216, 372, 346, 18)
    battery_terminal = _inside_rounded_rect(x, y, 398, 241, 430, 321, 12)
    if (outer_battery and not inner_battery) or battery_terminal:
        color = TEAL

    bolt = ((273, 218), (205, 296), (253, 296), (229, 344), (317, 266), (267, 266))
    if _inside_polygon(x, y, bolt):
        color = WHITE
    return color


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)


def _render_png(size: int, *, maskable: bool) -> bytes:
    scale = 512 / size
    rows = bytearray()
    for row in range(size):
        rows.append(0)
        for column in range(size):
            x = (column + 0.5) * scale
            y = (row + 0.5) * scale
            rows.extend(_pixel_at(x, y, maskable=maskable))
    header = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return header + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + _png_chunk(b"IEND", b"")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parents[1] / "growatt_guard" / "assets")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, size, maskable in (
        ("dashboard-icon-180.png", 180, False),
        ("dashboard-icon-192.png", 192, False),
        ("dashboard-icon-512.png", 512, False),
        ("dashboard-icon-maskable-512.png", 512, True),
    ):
        (args.output_dir / name).write_bytes(_render_png(size, maskable=maskable))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
