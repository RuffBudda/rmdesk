"""A layout that places its items left to right and wraps to new rows when the width runs out.

`spacing` is the gap between items in a row, `vspacing` (default: the same) the gap between
rows. Items added with add_right() go to the right end of the row they land in."""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QWidgetItem


class FlowLayout(QLayout):
    def __init__(self, parent=None, spacing: int = 8, vspacing: int | None = None):
        super().__init__(parent)
        self._items: list[QWidgetItem] = []
        self._right: set[int] = set()  # id() of the items pushed to the row's right end
        self._space = spacing
        self._vspace = spacing if vspacing is None else vspacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:
        self._items.append(item)

    def add_right(self, widget) -> None:
        """Add `widget` at the right end of its row (after the left-aligned items)."""
        self.addWidget(widget)
        self._right.add(id(self._items[-1]))

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i: int):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i: int):
        if not 0 <= i < len(self._items):
            return None
        it = self._items.pop(i)
        self._right.discard(id(it))
        return it

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._layout(QRect(0, 0, width, 0), dry=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._layout(rect, dry=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for it in self._items:
            if it.isEmpty():
                continue
            size = size.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _layout(self, rect: QRect, dry: bool) -> int:
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, row_h = area.x(), area.y(), 0
        row: list[tuple[QWidgetItem, QPoint, QSize]] = []

        def flush() -> None:
            if dry or not row:
                return
            # the trailing right-aligned items move to the row's right end
            k = len(row)
            while k > 0 and id(row[k - 1][0]) in self._right:
                k -= 1
            end = row[-1][1].x() + row[-1][2].width()
            shift = max(area.right() + 1 - end, 0) if k < len(row) else 0
            for i, (it, pos, sz) in enumerate(row):  # centre every item vertically in its row
                dx = shift if i >= k else 0
                it.setGeometry(QRect(QPoint(pos.x() + dx, pos.y() + (row_h - sz.height()) // 2),
                                     sz))

        for it in self._items:
            if it.isEmpty():
                continue
            sz = it.sizeHint().expandedTo(it.minimumSize())
            sz = sz.boundedTo(QSize(max(area.width(), 1), 16777215))
            if x + sz.width() > area.right() + 1 and row_h > 0:
                flush()
                row = []
                x, y, row_h = area.x(), y + row_h + self._vspace, 0
            row.append((it, QPoint(x, y), sz))
            x += sz.width() + self._space
            row_h = max(row_h, sz.height())
        flush()
        return y + row_h - area.y() + m.top() + m.bottom()

    def spacing(self) -> int:
        return self._space
