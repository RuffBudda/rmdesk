"""Run blocking work off the GUI thread; callbacks are delivered on the GUI thread."""
from __future__ import annotations

from typing import Callable, TypeVar

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

T = TypeVar("T")


class _Dispatcher(QObject):
    """Lives in the GUI thread; queued signal delivery marshals callbacks onto it."""
    call = Signal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(self._run)

    @Slot(object)
    def _run(self, fn):
        fn()


_dispatcher: _Dispatcher | None = None


def _get_dispatcher() -> _Dispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = _Dispatcher()
    return _dispatcher


def call_on_gui(fn: Callable[[], None]) -> None:
    """Run fn on the GUI thread (safe to call from any thread once the dispatcher exists)."""
    _get_dispatcher().call.emit(fn)


class _Task(QRunnable):
    def __init__(self, fn, on_done, on_error, dispatcher):
        super().__init__()
        self._fn, self._done, self._error, self._d = fn, on_done, on_error, dispatcher

    def run(self):
        try:
            result = self._fn()
        except Exception as e:
            self._d.call.emit(lambda e=e: self._error(e))
        else:
            self._d.call.emit(lambda: self._done(result))


def run_in_thread(fn: Callable[[], T], on_done: Callable[[T], None],
                  on_error: Callable[[Exception], None]) -> None:
    """Call from the GUI thread. fn runs in the pool; on_done/on_error run on the GUI thread."""
    QThreadPool.globalInstance().start(_Task(fn, on_done, on_error, _get_dispatcher()))
