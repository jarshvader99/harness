#!/usr/bin/env python3
"""Draw TurboHarness's icon and build AppIcon.icns from it.

No image library is used: the marks are signed-distance shapes, drawn with an
analytic antialias and written straight into a PNG. Everything is generated, so
the icon can be re-rendered or tweaked without any source artwork.

Usage: python3 app/make-icon.py    ->  app/AppIcon.icns
"""

import math
import os
import shutil
import struct
import subprocess
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
SIZE = 1024

TOP = (36, 42, 110)        # indigo
BOTTOM = (13, 112, 128)    # teal
RING = (124, 247, 236)     # mint
BOLT = (255, 253, 248)     # warm white


def clamp(value, low=0.0, high=1.0):
    return low if value < low else high if value > high else value


def mix(a, b, t):
    return (a[0] + (b[0] - a[0]) * t,
            a[1] + (b[1] - a[1]) * t,
            a[2] + (b[2] - a[2]) * t)


def coverage(distance):
    """Antialias a signed distance (in pixels) into 0..1 coverage."""
    return clamp(0.5 - distance)


def rounded_rect(px, py, half_w, half_h, radius):
    dx = abs(px) - (half_w - radius)
    dy = abs(py) - (half_h - radius)
    outside = math.hypot(max(dx, 0.0), max(dy, 0.0))
    inside = min(max(dx, dy), 0.0)
    return outside + inside - radius


def segment_distance(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    length = vx * vx + vy * vy
    t = 0.0 if length == 0 else clamp((wx * vx + wy * vy) / length)
    return math.hypot(wx - vx * t, wy - vy * t)


def polygon_distance(px, py, points):
    """Signed distance to a polygon: negative inside."""
    best = float("inf")
    inside = False
    count = len(points)
    for index in range(count):
        ax, ay = points[index]
        bx, by = points[(index + 1) % count]
        best = min(best, segment_distance(px, py, ax, ay, bx, by))
        if (ay > py) != (by > py):
            crossing = (bx - ax) * (py - ay) / (by - ay) + ax
            if px < crossing:
                inside = not inside
    return -best if inside else best


def arc_distance(px, py, radius, half_width, start_deg, end_deg):
    """Signed distance to a circular arc, with round caps at both ends."""
    distance = math.hypot(px, py)
    angle = math.degrees(math.atan2(py, px)) % 360.0
    start = start_deg % 360.0
    end = end_deg % 360.0
    span = (end - start) % 360.0
    if (angle - start) % 360.0 <= span:
        spine = abs(distance - radius)
    else:
        spine = min(
            math.hypot(px - radius * math.cos(math.radians(edge)),
                       py - radius * math.sin(math.radians(edge)))
            for edge in (start, end)
        )
    return spine - half_width


def bolt_points(size):
    """An upright lightning bolt, small enough to sit inside the ring.

    Drawn on a 24-unit grid and scaled about the centre, so the proportions hold
    at every icon size and it still reads as a bolt at 16 pixels.
    """
    shape = [(11, 21), (11, 13), (7, 13), (12, 3), (12, 11), (17, 11)]
    scale = 0.72
    return [((0.5 + (x / 24 - 0.5) * scale) * size,
             (0.5 + (y / 24 - 0.5) * scale) * size) for x, y in shape]


def render(size=SIZE):
    pixels = bytearray(size * size * 4)
    centre = size / 2.0
    radius = size * 0.345
    ring_width = size * 0.028
    # The loop measures from the centre, so the bolt has to as well.
    bolt = [(x - centre, y - centre) for x, y in bolt_points(size)]

    for y in range(size):
        py = y - centre
        row = y * size * 4
        gradient = y / (size - 1)
        base = mix(TOP, BOTTOM, gradient ** 0.85)
        for x in range(size):
            px = x - centre
            offset = row + x * 4

            # The tile itself: a rounded square with the app-icon proportions.
            body = rounded_rect(px, py, size * 0.5, size * 0.5, size * 0.225)
            alpha = coverage(body)
            if alpha <= 0.0:
                continue

            colour = base
            # A soft light from the upper left, so the tile is not flat.
            glow = clamp(1.0 - math.hypot(px + size * 0.22, py + size * 0.28) / (size * 0.85))
            colour = mix(colour, mix(colour, (255, 255, 255), 0.30), glow * 0.55)
            # And a vignette into the corners.
            vignette = clamp(math.hypot(px, py) / (size * 0.78))
            colour = mix(colour, (0, 0, 0), vignette * 0.16)

            # The harness: an open ring the bolt sits inside.
            ring = arc_distance(px, py, radius, ring_width, -60, 240)
            ring_alpha = coverage(ring) * 0.92
            if ring_alpha > 0:
                colour = mix(colour, RING, ring_alpha)

            # The bolt, with a soft shadow so it lifts off the tile.
            shape = polygon_distance(px, py, bolt)
            bolt_alpha = coverage(shape)
            if bolt_alpha > 0:
                colour = mix(colour, BOLT, bolt_alpha)
            else:
                shadow = coverage(polygon_distance(px - size * 0.006, py - size * 0.010, bolt)
                                  + size * 0.004)
                if shadow > 0:
                    colour = mix(colour, (4, 12, 30), shadow * 0.30)

            pixels[offset] = int(clamp(colour[0], 0, 255))
            pixels[offset + 1] = int(clamp(colour[1], 0, 255))
            pixels[offset + 2] = int(clamp(colour[2], 0, 255))
            pixels[offset + 3] = int(clamp(alpha * 255, 0, 255))
    return pixels


def write_png(path, size, rgba):
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    with open(path, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n")
        handle.write(chunk(b"IHDR", header))
        handle.write(chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
        handle.write(chunk(b"IEND", b""))


def build():
    png = os.path.join(HERE, "icon-1024.png")
    print(f"drawing {png}")
    write_png(png, SIZE, render())

    iconset = os.path.join(HERE, "AppIcon.iconset")
    shutil.rmtree(iconset, ignore_errors=True)
    os.makedirs(iconset)
    for points in (16, 32, 128, 256, 512):
        for scale, suffix in ((1, ""), (2, "@2x")):
            target = os.path.join(iconset, f"icon_{points}x{points}{suffix}.png")
            subprocess.run(["sips", "-z", str(points * scale), str(points * scale),
                            png, "--out", target],
                           check=True, capture_output=True)

    icns = os.path.join(HERE, "AppIcon.icns")
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns], check=True)
    shutil.rmtree(iconset)
    print(f"wrote {icns}")


if __name__ == "__main__":
    build()
