from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QAbstractButton

from rmdesk.ui.theme import TOKENS


class ToggleSwitch(QAbstractButton):
    """36x20 switch; accent track when on.

    Callers must setAccessibleName (the switch has no text); the same goes for icon-only buttons.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(36, 20)

    def sizeHint(self) -> QSize:
        return QSize(36, 20)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        on = self.isChecked()
        track = QColor(TOKENS["accent"] if on else TOKENS["surface-3"])
        if not self.isEnabled():
            track.setAlpha(110)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, 0, 36, 20), 10, 10)
        p.setBrush(QColor("#FFFFFF" if on else TOKENS["text-2"]))
        p.drawEllipse(QRectF(18 if on else 2, 2, 16, 16))
        if self.hasFocus():
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QColor(TOKENS["accent-text"]))
            p.drawRoundedRect(QRectF(0.5, 0.5, 35, 19), 9.5, 9.5)
