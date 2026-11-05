"""Text -> single-line (Hershey) polylines in pixel units.

One style per text: a font family, bold, italic, a per-line alignment, a line spacing and a
letter spacing. Each family maps to the Hershey faces the HersheyFonts package ships:

    sans       futural, bold futuram
    serif      timesr, bold timesrb, italic timesi, bold italic timesib
    script     scripts, bold scriptc
    gothic     gothiceng
    cursive    cursive
    gothicita  gothicita (Italian gothic)
    greek      greeks, bold greekc   (Latin keys type the Greek letters; no y/z)
    cyrillic   cyrillic              (Latin keys type the Cyrillic letters)

A face that is missing is synthesized: no italic -> the strokes are sheared by
ITALIC_SHEAR_DEG around each line's baseline; no bold -> the strokes are drawn a second
time shifted right by BOLD_OFFSET * size (a "double strike", so the pen and its
thickness stay what the user chose). Everything is linear in `size`, so strokes made at
one size scale exactly to another. The defaults reproduce the original sans output.

Spacing, both relative to `size` so they scale with it too:
    line_spacing    multiple of the default line pitch LINE_HEIGHT * size (1.0 = 1.4 em)
    letter_spacing  extra advance after every drawn character, in em (fractions of `size`)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache

from HersheyFonts import HersheyFonts

from .place import Polyline

LINE_HEIGHT = 1.4
_CAP_UNITS = 0.75  # normalize_rendering(1.0): "H" spans y 0.25..1.0 (y points up)

FONTS = ("sans", "serif", "script", "gothic", "cursive", "gothicita", "greek", "cyrillic")
ALIGNS = ("left", "center", "right")
LINE_SPACING_DEFAULT, LINE_SPACING_MIN, LINE_SPACING_MAX = 1.0, 0.8, 3.0  # x LINE_HEIGHT
LETTER_SPACING_DEFAULT, LETTER_SPACING_MIN, LETTER_SPACING_MAX = 0.0, -0.1, 0.5  # em
ITALIC_SHEAR_DEG = 12.0
BOLD_OFFSET = 0.04  # synthesized bold: second pass shifted right by this fraction of `size`

# (family, bold, italic) -> Hershey face
_FACES = {
    ("sans", False, False): "futural", ("sans", True, False): "futuram",
    ("serif", False, False): "timesr", ("serif", True, False): "timesrb",
    ("serif", False, True): "timesi", ("serif", True, True): "timesib",
    ("script", False, False): "scripts", ("script", True, False): "scriptc",
    ("gothic", False, False): "gothiceng",
    ("cursive", False, False): "cursive",
    ("gothicita", False, False): "gothicita",
    ("greek", False, False): "greeks", ("greek", True, False): "greekc",
    ("cyrillic", False, False): "cyrillic",
}


@dataclass
class InkTextResult:
    lines: list[Polyline]
    skipped: list[str]
    # per text line: the caret x for every position 0..len(line), in the same pixel units
    stops: list[list[float]] = field(default_factory=list)
    # per text line: the index in `text` where it starts (with max_width, one per wrapped row)
    row_starts: list[int] = field(default_factory=list)


def line_pitch(size: float, line_spacing: float = LINE_SPACING_DEFAULT) -> float:
    """Distance from one text line's top to the next, in pixels."""
    return (LINE_HEIGHT * line_spacing) * size


def face(font: str, bold: bool, italic: bool) -> tuple[str, bool, bool]:
    """(Hershey face, synthesize bold, synthesize italic) for a style."""
    if font not in FONTS:
        raise ValueError(f"unknown font {font!r}")
    bold, italic = bool(bold), bool(italic)
    for b, i in ((bold, italic), (bold, False), (False, italic), (False, False)):
        name = _FACES.get((font, b, i))
        if name is not None:
            return name, bold and not b, italic and not i
    raise ValueError(f"no face for {font!r}")  # pragma: no cover - every family has a regular


@lru_cache(maxsize=None)
def _font(name: str) -> HersheyFonts:
    f = HersheyFonts()
    f.load_default_font(name)
    f.normalize_rendering(1.0)
    return f


