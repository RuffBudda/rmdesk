"""A group of related controls on a subtle pill (surface-2, radius 10, 4 px padding), so a
toolbar reads as a few clusters; a FlowLayout wraps the clusters as units."""
from __future__ import annotations

from PySide6.QtWidgets import QFrame, QHBoxLayout, QWidget

PAD = 4  # around the controls
GAP = 8  # between the controls in a cluster
BETWEEN = 16  # between clusters (the toolbars' FlowLayout spacing)
HEIGHT = 36 + 2 * PAD  # every control in a cluster is 36 px tall


class Cluster(QFrame):
    def __init__(self, *widgets: QWidget, name: str = "", parent=None):
        super().__init__(parent)
        self.setProperty("cluster", True)
        if name:
            self.setAccessibleName(name)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(PAD, PAD, PAD, PAD)
        lay.setSpacing(GAP)
        self.setMinimumHeight(HEIGHT)
        for w in widgets:
            lay.addWidget(w)

    def add(self, w: QWidget) -> None:
        self.layout().addWidget(w)
