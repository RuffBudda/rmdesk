from __future__ import annotations

from PySide6.QtWidgets import QLabel

from rmdesk.ui.widgets import repolish

STATES = ("ok", "warn", "err", "neutral")


class Chip(QLabel):
    def __init__(self, state: str = "neutral", text: str = "", parent=None):
        super().__init__(text, parent)
        self.setProperty("chip", state)
        self.setFixedHeight(24)

    def state(self) -> str:
        return self.property("chip")

    def set_state(self, state: str, text: str | None = None) -> None:
        self.setProperty("chip", state)
        if text is not None:
            self.setText(text)
        repolish(self)
