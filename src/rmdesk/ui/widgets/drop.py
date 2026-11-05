"""The outline shown over the picture while files are dragged onto the Screen view: a red
rounded frame and a label saying what the drop will do."""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from rmdesk.ui.theme import TOKENS

LINE = 2  # px
RADIUS = 10
PAD_X, PAD_Y = 14, 8  # inside the label pill


class DropOutline(QWidget):
    """Placed over the picture by its owner (set_area); never takes the mouse."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("dropOutline")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._text = ""
        self._refused = False
        self.hide()

    def text(self) -> str:
        return self._text

    def refused(self) -> bool:
        return self._refused

    def show_for(self, area, text: str, refused: bool = False) -> None:
        """Cover `area` (a QRect in the parent's coordinates) with the outline and `text`."""
        self._text, self._refused = text, bool(refused)
        self.setGeometry(area)
        self.show()
        self.raise_()
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(LINE, LINE, -LINE, -LINE)
        color = QColor(TOKENS["text-3"] if self._refused else TOKENS["accent"])
        tint = QColor(color)
        tint.setAlpha(28)
        p.setBrush(tint)
        pen = QPen(color, LINE)
        if self._refused:
            pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawRoundedRect(r, RADIUS, RADIUS)
        if not self._text:
            return
        f = QFont(self.font())
        f.setPixelSize(14)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        fm = p.fontMetrics()
        avail = max(int(r.width()) - 2 * PAD_X - 24, 40)
        text = fm.elidedText(self._text, Qt.TextElideMode.ElideRight, avail)
        w = fm.horizontalAdvance(text) + 2 * PAD_X
        h = fm.height() + 2 * PAD_Y
        box = QRectF(r.center().x() - w / 2, r.center().y() - h / 2, w, h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(TOKENS["surface-3"] if self._refused else TOKENS["accent"]))
        p.drawRoundedRect(box, h / 2, h / 2)
        p.setPen(QColor("#FFFFFF" if not self._refused else TOKENS["text"]))
        p.drawText(box, int(Qt.AlignmentFlag.AlignCenter), text)
