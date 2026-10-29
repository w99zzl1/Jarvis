#!/usr/bin/env python
"""Generates the app icon (PNG + ICO) with no third-party dependencies.

    python scripts/make_icon.py

Writes:
    src-tauri/icons/32x32.png
    src-tauri/icons/128x128.png
    src-tauri/icons/128x128@2x.png      (256x256)
    src-tauri/icons/icon.ico            (16..256, 32-bit)
    public/favicon.png                  (64x64)

The mark: a cyan core inside a thin ring on a dark rounded square, matching the
orb in the app. Everything is drawn analytically and supersampled, so it stays
crisp at every size without shipping an image asset in git history.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "src-tauri" / "icons"
PUBLIC = ROOT / "public"

BG_TOP = (7, 16, 25)
BG_BOTTOM = (2, 4, 10)
CORE = (140, 245, 255)
CORE_MID = (0, 200, 235)
CORE_EDGE = (0, 130, 200)
RING = (0, 217, 255)
ACCENT = (139, 92, 255)


def _mix(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def _rounded_alpha(x, y, half, radius):
    """Coverage of a rounded square centered at 0 with half-size ``half``."""
    ax, ay = abs(x), abs(y)
    if ax <= half - radius or ay <= half - radius:
        inside = ax <= half and ay <= half
        return 1.0 if inside else 0.0
    dx = ax - (half - radius)
    dy = ay - (half - radius)
    if dx <= 0 or dy <= 0:
        return 1.0
    return 1.0 if math.hypot(dx, dy) <= radius else 0.0


def sample(x, y):
    """RGBA at normalized coordinates in [-1, 1]."""
    # Rounded square background with a vertical gradient.
    bg = _rounded_alpha(x, y, 0.98, 0.46)
    if bg <= 0.0:
        return (0, 0, 0, 0)

    color = _mix(BG_TOP, BG_BOTTOM, (y + 1.0) / 2.0)
    alpha = 1.0

    # Soft glow behind the core.
    dist = math.hypot(x, y)
    if dist < 0.95:
        glow = (1.0 - dist / 0.95) ** 2 * 0.45
        color = _mix(color, ACCENT if x < 0 else CORE_MID, glow * 0.5)

    # Thin ring.
    ring_w = 0.028
    if abs(dist - 0.66) <= ring_w:
        t = 1.0 - abs(dist - 0.66) / ring_w
        color = _mix(color, RING, 0.55 + 0.45 * t)

    # Core sphere: bright center, darker rim.
    r = 0.44
    if dist <= r:
        t = dist / r
        if t < 0.55:
            core = _mix(CORE, CORE_MID, t / 0.55)
        else:
            core = _mix(CORE_MID, CORE_EDGE, (t - 0.55) / 0.45)
        # Specular highlight, upper left.
        hl = math.hypot(x + 0.13, y + 0.13)
        if hl < 0.18:
            core = _mix(core, (255, 255, 255), (1.0 - hl / 0.18) ** 2 * 0.75)
        edge = 1.0 if dist < r - 0.015 else max(0.0, (r - dist) / 0.015)
        color = _mix(color, core, edge)

    return (int(color[0]), int(color[1]), int(color[2]), int(alpha * bg * 255))


def render(size, ss=4):
    """Render ``size`` x ``size`` RGBA rows, supersampled ``ss`` times."""
    rows = []
    step = 2.0 / (size * ss)
    for py in range(size):
        row = bytearray()
        for px in range(size):
            r = g = b = a = 0.0
            for sy in range(ss):
                y = -1.0 + (py * ss + sy + 0.5) * step
                for sx in range(ss):
                    x = -1.0 + (px * ss + sx + 0.5) * step
                    sr, sg, sb, sa = sample(x, y)
                    # Premultiplied accumulation keeps edges from darkening.
                    r += sr * sa
                    g += sg * sa
                    b += sb * sa
                    a += sa
            n = ss * ss
            if a > 0:
                # Colors were accumulated premultiplied, so divide by the total
                # alpha weight (not by n) to recover the average color.
                row += bytes(
                    (
                        min(255, int(r / a)),
                        min(255, int(g / a)),
                        min(255, int(b / a)),
                        min(255, int(a / n)),
                    )
                )
            else:
                row += b"\x00\x00\x00\x00"
        rows.append(bytes(row))
    return rows


def png_bytes(size, rows):
    raw = b"".join(b"\x00" + row for row in rows)

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def bmp_bytes(size, rows):
    """32-bit BGRA DIB for an ICO entry (bottom-up, plus an empty AND mask)."""
    xor = bytearray()
    for row in reversed(rows):
        for i in range(0, len(row), 4):
            r, g, b, a = row[i : i + 4]
            xor += bytes((b, g, r, a))
    mask_row = ((size + 31) // 32) * 4
    and_mask = b"\x00" * (mask_row * size)
    header = struct.pack(
        "<IiiHHIIiiII",
        40, size, size * 2, 1, 32, 0, len(xor) + len(and_mask), 0, 0, 0, 0,
    )
    return header + bytes(xor) + and_mask


def ico_bytes(sizes):
    images = []
    for size in sizes:
        rows = render(size)
        images.append((size, bmp_bytes(size, rows)))

    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for size, data in images:
        out += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,
            0 if size >= 256 else size,
            0,
            0,
            1,
            32,
            len(data),
            offset,
        )
        offset += len(data)
    for _, data in images:
        out += data
    return out


def main():
    ICONS.mkdir(parents=True, exist_ok=True)
    PUBLIC.mkdir(parents=True, exist_ok=True)

    targets = {
        ICONS / "32x32.png": 32,
        ICONS / "128x128.png": 128,
        ICONS / "128x128@2x.png": 256,
    }
    for path, size in targets.items():
        path.write_bytes(png_bytes(size, render(size)))
        print(f"wrote {path.relative_to(ROOT)} ({size}x{size})")

    ico = ico_bytes([16, 24, 32, 48, 64, 128, 256])
    (ICONS / "icon.ico").write_bytes(ico)
    print(f"wrote {ICONS / 'icon.ico'} ({len(ico)} bytes)")

    favicon = png_bytes(64, render(64))
    (PUBLIC / "favicon.png").write_bytes(favicon)
    print(f"wrote {PUBLIC / 'favicon.png'} (64x64)")


if __name__ == "__main__":
    main()
