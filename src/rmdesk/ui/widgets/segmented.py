from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QPushButton


class Segmented(QFrame):
    """Rounded group of exclusive checkable buttons; emits the chosen index."""
    changed = Signal(int)

    def __init__(self, labels: list[str], parent=None):
        super().__init__(parent)
        self.setProperty("segmented", True)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self.buttons: list[QPushButton] = []
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        for i, text in enumerate(labels):
            b = QPushButton(text)
            b.setCheckable(True)
            self._group.addButton(b, i)
            lay.addWidget(b)
            self.buttons.append(b)
        if self.buttons:
            self.buttons[0].setChecked(True)
        self._group.idClicked.connect(self.changed)

    def index(self) -> int:
        return self._group.checkedId()

    def set_index(self, i: int) -> None:
        self.buttons[i].setChecked(True)