def text_to_polylines(text: str, size: float, font: str = "sans", bold: bool = False,
                      italic: bool = False, align: str = "left",
                      line_spacing: float = LINE_SPACING_DEFAULT,
                      letter_spacing: float = LETTER_SPACING_DEFAULT,
                      max_width: float | None = None) -> InkTextResult:
    """Strokes for `text`, one row per line. With `max_width` (pixels) lines are also broken
    so every row fits that width: after the last space that fits (the space stays at the
    row's end), or inside a word that is wider than the width on its own; alignment is then
    within `max_width`. Without it the output is what it always was."""
    if align not in ALIGNS:
        raise ValueError(f"unknown alignment {align!r}")
    name, fake_bold, fake_italic = face(font, bold, italic)
    hf = _font(name)
    glyphs = hf.all_glyphs
    unit = hf.render_options.scalex
    scale = size / _CAP_UNITS
    shear = math.tan(math.radians(ITALIC_SHEAR_DEG)) if fake_italic else 0.0
    extra = letter_spacing * size / scale  # font units added after each drawn character
    skipped: list[str] = []
    rows: list[list[Polyline]] = []
    row_stops: list[list[float]] = []
    limit = 0.0
    if max_width is None:
        texts = text.split("\n")
        row_starts = []
        i = 0
        for line in texts:
            row_starts.append(i)
            i += len(line) + 1
    else:
        # room for what is drawn past the advances: the synthesized italic's lean and bold's
        # second pass
        limit = float(max_width) - (1.5 * shear * size) - (BOLD_OFFSET * size if fake_bold else 0.0)
        texts, row_starts = _wrap(hf, text, unit, scale, extra, limit)
    for row, line in enumerate(texts):
        clean = []
        stops = [0.0]
        adv = 0.0
        for ch in line:
            if ch == " " or _supported(hf, ch):
                clean.append(ch)
                g = glyphs.get(ch)
                if g is not None:
                    adv += g.char_width * unit
                if extra:
                    adv += extra
            elif ch not in skipped:
                skipped.append(ch)
            stops.append(adv * scale)
        top = row * (LINE_HEIGHT * line_spacing) * size
        if extra:
            raw = _spaced_lines(hf, clean, unit, extra)
        else:
            raw = hf.lines_for_text("".join(clean))
        segs = [[(x1 * scale, top + (1.0 - y1) * scale), (x2 * scale, top + (1.0 - y2) * scale)]
                for (x1, y1), (x2, y2) in raw]
        if shear:
            base = top + size  # the baseline (y up 0.25) of this row
            segs = [[(x + (base - y) * shear, y) for x, y in s] for s in segs]
        rows.append(segs)
        row_stops.append(stops)
    if align != "left":
        widths = [max((x for s in segs for x, _ in s), default=0.0) for segs in rows]
        box_w = max(widths, default=0.0)
        k = 0.5 if align == "center" else 1.0
        for i, w in enumerate(widths):
            dx = (box_w - w) * k
            if max_width is not None:
                dx = max(0.0, (limit - w) * k)
            if dx:
                rows[i] = [[(x + dx, y) for x, y in s] for s in rows[i]]
                row_stops[i] = [x + dx for x in row_stops[i]]
    segs = [s for r in rows for s in r]
    if shear:  # descenders sheared left of the box: move the whole text back in
        left = min((x for s in segs for x, _ in s), default=0.0)
        if left < 0:
            segs = [[(x - left, y) for x, y in s] for s in segs]
            row_stops = [[x - left for x in st] for st in row_stops]
    lines = _merge(segs)
    if fake_bold:
        d = BOLD_OFFSET * size
        lines += _merge([[(x + d, y) for x, y in s] for s in segs])
    return InkTextResult(lines, skipped, row_stops, row_starts)


def _wrap(hf: HersheyFonts, text: str, unit: float, scale: float, extra: float,
          limit: float) -> tuple[list[str], list[int]]:
    """`text` broken into rows whose advances fit `limit` pixels, and where each row starts."""
    rows: list[str] = []
    starts: list[int] = []
    base = 0
    for line in text.split("\n"):
        stops = _stops(hf, line, unit, scale, extra)
        start, last_space, i = 0, None, 0
        while i < len(line):
            if line[i] == " ":
                last_space = i  # spaces never break a row on their own: they may hang over
            elif i > start and stops[i + 1] - stops[start] > limit:
                cut = last_space + 1 if last_space is not None else i
                rows.append(line[start:cut])
                starts.append(base + start)
                start, last_space = cut, None
                continue
            i += 1
        rows.append(line[start:])
        starts.append(base + start)
        base += len(line) + 1
    return rows, starts


def _stops(hf: HersheyFonts, line: str, unit: float, scale: float, extra: float) -> list[float]:
    """The caret x before every position of one line (as text_to_polylines computes them)."""
    glyphs = hf.all_glyphs
    adv = 0.0
    stops = [0.0]
    for ch in line:
        if ch == " " or _supported(hf, ch):
            g = glyphs.get(ch)
            if g is not None:
                adv += g.char_width * unit
            if extra:
                adv += extra
        stops.append(adv * scale)
    return stops


def _spaced_lines(hf: HersheyFonts, chars: list[str], unit: float, extra: float):
    """hf.lines_for_text("".join(chars)) with `extra` font units after every character."""
    glyphs = hf.all_glyphs
    out = []
    dx = 0.0
    for ch in chars:
        out += [((x1 + dx, y1), (x2 + dx, y2)) for (x1, y1), (x2, y2) in hf.lines_for_text(ch)]
        g = glyphs.get(ch)
        if g is not None:
            dx += g.char_width * unit
        dx += extra
    return out


def _supported(font, ch: str) -> bool:
    return bool(list(font.lines_for_text(ch)))


def _merge(segs: list[Polyline]) -> list[Polyline]:
    out: list[Polyline] = []
    for s in segs:
        if out and _close(out[-1][-1], s[0]):
            out[-1].append(s[1])
        else:
            out.append(list(s))
    return out


def _close(a, b) -> bool:
    return abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) < 1e-6
