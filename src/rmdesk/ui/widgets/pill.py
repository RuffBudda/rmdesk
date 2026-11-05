from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QLabel, QStyle, QStyleOption

from rmdesk.ui.widgets import repolish

TEXT = {"usb": "USB", "wifi": "Wi-Fi", "offline": "Offline", "connecting": "Connecting…"}
SUB = {"usb": "reMarkable 2", "wifi": "reMarkable 2", "offline": "Plug in or check Wi-Fi",
       "connecting": "Looking for the tablet…"}
ACTION = "open device settings"
HEIGHT = 32  # level with the icon buttons in a header cluster


class DevicePill(QAbstractButton):
    """The connection state as a compact button (dot + "Wi-Fi" / "USB" / "Offline") in the
    Screen header; a click opens the Device page. The details ("fw 3.29.1.0 · 56%") are in
    the tooltip. `set_current(True)` marks the Device page as the one shown."""

    def __init__(self, state: str = "offline", parent=None):
        super().__init__(parent)
        self.setObjectName("devicePill")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)  # keyboard-focusable; a click leaves no ring
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setProperty("current", "false")
        self.dot = QLabel()
        self.dot.setFixedSize(8, 8)
        self.label = QLabel()
        self.label.setObjectName("pillState")
        for w in (self.dot, self.label):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(11, 0, 12, 0)
        lay.setSpacing(8)
        lay.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.label, 0, Qt.AlignmentFlag.AlignVCenter)
        self._lay = lay
        self.setFixedHeight(HEIGHT)
        self._sub = ""
        self._low = False
        self.set_state(state)

    def _tip(self) -> None:
        text = " · ".join(x for x in (self.label.text(), self._sub) if x)
        self.setToolTip(f"{text}\nConnection and settings (Ctrl+3)")
        self.setAccessibleName(f"Connection: {self.label.text()} — {ACTION}")
        self.setAccessibleDescription(self._sub)

    def state(self) -> str:
        return self.property("state")

    def text_label(self) -> str:
        return self.label.text()

    def sub_text(self) -> str:
        """The details as plain text ("fw 3.29.1.0 · 56%"), shown in the tooltip."""
        return self._sub

    def battery_low(self) -> bool:
        return self._low

    def set_state(self, state: str, text: str | None = None, sub: str | None = None,
                  battery: str | None = None, battery_low: bool = False) -> None:
        """`battery` ("56%", "56% ⚡") joins the details; `battery_low` marks it low."""
        self.setProperty("state", state)
        self.dot.setProperty("dot", state)
        self.label.setText(text if text is not None else TEXT.get(state, state))
        base = sub if sub is not None else SUB.get(state, "")
        self._sub = " · ".join(x for x in (base, battery) if x)
        self._low = bool(battery and battery_low)
        repolish(self.dot)
        repolish(self)
        self._tip()
        self.updateGeometry()

    def set_current(self, on: bool) -> None:
        v = "true" if on else "false"
        if self.property("current") != v:
            self.setProperty("current", v)
            repolish(self)

    def is_current(self) -> bool:
        return self.property("current") == "true"

    def keyPressEvent(self, e) -> None:
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not e.isAutoRepeat():
            self.animateClick()
            e.accept()
            return
        super().keyPressEvent(e)  # Space: QAbstractButton clicks on release

    def sizeHint(self):
        s = self._lay.totalSizeHint()
        s.setHeight(HEIGHT)
        return s

    def minimumSizeHint(self):
        return self.sizeHint()

    def paintEvent(self, _e) -> None:
        from PySide6.QtGui import QPainter
        p = QPainter(self)
        opt = QStyleOption()
        opt.initFrom(self)
        self.style().drawPrimitive(QStyle.PrimitiveElement.PE_Widget, opt, p, self)  # QSS box
