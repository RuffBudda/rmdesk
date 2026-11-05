"""Sharp text for line art: words recognised with Tesseract, redrawn as single-stroke text.

A label 6 px tall cannot be thinned into clean letters, so `lineart` (with sharp text on)
asks this module which words the picture holds, erases their ink from the raster it
thins, and draws each word in the Hershey sans face (`inktext.text_to_polylines`) fitted
into the word's own box. Lines, boxes and arrows are drawn as line art as before; what
Tesseract does not recognise (vertical or rotated words, odd fonts) stays line art too.

Tesseract runs locally (`tesseract stdin stdout --psm 11 tsv`, the image piped in; no
network), on the source picture at OCR_SCALE x (small pictures up to 3x, so 10 px labels
are big enough to read), never on the fitted box. The result is cached per picture file
(path, size, mtime), invert and scale: fitting, moving or rotating the box never runs it
again. Pure Python + cv2, no Qt; safe to call from a worker thread.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

TESSERACT = "/usr/bin/tesseract"
LANG = "eng"
PSM = 11  # sparse text: finds labels scattered over a diagram (3 and 6 miss or merge them)
OCR_DPI = 100  # per 1x; Tesseract guesses badly without it (stdin has no resolution)
# Upscaling before OCR: by the long side of the picture (Tesseract reads ~20 px letters
# best; 10-14 px labels in a 1500-2400 px chart need 2-3x).
OCR_SCALES = ((2000, 3), (4000, 2))  # long side up to -> factor; larger pictures 1x
OCR_MAX_PX = 40_000_000  # the upscaled image never exceeds this many pixels
OCR_TIMEOUT_S = 60
MIN_CONF = 60.0  # words below this confidence stay line art
SINGLE_CONF = 90.0  # a one-character word (often an arrowhead or a stray mark) needs this
JUNK = set("|[]{}()_~`^\\/<>=—–-")  # a word that is only these (or contains "|") is a box edge

UNAVAILABLE_NO_TESSERACT = "Sharp text needs Tesseract OCR (the tesseract program is not installed)"
UNAVAILABLE_NO_ENG = "Sharp text needs Tesseract's English model (eng); it is not installed"


@dataclass(frozen=True)
class Word:
    """A recognised word: its text, Tesseract's confidence and its box in source pixels."""
    text: str
    conf: float
    x: float
    y: float
    w: float
    h: float


def _exe() -> str | None:
    if os.access(TESSERACT, os.X_OK):
        return TESSERACT
    return shutil.which("tesseract")


