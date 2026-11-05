"""Card painter for the Files grid: thumbnail on paper, title, type tag + pages, selection ring."""
from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

from rmdesk.ui.theme import TOKENS, icon

ID_ROLE = Qt.ItemDataRole.UserRole
KIND_ROLE = Qt.ItemDataRole.UserRole + 1
PAGES_ROLE = Qt.ItemDataRole.UserRole + 2
THUMB_ROLE = Qt.ItemDataRole.UserRole + 3
TILE = "tile"
CARD_SIZE = QSize(164, 214)  # grid cell: 150 x 200 card + 14 gap
_TAGS = {"notebook": "NOTEBOOK", "pdf": "PDF", "epub": "EPUB", "folder": "FOLDER"}
_ICONS = {"notebook": "notebook", "pdf": "file-pdf", "epub": "file-epub", "folder": "folder"}


def _c(name: str) -> QColor:
    return QColor(TOKENS[name])


class DocDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index) -> QSize:
        return CARD_SIZE

    def paint(self, p: QPainter, option, index) -> None:
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        card = QRectF(option.rect).adjusted(7, 7, -7, -7)
        kind = index.data(KIND_ROLE)
        if kind == TILE:
            self._paint_tile(p, card, bool(option.state & QStyle.StateFlag.State_MouseOver))
        else:
            self._paint_card(p, option, index, card, kind)
        p.restore()

    def _paint_tile(self, p: QPainter, card: QRectF, hover: bool) -> None:
        r = card.adjusted(1, 1, -1, -1)
        p.setBrush(QBrush(_c("surface-2") if hover else _c("surface-1")))
        pen = QPen(_c("accent") if hover else _c("line"), 1.5, Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawRoundedRect(r, 12, 12)
        ic = icon("upload", 28, TOKENS["accent-text"]).pixmap(28, 28)
        p.drawPixmap(int(r.center().x() - 14), int(r.center().y() - 44), ic)
        f = QFont(p.font())
        f.setPixelSize(13)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.setPen(_c("text"))
        text = QRectF(r.left() + 10, r.center().y() - 4, r.width() - 20, 48)
        p.drawText(text, int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
                   | int(Qt.TextFlag.TextWordWrap), "Drop PDFs or EPUBs here")

    def _paint_card(self, p: QPainter, option, index, card: QRectF, kind: str) -> None:
        folder = kind == "folder"
        selected = bool(option.state & QStyle.StateFlag.State_Selected) and not folder  # folders aren't actionable
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(_c("surface-2") if hover and not selected else _c("surface-1")))
        p.drawRoundedRect(card, 12, 12)
        if selected:
            ring = card.adjusted(1, 1, -1, -1)  # 2 px ring drawn inside the card edge
            p.setPen(QPen(_c("accent"), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(ring, 11, 11)
            p.setPen(Qt.PenStyle.NoPen)
        # thumbnail
        thumb = QRectF(card.left() + 10, card.top() + 10, card.width() - 20, 128)
        path = QPainterPath()
        path.addRoundedRect(thumb, 6, 6)
        pm = index.data(THUMB_ROLE)
        p.fillPath(path, QBrush(_c("surface-3") if folder else _c("paper")))
        if isinstance(pm, QPixmap) and not pm.isNull():
            p.save()
            p.setClipPath(path)
            scaled = pm.scaled(thumb.size().toSize(), Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
            dpr = scaled.devicePixelRatio() or 1.0
            w, h = scaled.width() / dpr, scaled.height() / dpr
            p.drawPixmap(QRectF(thumb.center().x() - w / 2, thumb.center().y() - h / 2, w, h),
                         scaled, QRectF(scaled.rect()))
            p.restore()
        else:
            color = TOKENS["text-2"] if folder else "#8A8880"
            ic = icon(_ICONS.get(kind, "file-pdf"), 20, color).pixmap(20, 20)
            p.drawPixmap(int(thumb.center().x() - 10), int(thumb.center().y() - 10), ic)
        # title
        f = QFont(p.font())
        f.setPixelSize(13)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.setPen(_c("text"))
        title_r = QRectF(card.left() + 10, thumb.bottom() + 8, card.width() - 20, 18)
        title = p.fontMetrics().elidedText(str(index.data(Qt.ItemDataRole.DisplayRole) or ""),
                                           Qt.TextElideMode.ElideRight, int(title_r.width()))
        p.drawText(title_r, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), title)
        # type tag + pages, in text-3
        f.setPixelSize(11)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.setPen(_c("text-3"))
        meta_r = QRectF(card.left() + 10, title_r.bottom() + 2, card.width() - 20, 16)
        tag = _TAGS.get(kind, "")
        p.drawText(meta_r, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), tag)
        pages = index.data(PAGES_ROLE)
        if pages:
            tag_w = p.fontMetrics().horizontalAdvance(tag)
            f.setWeight(QFont.Weight.Normal)
            p.setFont(f)
            p.drawText(meta_r.adjusted(tag_w + 8, 0, 0, 0),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                       f"{pages} page{'s' if pages != 1 else ''}")
        if selected:  # tick badge, top-right of the thumbnail
            badge = QRectF(thumb.right() - 24, thumb.top() + 6, 18, 18)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(_c("accent")))
            p.drawEllipse(badge)
            tick = icon("check", 12, "#FFFFFF").pixmap(12, 12)
            p.drawPixmap(int(badge.center().x() - 6), int(badge.center().y() - 6), tick)
