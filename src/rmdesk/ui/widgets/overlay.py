"""Keep a small overlay widget pinned to a corner of another widget (status chips over a picture)."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint
from PySide6.QtWidgets import QWidget

MARGIN = 12


class OverlayPin(QObject):
    """`overlay` (a child of `parent`) follows the top-left corner of `host`, which may change."""

    def __init__(self, overlay: QWidget, parent: QWidget, corner: str = "tl",
                 margin: int = MARGIN):
        super().__init__(overlay)
        self.overlay, self.parent_widget = overlay, parent
        self.host: QWidget | None = None
        self.corner = corner
        self.margin = margin
        self.extra_top = 0  # pushes the overlay down (something else sits in the corner)

    def set_host(self, host: QWidget) -> None:
        if self.host is not None:
            self.host.removeEventFilter(self)
        self.host = host
        host.installEventFilter(self)
        self.update()

    def eventFilter(self, obj, e) -> bool:
        if obj is self.host and e.type() in (QEvent.Type.Resize, QEvent.Type.Move,
                                             QEvent.Type.Show, QEvent.Type.LayoutRequest):
            self.update()
        return False

    def update(self) -> None:
        host, ov = self.host, self.overlay
        if host is None:
            return
        m = self.margin
        avail = max(host.width() - 2 * m, 80)
        lay = ov.layout()
        want = lay.totalSizeHint().width() if lay is not None else ov.sizeHint().width()
        w = min(max(want, 1), avail)
        ov.setFixedWidth(w)
        if lay is not None:
            h = (lay.totalHeightForWidth(w) if lay.hasHeightForWidth()
                 else lay.totalSizeHint().height())
            ov.setFixedHeight(max(h, 1))
        else:
            ov.adjustSize()
        x = m if self.corner == "tl" else host.width() - w - m
        ov.move(host.mapTo(self.parent_widget, QPoint(max(x, 0), m + self.extra_top)))
        ov.raise_()
