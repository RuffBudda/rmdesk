from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from rmdesk.ui.theme import TOKENS, icon


class EmptyState(QWidget):
    """Icon + one plain sentence + optional action."""
    action = Signal()

    def __init__(self, icon_name: str, sentence: str, action_text: str | None = None, parent=None):
        super().__init__(parent)
        self.icon_label = QLabel()
        self.icon_label.setPixmap(icon(icon_name, 32, TOKENS["text-3"]).pixmap(32, 32))
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label = QLabel(sentence)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setWordWrap(True)
        self.label.setProperty("role", "hint")
        self.button = QPushButton(action_text or "")
        self.button.setVisible(bool(action_text))
        self.button.clicked.connect(self.action)
        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        lay.addStretch(1)
        lay.addWidget(self.icon_label)
        lay.addWidget(self.label)
        lay.addWidget(self.button, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addStretch(1)
