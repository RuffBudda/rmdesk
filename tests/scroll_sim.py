"""A simulated keyboard-layout page for the scroll measuring tests (no Qt).

The page is an image indexed [page y, frame row]: frame row fy shows page x = fy - 936. The
frame at scroll s shows page y = 1404 - fx + s in column fx (compose.frame, KEYBOARD)."""
from __future__ import annotations

import numpy as np

H, W = 1872, 1404
PAGE_TOP, PAGE_BOTTOM = 234, 1638  # frame rows the page's width covers (page x -702..702)


def blank_page(length: int = 3600) -> np.ndarray:
    pg = np.full((length, H), 255, np.uint8)
    pg[:, :PAGE_TOP] = 200  # the grey round the page
    pg[:, PAGE_BOTTOM:] = 200
    return pg


def add_text(pg: np.ndarray, seed: int = 1, top: int = 120, bottom: int | None = None,
             line_h: int = 64) -> np.ndarray:
    """Typed-text-like lines: glyph blobs along page x, lines of different lengths, some
    blank lines between paragraphs."""
    rng = np.random.default_rng(seed)
    bottom = bottom if bottom is not None else pg.shape[0] - 80
    y = top
    while y + 30 < bottom:
        if rng.random() < 0.15:  # a paragraph break
            y += line_h
            continue
        x = PAGE_TOP + 60
        end = PAGE_TOP + 60 + int(rng.uniform(0.3, 1.0) * (PAGE_BOTTOM - PAGE_TOP - 120))
        while x < end:
            w = int(rng.integers(8, 22))
            h = int(rng.integers(14, 28))
            pg[y + 28 - h:y + 28, x:x + w] = int(rng.integers(0, 60))
            x += w + int(rng.integers(3, 9))
            if rng.random() < 0.18:
                x += 14  # a word space
        y += line_h
    return pg


def add_dots(pg: np.ndarray, step: int = 50) -> np.ndarray:
    for yy in range(0, pg.shape[0], step):
        for xx in range(PAGE_TOP, PAGE_BOTTOM, step):
            pg[yy:yy + 3, xx:xx + 3] = 120
    return pg


def add_lines(pg: np.ndarray, step: int = 70) -> np.ndarray:
    pg[::step, PAGE_TOP:PAGE_BOTTOM] = 150
    pg[1::step, PAGE_TOP:PAGE_BOTTOM] = 150
    return pg


def frame_at(pg: np.ndarray, scroll: int) -> np.ndarray:
    """The 1872x1404 frame the tablet shows the page at `scroll` (rows = frame y)."""
    py = scroll + W - np.arange(W)  # page y of each frame column
    out = np.full((H, W), 255, np.uint8)
    ok = (py >= 0) & (py < pg.shape[0])
    out[:, ok] = pg[py[ok], :].T
    return out


def text_page(seed: int = 1, length: int = 3600) -> np.ndarray:
    return add_text(blank_page(length), seed)
