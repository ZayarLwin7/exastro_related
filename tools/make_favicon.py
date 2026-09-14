#!/usr/bin/env python3
"""Regenerate static/favicon.ico from the geometry in static/favicon.svg.

Why a generator and not a committed binary: the icon has to keep matching the app
bar, and a file nobody can read is a file nobody updates. It is also pure
standard library -- no Pillow, no ImageMagick, no font rasteriser -- because the
machine this gets deployed to is a closed Japanese network where installing
image libraries is a change request, and an icon should never be one.

    python3 tools/make_favicon.py            # writes static/favicon.ico
    python3 tools/make_favicon.py --check    # exit 1 if the file is out of date

Sizes: 16, 32 and 48. Browsers pick one; 16 is the tab, 32 the pinned tab and
taskbar on Windows, 48 covers high-DPI. Each is drawn at four times the target
and box-filtered down, because three 2-unit strokes at 16px is exactly where
nearest-neighbour drawing turns to mud.
"""
import pathlib
import struct
import sys
import zlib

# --- the drawing, in the same 64-unit space as the SVG ---------------------
RADIUS_TILE = 15.0 / 64.0          # the rounded square's corner
# The app bar draws this glyph 40px wide with a 2.2 stroke; on a 16px tab that
# same stroke is one and a half pixels and turns to grey mush, so the icon carries
# it at 3.2 instead -- the only place the two differ, and the reason a tab icon is
# a drawing of its own rather than the same file scaled down.
ICON_STROKE = 3.2
# A stroke of ICON_STROKE in the glyph's own 24-unit space, and the glyph is
# laid down across 40 of the tile's 64 units -- so in unit space of the whole
# image that is (24/40) ... divided out, with half taken for the capsule:
BAR_HALF = ICON_STROKE / 24.0 * (40.0 / 64.0) / 2.0
# the app bar's `M4 7h16M4 12h16M4 17h10`, mapped from 24 units into the tile
# through translate(12 12) scale(1.66667) -- see static/favicon.svg
BARS = [(10.67, 18.67, 53.33, 18.67),
        (10.67, 32.00, 53.33, 32.00),
        (10.67, 45.33, 37.33, 45.33)]
ACCENT_A = (0x4F, 0x46, 0xE5)      # --acc
ACCENT_B = (0x7C, 0x3A, 0xED)      # --acc2
STROKE = (255, 255, 255)
SUPERSAMPLE = 4


def _bars_for(size):
    """The glyph's bars in unit space, for one target size.

    A 16px tile cannot show a line whose centre falls on a pixel boundary: it
    splits into two half-lit rows and reads as grey smudge. So at icon sizes the
    centres snap to pixel centres and the stroke is widened to at least 1.5px --
    the same thing a person drawing a favicon does by hand, done here from the
    same geometry rather than in a second file that can drift."""
    out = []
    for x0, y0, x1, y1 in BARS:
        half = BAR_HALF
        bx0, bx1, by = x0 / 64.0, x1 / 64.0, y0 / 64.0
        if size <= 24:
            py = int(round(y0 / 64.0 * size - 0.5)) + 0.5      # pixel centre
            by = py / size
            bx0 = round(x0 / 64.0 * size) / size
            bx1 = round(x1 / 64.0 * size) / size
            half = max(half, 0.75 / size)
        out.append((bx0, by, bx1, by, half))
    return out


def _to_pixel(signed, size):
    """Coverage from a signed distance in *unit* space.

    The two shapes below measure distance in units of the whole image, but an
    edge has to soften over one *pixel*, and a pixel is 1/size units. Mixing the
    two -- which a fixed `+ 0.5` does -- is how the first version of this file
    produced a hard-edged square with bars washed to 55 % white: the antialias
    term was a pixel wide at one size and fifty-five at another."""
    return max(0.0, min(1.0, 0.5 - signed * size))


def _rounded_rect_coverage(px, py, r, size):
    """Inside the tile: 1. Outside: 0. The corner is an honest arc, not a
    staircase, because a tab icon is read entirely as its silhouette."""
    qx = max(abs(px - 0.5) - (0.5 - r), 0.0)
    qy = max(abs(py - 0.5) - (0.5 - r), 0.0)
    d = (qx * qx + qy * qy) ** 0.5
    return _to_pixel(d - r, size)


def _bar_coverage(px, py, x0, y0, x1, y1, half, size):
    """A capsule: distance to the segment, less the stroke's half width."""
    dx, dy = x1 - x0, y1 - y0
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / length))
    cx, cy = x0 + t * dx, y0 + t * dy
    d = ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5
    return _to_pixel(d - half, size)


def _gradient(px, py):
    """The 135-degree sweep, sampled the way the SVG's linearGradient does."""
    f = max(0.0, min(1.0, (px + py) / 2.0))
    return tuple(round(a + (b - a) * f) for a, b in zip(ACCENT_A, ACCENT_B))


def render(size):
    """One RGBA image, drawn at SUPERSAMPLE*size and averaged down."""
    n = size * SUPERSAMPLE
    bars = _bars_for(size)
    raw = bytearray()
    for oy in range(size):
        for ox in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(SUPERSAMPLE):
                py_base = (oy + sy / SUPERSAMPLE) / size
                for sx in range(SUPERSAMPLE):
                    px = (ox + sx / SUPERSAMPLE) / size
                    py = py_base
                    tile = _rounded_rect_coverage(px, py, RADIUS_TILE, size)
                    if tile <= 0.0:
                        continue
                    col = _gradient(px, py)
                    ink = 0.0
                    for bx0, by, bx1, _by, half in bars:
                        ink = max(ink, _bar_coverage(px, py, bx0, by, bx1, by,
                                                     half, size))
                    r = round(col[0] + (STROKE[0] - col[0]) * ink)
                    g = round(col[1] + (STROKE[1] - col[1]) * ink)
                    b = round(col[2] + (STROKE[2] - col[2]) * ink)
                    acc[0] += r * tile
                    acc[1] += g * tile
                    acc[2] += b * tile
                    acc[3] += 255 * tile
            s = SUPERSAMPLE * SUPERSAMPLE
            raw += bytes(int(min(255, round(a / s))) for a in acc)
    return raw


def png(size):
    """A minimal RGBA PNG: signature, IHDR, one IDAT, IEND. No ancillary
    chunks, because nothing has to read it back except a browser."""
    raw = render(size)
    stride = size * 4
    scanlines = b"".join(b"\x00" + raw[y * stride:(y + 1) * stride]
                         for y in range(size))

    def chunk(kind, data):
        body = kind + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(scanlines, 9))
            + chunk(b"IEND", b""))


def ico(sizes):
    """PNG-in-ICO, which every browser that matters has accepted since about
    2010; the alternative (BMP-with-AND-mask) predates alpha and shows jaggies."""
    images = [(s, png(s)) for s in sizes]
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries, blob = b"", b""
    for size, data in images:
        w = size if size < 256 else 0
        h = size if size < 256 else 0
        entries += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        blob += data
    return header + entries + blob


def main():
    here = pathlib.Path(__file__).resolve().parent.parent
    target = here / "static" / "favicon.ico"
    built = ico([16, 32, 48])
    if "--check" in sys.argv:
        current = target.read_bytes() if target.exists() else b""
        if current == built:
            print("favicon.ico is up to date")
            return 0
        print("favicon.ico is OUT OF DATE -- run tools/make_favicon.py",
              file=sys.stderr)
        return 1
    target.write_bytes(built)
    sizes = ", ".join(str(s) for s in (16, 32, 48))
    print(f"wrote {target.name} ({len(built)} bytes; {sizes} px)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
