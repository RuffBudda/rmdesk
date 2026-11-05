"""Ask the send's confirm question on the GUI thread from the worker that holds xochitl stopped.

The worker calls `ConfirmBridge.confirm(text)`; a queued signal shows the question on the GUI
thread and the worker waits on a threading.Event. No answer within `timeout` seconds (60 by
default) counts as No, and the dialog is closed. The result is always an explicit bool.
`cancel()` (window closing) answers a pending question No at once and closes its dialog.
"""
from __future__ import annotations

import threading
from typing import Callable

import shiboken6
from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWidgets import QWidget

from rmdesk.ui.widgets.sheet import ConfirmSheet

CONFIRM_TIMEOUT = 60.0  # seconds; no answer = No

Reply = Callable[[bool], None]


class _Request:
    def __init__(self, text: str, items: list[str] | None = None):
        self.text = text
        self.items = items
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.answer: bool | None = None
        self.closed = False

    def reply(self, answer: bool) -> None:
        with self.lock:
            if self.closed:
                return  # too late: the worker already took the timeout as No
            self.answer, self.closed = bool(answer), True
        self.event.set()

    def expire(self) -> bool:
        """Called by the worker on timeout; the final answer."""
        with self.lock:
            self.closed = True
            return self.answer is True


class _Pending:
    """The questions waiting for an answer. Plain Python: usable while the widgets die."""

    def __init__(self):
        self.lock = threading.Lock()
        self.requests: set[_Request] = set()

    def add(self, req: _Request) -> None:
        with self.lock:
            self.requests.add(req)

    def discard(self, req: _Request) -> None:
        with self.lock:
            self.requests.discard(req)

    def cancel_all(self, *_args) -> None:
        with self.lock:
            reqs, self.requests = list(self.requests), set()
        for req in reqs:
            req.reply(False)


class ConfirmBridge(QObject):
    """Create on the GUI thread. `show(text, reply)` runs on the GUI thread and must call
    reply(bool) once, now or later (tests replace it); the default opens an in-window Yes/No sheet (ConfirmSheet)."""

    _ask = Signal(object)

    def __init__(self, parent: QWidget | None = None, timeout: float = CONFIRM_TIMEOUT):
        super().__init__(parent)
        self._widget = parent
        self.timeout = timeout
        self.show: Callable[[str, Reply], None] = self._sheet
        self.items: list[str] | None = None  # what the question being shown will write
        self._pending = _Pending()
        self._sheets: list[ConfirmSheet] = []
        self._ask.connect(self._on_ask)

    def canceller(self) -> Callable[..., None]:
        """A Qt-free callable that answers every pending question No (for teardown hooks)."""
        return self._pending.cancel_all

    def cancel(self) -> None:
        """GUI thread: answer every pending question No now and close the open sheets."""
        self._pending.cancel_all()
        sheets, self._sheets = self._sheets, []
        for sheet in sheets:
            if shiboken6.isValid(sheet):
                sheet.answer(False)

    def confirm(self, text: str, items: list[str] | None = None) -> bool:
        """Worker thread only: True only for an explicit Yes within the timeout."""
        if threading.current_thread() is threading.main_thread():
            raise RuntimeError("ConfirmBridge.confirm must not run on the GUI thread")
        req = _Request(text, items)
        self._pending.add(req)
        try:
            try:
                self._ask.emit(req)
            except RuntimeError:  # the bridge was deleted (window closed): No
                return False
            if req.event.wait(self.timeout):
                return req.answer is True
            return req.expire()
        finally:
            self._pending.discard(req)

    @Slot(object)
    def _on_ask(self, req: _Request) -> None:
        if req.closed:  # the worker already gave up (timeout or window closing): no stale sheet
            return
        self.items = req.items
        self.show(req.text, req.reply)

    def _sheet(self, text: str, reply: Reply) -> None:
        owner = self._widget
        window = owner.window() if owner is not None and shiboken6.isValid(owner) else None
        if window is None or not owner.isVisible():
            reply(False)  # no visible window to ask in: that is a No
            return

        def done(sheet: ConfirmSheet) -> None:
            if sheet in self._sheets:
                self._sheets.remove(sheet)

        # the sheet times out at the same moment the worker gives up
        sheet = ConfirmSheet(window, owner, text, reply, self.timeout, self.items, done)
        self._sheets.append(sheet)
