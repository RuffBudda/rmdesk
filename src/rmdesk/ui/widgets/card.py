from __future__ import annotations

from PySide6.QtWidgets import QFrame


class Card(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("card", True)
