"""The tablet's battery as a small icon + percentage (the Screen header, and over the picture
in Expand)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from rmdesk.ui.theme import TOKENS, icon
from rmdesk.ui.widgets import repolish


def battery_hint(b) -> str:
    """"Tablet battery: 56%, discharging" ("" when unknown)."""
    return f"Tablet battery: {b.percent}%, {b.status.lower()}" if b is not None else ""


class BatteryBadge(QWidget):
    """Icon + "56%" ("56% ⚡" charging; the warn colour when low and discharging). Hidden while
    the battery is unknown. `overlay` is the small translucent variant over the picture."""

    def __init__(self, overlay: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("batteryOverlay" if overlay else "batteryBadge")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setProperty("low", "false")
        self._size = 12 if overlay else 16
        self.icon = QLabel()
        self.icon.setFixedSize(self._size, self._size)
        self.text = QLabel()
        self.text.setObjectName("batteryOverlayText" if overlay else "batteryBadgeText")
        for w in (self, self.icon, self.text):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, overlay)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(*((6, 2, 7, 2) if overlay else (6, 0, 4, 0)))
        lay.setSpacing(4 if overlay else 6)
        lay.addWidget(self.icon)
        lay.addWidget(self.text)
        if not overlay:
            self.setFixedHeight(36)
        self.battery = None
        self.set_battery(None)

    def low(self) -> bool:
        return self.property("low") == "true"

    def set_battery(self, b) -> None:
        self.battery = b
        if b is None:
            self.text.setText("")
            self.setAccessibleName("")
            self.hide()
            return
        low = bool(b.low)
        color = TOKENS["warn"] if low else TOKENS["text-2"]
        self.icon.setPixmap(icon("battery", self._size, color).pixmap(self._size, self._size))
        self.text.setText(b.short())
        self.setAccessibleName(battery_hint(b))
        self.setToolTip(battery_hint(b))
        if self.property("low") != ("true" if low else "false"):
            self.setProperty("low", "true" if low else "false")
            repolish(self.text)
            repolish(self)
        self.show()
