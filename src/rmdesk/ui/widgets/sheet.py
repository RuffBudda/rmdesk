"""An in-window modal sheet: a scrim over the whole window and a centred card with a question.

Used for the send confirmation while the tablet is paused. It answers exactly once through
`reply(bool)`: Yes/No buttons, Esc (No), the timeout (No), the window closing (No) or the
owning view being hidden (No).
"""
from __future__ import annotations

from typing import Callable

import shiboken6
from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget)

from rmdesk.ui.theme import TOKENS

CARD_WIDTH = 460
PAUSED_NOTE = "The tablet is paused until you answer."


class ConfirmSheet(QWidget):
    def __init__(self, window: QWidget, owner: QWidget | None, text: str,
                 reply: Callable[[bool], None], timeout: float, items: list[str] | None = None,
                 on_done: Callable[["ConfirmSheet"], None] | None = None):
        super().__init__(window)
        self.setObjectName("sheet")
        self._window, self._owner = window, owner
        self._reply, self._on_done = reply, on_done
        self._answered = False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.card = QFrame()
        self.card.setObjectName("sheetCard")
        self.card.setProperty("sheetCard", True)
        self.card.setFixedWidth(CARD_WIDTH)
        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(24, 22, 24, 20)
        lay.setSpacing(12)
        self.title = QLabel("Write to the tablet?")
        self.title.setObjectName("sheetTitle")
        self.body = QLabel(text)
        self.body.setWordWrap(True)
        self.items_label: QLabel | None = None
        lay.addWidget(self.title)
        lay.addWidget(self.body)
        if items:
            self.items_label = QLabel("\n".join("• " + i for i in items))
            self.items_label.setObjectName("sheetItems")
            self.items_label.setWordWrap(True)
            lay.addWidget(self.items_label)
        self.note = QLabel(PAUSED_NOTE)
        self.note.setObjectName("hint")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)
        row = QHBoxLayout()
        row.addStretch(1)
        self.no_btn = QPushButton("No")
        self.yes_btn = QPushButton("Yes")
        self.yes_btn.setProperty("variant", "primary")
        row.addWidget(self.no_btn)
        row.addWidget(self.yes_btn)
        lay.addSpacing(4)
        lay.addLayout(row)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addWidget(self.card, 0, Qt.AlignmentFlag.AlignCenter)
        self.no_btn.setDefault(True)
        self.no_btn.clicked.connect(lambda: self.answer(False))
        self.yes_btn.clicked.connect(lambda: self.answer(True))
        self.setTabOrder(self.no_btn, self.yes_btn)
        for b in (self.no_btn, self.yes_btn):
            b.installEventFilter(self)  # Tab stays inside the sheet: it is modal in effect
        window.installEventFilter(self)
        if owner is not None and owner is not window:
            owner.installEventFilter(self)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self.answer(False))
        self._timer.start(max(round(timeout * 1000), 1))
        self.setGeometry(window.rect())
        self.show()
        self.raise_()
        self.no_btn.setFocus()

    # -- answering ------------------------------------------------------------------
    def answer(self, yes: bool) -> None:
        if self._answered:
            return
        self._answered = True
        self._timer.stop()
        if shiboken6.isValid(self._window):
            self._window.removeEventFilter(self)
        if self._owner is not None and shiboken6.isValid(self._owner):
            self._owner.removeEventFilter(self)
        self.hide()
        try:
            self._reply(bool(yes))
        finally:
            if self._on_done is not None:
                self._on_done(self)
            self.deleteLater()

    def is_open(self) -> bool:
        return not self._answered

    # -- modal in effect ------------------------------------------------------------
    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 170))

    def mousePressEvent(self, e) -> None:
        e.accept()  # the scrim swallows clicks

    def mouseReleaseEvent(self, e) -> None:
        e.accept()

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.answer(False)
            e.accept()
        else:
            super().keyPressEvent(e)

    def eventFilter(self, obj, e) -> bool:
        t = e.type()
        if obj is self._window:
            if t == QEvent.Type.Resize:
                self.setGeometry(self._window.rect())
                self.raise_()
            elif t in (QEvent.Type.Close, QEvent.Type.Hide):
                self.answer(False)
            return False
        if obj is self._owner:
            if t in (QEvent.Type.Hide, QEvent.Type.Close):
                self.answer(False)
            return False
        if t == QEvent.Type.KeyPress and e.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            other = self.yes_btn if obj is self.no_btn else self.no_btn
            other.setFocus(Qt.FocusReason.TabFocusReason)
            return True
        return False
