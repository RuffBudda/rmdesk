"""Sharp text: words recognised with Tesseract and redrawn in line art (compose.sharptext)."""
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen

from rmdesk.compose import lineart, sharptext

needs_tesseract = pytest.mark.skipif(not sharptext.available(),
                                     reason="tesseract with the eng model is not installed")

LABELS = ["Receive request", "Check budget", "Approve", "Reject", "Notify manager",
          "Log ticket", "Assign owner", "Review draft", "Send invoice", "Archive file",
          "Update record", "Call client", "Sign contract", "Order parts", "Plan meeting"]


def flowchart(path: Path, w=1500, h=950, cols=5, rows=3, font_px=12):
    """A busy office process map: boxed labels font_px tall, arrows between them and a
    title. Returns [(label, QRectF box)] in source pixels."""
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    f = QFont("DejaVu Sans")
    f.setPixelSize(int(font_px * 1.8))
    f.setBold(True)
    p.setFont(f)
    p.setPen(QPen(QColor("black"), 2))
    p.drawText(QRectF(0, 10, w, font_px * 3), Qt.AlignmentFlag.AlignCenter, "Office Process Map")
    f.setPixelSize(font_px)
    f.setBold(False)
    p.setFont(f)
    out = []
    bw, bh = w / cols * 0.62, font_px * 3.2
    k = 0
    for r in range(rows):
        for c in range(cols):
            cx, cy = (c + 0.5) * w / cols, 120 + (r + 0.5) * (h - 140) / rows
            rect = QRectF(cx - bw / 2, cy - bh / 2, bw, bh)
            p.setPen(QPen(QColor("black"), 3))
            p.drawRect(rect)
            text = LABELS[k % len(LABELS)]
            k += 1
            p.setPen(QPen(QColor("black"), 1))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
            out.append((text, rect))
            p.setPen(QPen(QColor("black"), 2))
            if c < cols - 1:  # an arrow to the right
                x0, x1 = cx + bw / 2, cx + w / cols - bw / 2 - 2
                p.drawLine(QPointF(x0, cy), QPointF(x1, cy))
                p.drawLine(QPointF(x1, cy), QPointF(x1 - 12, cy - 6))
                p.drawLine(QPointF(x1, cy), QPointF(x1 - 12, cy + 6))
            if r < rows - 1 and c % 2 == 0:  # and one down
                y0, y1 = cy + bh / 2, cy + (h - 140) / rows - bh / 2 - 2
                p.drawLine(QPointF(cx, y0), QPointF(cx, y1))
                p.drawLine(QPointF(cx, y1), QPointF(cx - 6, y1 - 12))
                p.drawLine(QPointF(cx, y1), QPointF(cx + 6, y1 - 12))
    p.end()
    assert img.save(str(path))
    return out


BOX = (600, 380)  # the 1500 x 950 chart fitted small: 12 px labels become ~4.8 px


def _fit(w, h):
    k = min(BOX[0] / w, BOX[1] / h)
    tw, th = round(w * k), round(h * k)
    return tw / w, th / h, (BOX[0] - tw) / 2, (BOX[1] - th) / 2


def _word_rects(words, src_w=1500, src_h=950):
    kx, ky, ox, oy = _fit(src_w, src_h)
    return [(w.text, ox + w.x * kx, oy + w.y * ky, ox + (w.x + w.w) * kx,
             oy + (w.y + w.h) * ky) for w in words]


def _length(st):
    a = np.array([(x, y) for x, y, *_ in st])
    return float(np.hypot(*np.diff(a, axis=0).T).sum()) if len(a) > 1 else 0.0


def _dist_to_strokes(pts, strokes):
    """Distance from each point to the nearest segment of `strokes`."""
    segs = np.array([(a[0], a[1], b[0], b[1]) for st in strokes for a, b in zip(st, st[1:])])
    best = np.full(len(pts), np.inf)
    for x0, y0, x1, y1 in segs:
        d = np.array([x1 - x0, y1 - y0])
        n = float(d @ d) or 1e-9
        t = np.clip(((pts[:, 0] - x0) * d[0] + (pts[:, 1] - y0) * d[1]) / n, 0, 1)
        best = np.minimum(best, np.hypot(pts[:, 0] - (x0 + t * d[0]),
                                         pts[:, 1] - (y0 + t * d[1])))
    return best