@lru_cache(maxsize=1)
def status() -> tuple[bool, str]:
    """(available, why not): Tesseract is installed and has the eng model."""
    exe = _exe()
    if exe is None:
        return False, UNAVAILABLE_NO_TESSERACT
    try:
        out = subprocess.run([exe, "--list-langs"], capture_output=True, timeout=10,
                             check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False, UNAVAILABLE_NO_TESSERACT
    text = (out.stdout or b"").decode(errors="replace") + (out.stderr or b"").decode(
        errors="replace")
    langs = {ln.strip() for ln in text.splitlines()}
    if LANG not in langs:
        return False, UNAVAILABLE_NO_ENG
    return True, ""


def available() -> bool:
    return status()[0]


def ocr_scale(w: int, h: int) -> int:
    """The upscaling factor for a w x h source picture."""
    side = max(w, h)
    k = 1
    for limit, factor in OCR_SCALES:
        if side <= limit:
            k = factor
            break
    while k > 1 and w * h * k * k > OCR_MAX_PX:
        k -= 1
    return k


_cache: dict[tuple, tuple[Word, ...]] = {}
_lock = threading.Lock()
_CACHE_MAX = 32


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def recognise(path: Path, raw: np.ndarray | None = None,
              invert: bool = False) -> tuple[Word, ...]:
    """The plausible words in the picture at `path` (boxes in source pixels), cached.
    `raw` is the picture as cv2.imread(IMREAD_UNCHANGED) gave it (read again if None).
    No words if Tesseract is missing or fails."""
    if not available():
        return ()
    p = Path(path)
    try:
        st = p.stat()
        ident = (str(p.resolve()), st.st_size, st.st_mtime_ns)
    except OSError:
        ident = (str(p), 0, 0)
    if raw is None:
        raw = cv2.imread(str(p), cv2.IMREAD_UNCHANGED)
        if raw is None:
            return ()
    h, w = raw.shape[:2]
    k = ocr_scale(w, h)
    key = ident + (bool(invert), k)
    with _lock:  # one OCR at a time (it is CPU-heavy), and never twice for one key
        hit = _cache.get(key)
        if hit is not None:
            return hit
        words = _run(raw, k, invert)
        if len(_cache) >= _CACHE_MAX:
            _cache.pop(next(iter(_cache)))
        _cache[key] = words
        return words


def _grey(raw: np.ndarray, invert: bool) -> np.ndarray:
    """8-bit grey, alpha over white, dark ink (inverted first with `invert`)."""
    from .shade import _darkness
    h, w = raw.shape[:2]
    d = _darkness(raw, w, h, (0, 0, w, h), 1.0, 1.0, invert)
    return np.clip(255.0 * (1.0 - d), 0, 255).astype(np.uint8)


def _run(raw: np.ndarray, k: int, invert: bool) -> tuple[Word, ...]:
    exe = _exe()
    if exe is None:
        return ()
    grey = _grey(raw, invert)
    if k > 1:
        grey = cv2.resize(grey, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC)
    ok, png = cv2.imencode(".png", grey)
    if not ok:
        return ()
    env = dict(os.environ, OMP_THREAD_LIMIT="1")  # faster on small images, no thread storm
    try:
        out = subprocess.run([exe, "stdin", "stdout", "-l", LANG, "--psm", str(PSM),
                              "--dpi", str(OCR_DPI * k), "tsv"],
                             input=png.tobytes(), capture_output=True, timeout=OCR_TIMEOUT_S,
                             check=False, env=env)
    except (OSError, subprocess.SubprocessError):
        return ()
    if out.returncode != 0:
        return ()
    return tuple(w for w in parse_tsv(out.stdout.decode(errors="replace"), k) if plausible(w))


def parse_tsv(tsv: str, k: float = 1.0) -> list[Word]:
    """Word rows (level 5) of Tesseract's TSV, boxes divided by the OCR scale `k`."""
    words = []
    for line in tsv.splitlines()[1:]:
        f = line.split("\t")
        if len(f) < 12 or f[0] != "5":
            continue
        text = f[11].strip()
        try:
            left, top, width, height = (float(v) for v in f[6:10])
            conf = float(f[10])
        except ValueError:
            continue
        if text and width > 0 and height > 0:
            words.append(Word(text, conf, left / k, top / k, width / k, height / k))
    return words


def plausible(w: Word) -> bool:
    """Confident, real text: not a box edge read as "|", not a lone low-confidence symbol,
    and only characters the Hershey sans face can draw."""
    t = w.text
    if w.conf < MIN_CONF or not any(c.isalnum() for c in t) or "|" in t:
        return False
    if all(c in JUNK for c in t):
        return False
    if len(t) == 1 and w.conf < SINGLE_CONF:
        return False
    if w.h > 4 * w.w and len(t) > 1:  # a tall thin box: a vertical line, not a word
        return False
    return _drawable(t)


def _drawable(text: str) -> bool:
    from .inktext import text_to_polylines
    return not text_to_polylines(text, 1.0).skipped


# -- erasing a word's ink and drawing it again -----------------------------------------------

SHARP_MAX_PX = 24.0  # words taller than this once fitted (box px) stay line art: readable already
LINE_K = 1.5  # ink running straight for this many word heights is a line, never erased
MARGIN_K = 0.15  # the word box grows by this x its height (plus a pixel) to hold its glyphs
GROW_PX = 0.6  # the glyph pixels are dilated by this (box px) to take their soft edges too
PEN_K = 0.12  # a redrawn word's pen: this x its cap height (a regular weight), from the min...
INSET_MAX = 0.125  # ...and its strokes stay half a pen inside the box, at most this x its height
SQUEEZE_MIN = 0.7  # a word wider than its box is squeezed down to this, then made smaller
SPACING_MAX_EM = 0.3  # a word narrower than its box is letter-spaced by up to this


def erase_words(ink: np.ndarray, rects, r: float = 1.0) -> None:
    """Remove the words' glyphs from the boolean raster `ink` in place. `rects`: each word's
    box (x0, y0, x1, y1) in raster pixels; `r` raster pixels per box pixel.

    Only ink inside the (slightly grown) word box goes, and never ink that is part of a
    straight horizontal or vertical run longer than LINE_K word heights that reaches out of
    the box (a box border or an arrow through or beside the word): those stay line art."""
    H, W = ink.shape
    for x0, y0, x1, y1 in rects:
        hh = max(y1 - y0, 1.0)
        L = max(int(round(LINE_K * hh)), 3)
        m = MARGIN_K * hh + r
        bx0, by0 = max(0, int(np.floor(x0 - m))), max(0, int(np.floor(y0 - m)))
        bx1, by1 = min(W, int(np.ceil(x1 + m))), min(H, int(np.ceil(y1 + m)))
        if bx1 <= bx0 or by1 <= by0:
            continue
        wx0, wy0 = max(0, bx0 - L), max(0, by0 - L)  # a window that sees the lines whole
        wx1, wy1 = min(W, bx1 + L), min(H, by1 + L)
        sub = ink[wy0:wy1, wx0:wx1]
        u8 = sub.astype(np.uint8)
        lines = (cv2.morphologyEx(u8, cv2.MORPH_OPEN, np.ones((1, L), np.uint8))
                 | cv2.morphologyEx(u8, cv2.MORPH_OPEN, np.ones((L, 1), np.uint8))) > 0
        inbox = np.zeros_like(sub)
        inbox[by0 - wy0:by1 - wy0, bx0 - wx0:bx1 - wx0] = True
        if lines.any():  # a run that stays inside the box (letters merged at their baseline)
            n, lab = cv2.connectedComponents(lines.astype(np.uint8), connectivity=8)
            out = np.bincount(lab[~inbox], minlength=n) > 0  # ...is the word's, not a line
            out[0] = False
            lines = out[lab]
        glyph = sub & inbox & ~lines
        if not glyph.any():
            continue
        k = max(1, int(round(GROW_PX * r)))
        grown = cv2.dilate(glyph.astype(np.uint8),
                           cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * k + 1,) * 2)) > 0
        sub[grown & ~lines] = False


