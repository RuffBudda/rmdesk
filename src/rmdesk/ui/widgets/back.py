"""The "← Screen" button at the left of the Files and Device headers."""
from __future__ import annotations

from PySide6.QtWidgets import QPushButton

from rmdesk.ui.theme import TOKENS, icon

LABEL = "Screen"


def back_button() -> QPushButton:
    """Back to the Screen view (the app's home): a chevron and "Screen"."""
    b = QPushButton(LABEL)
    b.setIcon(icon("back", 16, TOKENS["text-2"]))
    b.setObjectName("backButton")
    b.setProperty("variant", "ghost")
    b.setProperty("dense", True)
    b.setToolTip("Back to the Screen view (Ctrl+2)")
    b.setAccessibleName("Back to Screen")
    return b
