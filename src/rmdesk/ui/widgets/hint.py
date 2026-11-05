"""An in-window hint line: hovering or keyboard-focusing a watched widget shows its short
description in a small label inside the window, just under the widget.

Qt tooltips are separate popup windows, which a tiling compositor (Hyprland) may not show; the
widgets keep their tooltips, and this label is drawn by the window itself. One event filter
serves every watched widget."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtWidgets import QLabel, QWidget

GAP = 6  # between the widget and the hint under it
MARGIN = 8  # the hint stays this far inside the host


class HoverHint(QObject):
    """`host` owns the label (a child of it, floating above its other children)."""

    def __init__(self, host: QWidget):
        super().__init__(host)
        self.host = host
        self.label = QLabel(host)
        self.label.setObjectName("hoverHint")
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.label.hide()
        self._texts: dict[int, tuple[QWidget, str | Callable[[], str]]] = {}
        self._hovered: QWidget | None = None
        self._focused: QWidget | None = None
        self.target: QWidget | None = None  # whose hint shows now

    def watch(self, widget: QWidget, text: str | Callable[[], str]) -> None:
        """Show `text` (or what the callable returns at the time) for `widget`."""
        if id(widget) not in self._texts:
            widget.installEventFilter(self)
            widget.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self._texts[id(widget)] = (widget, text)
        if widget.accessibleDescription() == "" and isinstance(text, str):
            widget.setAccessibleDescription(text)
        if self.target is widget:
            self.refresh()

    def text_for(self, widget: QWidget) -> str:
        entry = self._texts.get(id(widget))
        if entry is None:
            return ""
        t = entry[1]
        return t() if callable(t) else t

    def text(self) -> str:
        """The hint showing now ("" when none)."""
        return self.label.text() if self.label.isVisible() else ""

    def eventFilter(self, obj, e) -> bool:
        t = e.type()
        if t == QEvent.Type.ToolTip:
            return True  # the in-window hint already says it: no second, popup tooltip
        if t in (QEvent.Type.Enter, QEvent.Type.HoverEnter):
            self._hovered = obj
            self._show(obj)
        elif t in (QEvent.Type.Leave, QEvent.Type.HoverLeave):
            if self._hovered is obj:
                self._hovered = None
            self._settle()
        elif t == QEvent.Type.FocusIn:
            self._focused = obj
            if self._hovered is None:
                self._show(obj)
        elif t == QEvent.Type.FocusOut:
            if self._focused is obj:
                self._focused = None
            self._settle()
        elif t == QEvent.Type.Hide:
            if self._hovered is obj:
                self._hovered = None
            if self._focused is obj:
                self._focused = None
            self._settle()
        return False

    def _settle(self) -> None:
        """After a leave: the hovered widget's hint, else the focused one's, else none. A
        widget the pointer just left clears the hint even while it still has the focus."""
        if self._hovered is not None:
            self._show(self._hovered)
        else:
            self.clear()

    def clear(self) -> None:
        self.target = None
        self.label.hide()

    def refresh(self) -> None:
        """Re-read the shown widget's text (its state changed while it was hovered)."""
        if self.target is not None:
            self._show(self.target)

    def _show(self, w: QWidget) -> None:
        text = self.text_for(w)
        if not text or not w.isVisible():
            self.clear()
            return
        self.target = w
        lab = self.label
        lab.setText(text)
        lab.adjustSize()
        host = self.host
        below = w.mapTo(host, QPoint(0, w.height() + GAP))
        x = min(below.x(), host.width() - lab.width() - MARGIN)
        lab.move(max(x, MARGIN), below.y())
        lab.show()
        lab.raise_()
