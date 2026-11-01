"""Minimal PNG raster renderer for SPICE plots.

Pure stdlib: zlib for the compressed image data, a 3x5 bitmap font for labels.
This exists because the 32 bit interpreter that hosts the Multisim worker has no
Pillow, and a PNG is the only image format the model can actually look at.
"""

from __future__ import annotations

import math
import struct
import zlib
from typing import Dict, List, Sequence, Tuple

_FONT: Dict[str, Tuple[str, ...]] = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "001", "001", "001"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    "A": ("111", "101", "111", "101", "101"),
    "B": ("111", "101", "110", "101", "111"),
    "C": ("111", "100", "100", "100", "111"),
    "D": ("110", "101", "101", "101", "110"),
    "E": ("111", "100", "111", "100", "111"),
    "F": ("111", "100", "111", "100", "100"),
    "G": ("111", "100", "101", "101", "111"),
    "H": ("101", "101", "111", "101", "101"),
    "I": ("111", "010", "010", "010", "111"),
    "J": ("001", "001", "001", "101", "111"),
    "K": ("101", "101", "110", "101", "101"),
    "L": ("100", "100", "100", "100", "111"),
    "M": ("101", "111", "111", "101", "101"),
    "N": ("110", "101", "101", "101", "101"),
    "O": ("111", "101", "101", "101", "111"),
    "P": ("111", "101", "111", "100", "100"),
    "Q": ("111", "101", "101", "111", "001"),
    "R": ("111", "101", "111", "110", "101"),
    "S": ("111", "100", "111", "001", "111"),
    "T": ("111", "010", "010", "010", "010"),
    "U": ("101", "101", "101", "101", "111"),
    "V": ("101", "101", "101", "101", "010"),
    "W": ("101", "101", "111", "111", "101"),
    "X": ("101", "101", "010", "101", "101"),
    "Y": ("101", "101", "111", "010", "010"),
    "Z": ("111", "001", "010", "100", "111"),
    ".": ("000", "000", "000", "000", "010"),
    ",": ("000", "000", "000", "010", "100"),
    "-": ("000", "000", "111", "000", "000"),
    "+": ("000", "010", "111", "010", "000"),
    "(": ("001", "010", "010", "010", "001"),
    ")": ("100", "010", "010", "010", "100"),
    "/": ("001", "001", "010", "100", "100"),
    ":": ("000", "010", "000", "010", "000"),
    "=": ("000", "111", "000", "111", "000"),
    "%": ("101", "001", "010", "100", "101"),
    " ": ("000", "000", "000", "000", "000"),
}

GLYPH_W = 3
GLYPH_H = 5


class Canvas:
    """An RGB bitmap with the handful of primitives a line chart needs."""

    def __init__(self, width: int, height: int, background: Tuple[int, int, int] = (255, 255, 255)) -> None:
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self.pixels = bytearray(self.width * self.height * 3)
        for index in range(0, len(self.pixels), 3):
            self.pixels[index:index + 3] = bytes(background)

    def _set(self, x: int, y: int, colour: Tuple[int, int, int]) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            offset = (y * self.width + x) * 3
            self.pixels[offset:offset + 3] = bytes(colour)

    def fill_rect(self, x: int, y: int, w: int, h: int, colour: Tuple[int, int, int]) -> None:
        for row in range(int(y), int(y + h)):
            for column in range(int(x), int(x + w)):
                self._set(column, row, colour)

    def line(self, x0: float, y0: float, x1: float, y1: float, colour: Tuple[int, int, int], width: int = 1) -> None:
        x0, y0, x1, y1 = int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        error = dx - dy
        thick = max(1, int(width))
        while True:
            if thick <= 1:
                self._set(x0, y0, colour)
            else:
                self.fill_rect(x0, y0, thick, thick, colour)
            if x0 == x1 and y0 == y1:
                break
            doubled = 2 * error
            if doubled > -dy:
                error -= dy
                x0 += sx
            if doubled < dx:
                error += dx
                y0 += sy

    def polyline(self, points: Sequence[Tuple[float, float]], colour: Tuple[int, int, int], width: int = 2) -> None:
        previous = None
        for point in points:
            if previous is not None:
                self.line(previous[0], previous[1], point[0], point[1], colour, width)
            previous = point

    def text(self, x: float, y: float, value: str, colour: Tuple[int, int, int], scale: int = 2) -> int:
        """Draw ``value`` with its top-left at (x, y); returns the width used."""
        scale = max(1, int(scale))
        cursor = int(x)
        top = int(y)
        for character in str(value).upper():
            glyph = _FONT.get(character, _FONT[" "])
            for row_index, row in enumerate(glyph):
                for column_index, bit in enumerate(row):
                    if bit == "1":
                        self.fill_rect(
                            cursor + column_index * scale,
                            top + row_index * scale,
                            scale,
                            scale,
                            colour,
                        )
            cursor += (GLYPH_W + 1) * scale
        return cursor - int(x)

    def text_width(self, value: str, scale: int = 2) -> int:
        return len(str(value)) * (GLYPH_W + 1) * max(1, int(scale))

    def text_right(self, right: float, y: float, value: str, colour: Tuple[int, int, int], scale: int = 2) -> None:
        self.text(right - self.text_width(value, scale), y, value, colour, scale)

    def text_center(self, centre: float, y: float, value: str, colour: Tuple[int, int, int], scale: int = 2) -> None:
        width = self.text_width(value, scale)
        x = centre - width / 2.0
        # keep the label inside the image instead of clipping at the edge
        x = min(max(0.0, x), max(0.0, self.width - width))
        self.text(x, y, value, colour, scale)

    def png(self) -> bytes:
        raw = bytearray()
        stride = self.width * 3
        for row in range(self.height):
            raw.append(0)
            raw.extend(self.pixels[row * stride:(row + 1) * stride])

        def chunk(tag: bytes, payload: bytes) -> bytes:
            body = tag + payload
            return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

        header = struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b"")
        )


def nice_ticks(low: float, high: float, count: int = 5) -> List[float]:
    if not (math.isfinite(low) and math.isfinite(high)) or high == low:
        return [low]
    span = high - low
    raw = span / max(1, count)
    if raw == 0:
        return [low]
    magnitude = 10 ** math.floor(math.log10(abs(raw)))
    step = magnitude
    for factor in (1, 2, 2.5, 5, 10):
        step = factor * magnitude
        if span / step <= count:
            break
    ticks: List[float] = []
    value = math.floor(low / step) * step
    limit = high + step * 0.5
    while value <= limit and len(ticks) < 40:
        ticks.append(value)
        value += step
    return ticks


def decade_ticks(log_low: float, log_high: float) -> List[float]:
    if log_high <= log_low:
        return [10 ** log_low]
    first = int(math.floor(log_low))
    last = int(math.ceil(log_high))
    span = last - first
    ticks: List[float] = []
    if span <= 5:
        for exponent in range(first, last + 1):
            for factor in (1, 2, 5):
                value = factor * (10 ** exponent)
                if log_low <= math.log10(value) <= log_high:
                    ticks.append(value)
    else:
        stride = max(1, int(math.ceil(span / 6.0)))
        for exponent in range(first, last + 1, stride):
            ticks.append(10 ** exponent)
    return ticks or [10 ** log_low]


def tick_label(value: float) -> str:
    if value == 0:
        return "0"
    magnitude = abs(value)
    if magnitude >= 1e5 or magnitude < 1e-3:
        return "%.1E" % value
    if magnitude >= 100:
        return "%.0f" % value
    if magnitude >= 10:
        return "%.1f" % value
    return "%.3G" % value