@pytest.fixture
def chart(qapp, tmp_path):
    sharptext.clear_cache()
    path = tmp_path / "chart.png"
    return path, flowchart(path)


@needs_tesseract
def test_recognises_most_labels(chart):
    path, labels = chart
    found = [w.text for w in sharptext.recognise(path)]
    want = [t for text, _ in labels for t in text.split()]
    hit = sum(1 for t in want if t in found)
    assert hit >= 0.8 * len(want), (found, want)
    assert "Process" in found  # the title too


@needs_tesseract
def test_redrawn_words_stay_in_their_boxes_and_borders_stay_whole(chart):
    path, labels = chart
    res = lineart.lineart_image(path, *BOX, sharp_text=True)
    assert res.strokes and not res.capped
    rects = _word_rects(sharptext.recognise(path))
    assert len(rects) >= 30
    tol = 0.01
    for text, x0, y0, x1, y1 in rects:
        inside = [st for st in res.strokes
                  if any(x0 <= x <= x1 and y0 <= y <= y1 for x, y, *_ in st)]
        assert inside, text  # the word is drawn
        for st in inside:  # ... entirely inside its box, with a pen finer than the lines
            assert all(x0 - tol <= x <= x1 + tol and y0 - tol <= y <= y1 + tol
                       for x, y, *_ in st), text
            assert st[0][2] < 16.0
    # every label's frame is still drawn whole, as long strokes at the pen's width
    kx, ky, ox, oy = _fit(1500, 950)
    long = [st for st in res.strokes if _length(st) >= 20 and st[0][2] == 16.0]
    for _text, r in labels:
        bx0, by0 = ox + r.left() * kx, oy + r.top() * ky
        bx1, by1 = ox + r.right() * kx, oy + r.bottom() * ky
        t = np.linspace(0, 1, 40)[1:-1]
        edge = np.vstack([np.c_[bx0 + t * (bx1 - bx0), np.full_like(t, by0)],
                          np.c_[bx0 + t * (bx1 - bx0), np.full_like(t, by1)],
                          np.c_[np.full_like(t, bx0), by0 + t * (by1 - by0)],
                          np.c_[np.full_like(t, bx1), by0 + t * (by1 - by0)]])
        assert _dist_to_strokes(edge, long).max() <= 1.5


def test_off_never_reads_text_and_matches_the_default(chart, monkeypatch):
    path, _ = chart

    def boom(*_a, **_k):
        raise AssertionError("OCR with sharp text off")
    plain = lineart.lineart_image(path, *BOX)
    monkeypatch.setattr(sharptext, "recognise", boom)
    off = lineart.lineart_image(path, *BOX, sharp_text=False)
    assert off == plain and off.strokes


_TSV = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight"
        "\tconf\ttext\n"
        "1\t1\t0\t0\t0\t0\t0\t0\t900\t600\t-1\t\n"
        "5\t1\t1\t1\t1\t1\t300\t300\t150\t36\t95.0\tApprove\n"
        "5\t1\t1\t1\t1\t2\t600\t300\t9\t60\t40.0\t|\n"
        "5\t1\t1\t1\t1\t3\t700\t300\t12\t30\t70.0\tY\n")


