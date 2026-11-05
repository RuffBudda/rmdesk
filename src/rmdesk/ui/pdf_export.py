"""Write rendered notebook pages (SVG) to a vector PDF with Qt."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QMarginsF, QRectF, QSizeF
from PySide6.QtGui import QPageSize, QPainter, QPdfWriter
from PySide6.QtSvg import QSvgRenderer

DPI = 226
PAGE_W_PX, PAGE_H_PX = 1404, 1872
_MM_PER_INCH = 25.4


def write_pdf(svgs: list[bytes], dest: Path) -> None:
    """One PDF page per SVG, each page 1404x1872 px at 226 dpi."""
    writer = QPdfWriter(str(dest))
    writer.setResolution(DPI)
    size_mm = QSizeF(PAGE_W_PX / DPI * _MM_PER_INCH, PAGE_H_PX / DPI * _MM_PER_INCH)
    writer.setPageSize(QPageSize(size_mm, QPageSize.Unit.Millimeter))
    writer.setPageMargins(QMarginsF(0, 0, 0, 0))
    painter = QPainter(writer)
    try:
        for i, svg in enumerate(svgs):
            if i:
                writer.newPage()
            renderer = QSvgRenderer(svg)
            renderer.render(painter, QRectF(0, 0, writer.width(), writer.height()))
    finally:
        painter.end()
