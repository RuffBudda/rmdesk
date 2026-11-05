"""Pen test strip for shaded pictures, for scripts/shade_strip.py.

Three black-to-white gradient blocks (600x120 box px) stacked vertically with
40 px gaps, centred on a portrait page, one per pen (fineliner, ballpoint,
pencil), each with a small ink-text label above it, left-aligned. The user
looks at the tablet and says which pen shades smoothest.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import cv2
import numpy as np
from rmscene import scene_items as si

from ..notebook import page as pagemod
from ..notebook.opened import OpenedPage
from ..notebook.page import PAGE_H, PAGE_W
from . import build, inktext, shade
from .items import Box, ImageItem, InkTextItem, PenStyle

BLOCK_W, BLOCK_H = 600, 120
GAP = 40
LABEL_SIZE = 20.0  # cap height of the label text, px
LABEL_H = 40  # label row above each block (text plus a little air)
LABEL_PAD = 8  # label top sits this far below the row top (ascenders rise above the cap line)
PENS = ("fineliner", "ballpoint", "pencil")
PROMPT = ("Look at the tablet: which of the three gradients looks smoothest from black to "
          "white? Tell Claude: fineliner, ballpoint or pencil.")


class LandscapePage(Exception):
    """Positions on landscape pages are unconfirmed, so no strip is drawn there."""


def gradient_image() -> np.ndarray:
    """Black (left) to white (right), BLOCK_H x BLOCK_W, uint8 grey."""
    row = np.linspace(0, 255, BLOCK_W).round().astype(np.uint8)
    return np.tile(row, (BLOCK_H, 1))


def block_boxes() -> list[tuple[str, Box]]:
    """(pen, block box) per pen, top to bottom, centred on the page."""
    total = len(PENS) * (LABEL_H + BLOCK_H) + (len(PENS) - 1) * GAP
    x = (PAGE_W - BLOCK_W) / 2
    y = (PAGE_H - total) / 2
    out = []
    for pen in PENS:
        out.append((pen, Box(x, y + LABEL_H, BLOCK_W, BLOCK_H)))
        y += LABEL_H + BLOCK_H + GAP
    return out


def build_strip() -> list[si.Line]:
    """All label and gradient lines of the strip, in page coordinates."""
    lines: list[si.Line] = []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "gradient.png"
        if not cv2.imwrite(str(path), gradient_image()):
            raise RuntimeError("cannot write the temporary gradient picture")
        for pen, box in block_boxes():
            label = InkTextItem(pen, Box(box.x, box.y - LABEL_H + LABEL_PAD, BLOCK_W, LABEL_H - LABEL_PAD),
                                size=LABEL_SIZE, pen=PenStyle("fineliner", "black"))
            res = inktext.text_to_polylines(pen, LABEL_SIZE)
            lines += build.to_lines(label, res.lines)
            item = ImageItem(path, box, pen=PenStyle(pen, "black"))
            lines += build.shade_to_lines(item, shade.shade_item(item), item.pen)
    return lines


def build_page(opened: OpenedPage) -> tuple[bytes, int]:
    if opened.orientation != "portrait":
        raise LandscapePage("This page is landscape; the test strip's position there is "
                            "unconfirmed. Open a portrait page in \"rmdesk test\" and retry.")
    lines = build_strip()
    page = pagemod.load(opened.data)
    page.append_to_rmdesk_layer(lines)
    return page.to_bytes(), len(lines)