def word_pen(height: float, pen_min: float, pen_max: float) -> float:
    """The pen (box px) a word `height` box px tall is redrawn with."""
    return float(min(max(PEN_K * 0.75 * height, pen_min), max(pen_max, pen_min)))


def word_lines(text: str, x0: float, y0: float, x1: float, y1: float,
               pen: float) -> list[np.ndarray]:
    """`text` as Hershey sans polylines (box px) fitted into the box (x0, y0)-(x1, y1):
    its letters' height fills the box height (less half the `pen` each side), it is
    squeezed or letter-spaced to the box width, and centred both ways."""
    from .inktext import text_to_polylines
    bw, bh = x1 - x0, y1 - y0
    if bw <= 0 or bh <= 0:
        return []
    inset = min(pen / 2, INSET_MAX * bh)
    avail_w, avail_h = max(bw - 2 * inset, 0.5), max(bh - 2 * inset, 0.5)

    def extent(lines):
        pts = np.array([p for ln in lines for p in ln], np.float64)
        return pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max()

    unit = text_to_polylines(text, 1.0).lines
    if not unit:
        return []
    gx0, gy0, gx1, gy1 = extent(unit)
    size = avail_h / max(gy1 - gy0, 0.25)  # a dash (no height) as a quarter-em tall word
    sx, spacing = 1.0, 0.0
    natural = (gx1 - gx0) * size
    if natural > avail_w:
        sx = avail_w / natural
        if sx < SQUEEZE_MIN:
            size *= sx / SQUEEZE_MIN
            sx = SQUEEZE_MIN
    elif len(text) > 1:
        spacing = min((avail_w - natural) / (len(text) - 1) / size, SPACING_MAX_EM)
    lines = text_to_polylines(text, size, letter_spacing=spacing).lines
    lx0, ly0, lx1, ly1 = extent(lines)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    mx, my = (lx0 + lx1) / 2, (ly0 + ly1) / 2
    out = []
    for ln in lines:
        p = np.array(ln, np.float64)
        p[:, 0] = cx + (p[:, 0] - mx) * sx
        p[:, 1] = cy + (p[:, 1] - my)
        out.append(p)
    return out