def test_cache_runs_ocr_once_per_picture(chart, monkeypatch):
    """Refitting, moving or rotating the box reuses the words: one Tesseract run."""
    path, _ = chart
    calls = []

    def fake_run(args, **kw):
        if "tsv" in args:
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, _TSV.encode(), b"")
        raise AssertionError(args)
    monkeypatch.setattr(sharptext, "status", lambda: (True, ""))
    monkeypatch.setattr(sharptext, "_exe", lambda: "/usr/bin/tesseract")
    monkeypatch.setattr(sharptext.subprocess, "run", fake_run)
    sharptext.clear_cache()
    a = lineart.lineart_image(path, *BOX, sharp_text=True)
    b = lineart.lineart_image(path, 500, 320, sharp_text=True, box_x=40, box_y=90,
                              rotation=20)
    assert a.strokes and b.strokes
    assert len(calls) == 1
    k = sharptext.ocr_scale(1500, 950)
    assert k == 3 and calls[0][calls[0].index("--psm") + 1] == "11"
    assert calls[0][calls[0].index("--dpi") + 1] == str(sharptext.OCR_DPI * k)
    words = sharptext.recognise(path)  # the box edge "|" and the unsure "Y" are dropped
    assert [w.text for w in words] == ["Approve"]
    assert words[0].x == pytest.approx(100) and words[0].h == pytest.approx(12)
    sharptext.recognise(path, invert=True)  # another picture as far as OCR goes
    assert len(calls) == 2


def test_plausible_words():
    W = sharptext.Word
    assert sharptext.plausible(W("Approve", 80, 0, 0, 40, 10))
    assert sharptext.plausible(W("7", 95, 0, 0, 6, 10))
    assert not sharptext.plausible(W("Approve", 50, 0, 0, 40, 10))  # unsure
    assert not sharptext.plausible(W("Y", 70, 0, 0, 6, 10))  # a lone symbol, not sure
    assert not sharptext.plausible(W("—>", 99, 0, 0, 20, 10))  # an arrow
    assert not sharptext.plausible(W("|Log", 90, 0, 0, 30, 10))  # a box edge in it
    assert not sharptext.plausible(W("ll", 90, 0, 0, 2, 40))  # a vertical line


def test_word_lines_fill_the_box():
    lines = sharptext.word_lines("Approve", 10, 20, 70, 28, 1.0)
    pts = np.vstack(lines)
    x0, y0 = pts.min(axis=0)
    x1, y1 = pts.max(axis=0)
    assert 10 <= x0 and x1 <= 70 and 20 <= y0 and y1 <= 28
    assert y1 - y0 == pytest.approx(7.0, abs=0.01)  # the box height less the pen
    # 7 px tall "Approve" is ~29.5 px wide: spaced out by SPACING_MAX_EM at most, centred
    assert x1 - x0 == pytest.approx(29.5 + 6 * sharptext.SPACING_MAX_EM * 5.25, abs=0.2)
    assert (x0 + x1) / 2 == pytest.approx(40.0, abs=0.01)
    pts = np.vstack(sharptext.word_lines("Approve", 0, 0, 34, 8, 1.0))  # ...or to the width
    assert np.ptp(pts[:, 0]) == pytest.approx(33.0, abs=0.01)
    # a word wider than its box is squeezed into it
    pts = np.vstack(sharptext.word_lines("Approve", 0, 0, 20, 8, 1.0))
    assert pts[:, 0].min() >= 0 and pts[:, 0].max() <= 20


def test_erase_keeps_lines_through_the_word():
    ink = np.zeros((40, 120), bool)
    ink[10:12, :] = True  # a long line just above the word
    ink[14:24, 30:32] = True  # glyph strokes
    ink[14:24, 40:42] = True
    ink[18:20, 30:42] = True
    sharptext.erase_words(ink, [(28, 13, 44, 25)], 1.0)
    assert ink[10:12, :].all()  # the line stays whole
    assert not ink[13:26, :].any()  # the glyphs are gone


def test_status_without_eng(monkeypatch):
    sharptext.status.cache_clear()
    try:
        monkeypatch.setattr(sharptext, "_exe", lambda: "/usr/bin/tesseract")
        monkeypatch.setattr(sharptext.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
            a, 0, b'List of available languages in "/usr/share/tessdata/" (1):\nosd\n', b""))
        assert sharptext.status() == (False, sharptext.UNAVAILABLE_NO_ENG)
        sharptext.status.cache_clear()
        monkeypatch.setattr(sharptext, "_exe", lambda: None)
        assert sharptext.status() == (False, sharptext.UNAVAILABLE_NO_TESSERACT)
    finally:
        sharptext.status.cache_clear()
