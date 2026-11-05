from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from rmdesk.ui.theme import TOKENS, icon
from rmdesk.ui.widgets import repolish

_ICON = {"info": "info", "warn": "alert", "err": "alert", "ok": "check"}
_COLOR = {"info": "text-2", "warn": "warn", "err": "accent-text", "ok": "ok"}


class Banner(QFrame):
    action = Signal()

    def __init__(self, kind: str = "info", text: str = "", action_text: str | None = None, parent=None):
        super().__init__(parent)
        self.icon_label = QLabel()
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.button = QPushButton(action_text or "")
        self.button.setProperty("dense", True)
        self.button.clicked.connect(self.action)
        self.button.setVisible(bool(action_text))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(10)
        lay.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.label, 1)
        lay.addWidget(self.button)
        self.set_kind(kind)

    def kind(self) -> str:
        return self.property("banner")

    def set_kind(self, kind: str) -> None:
        self.setProperty("banner", kind)
        self.icon_label.setPixmap(icon(_ICON.get(kind, "info"), 16, TOKENS[_COLOR.get(kind, "text-2")]).pixmap(QSize(16, 16)))
        repolish(self)

    def set_text(self, text: str) -> None:
        self.label.setText(text)

    def text(self) -> str:
        return self.label.text()
