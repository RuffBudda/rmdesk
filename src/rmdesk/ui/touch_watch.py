"""Watch the tablet's touch events while items are placed (read only: nothing is sent).

TouchWatcher keeps one long-lived SSH exec channel, `cat /dev/input/eventN` on the touch device
found by name ("pt_mt", never by number), read on a thread of its own: the bytes become
one-finger gesture steps (remote.touch_watch) emitted on `touched`, which Qt delivers on the
GUI thread (LiveCompose passes them to compose.scroll_fuse). evdev gives every reader every
event, so rmdesk's own Control touches arrive here too, the same way. A few hundred events a
second at most.

watch(device) opens it (None closes it): LiveCompose asks for it while there are items, the
tablet is connected and shows the keyboard layout. A channel that dies is opened again after
BACKOFF seconds (growing, reset once one delivers events); shutdown() closes and joins.
"""
from __future__ import annotations

import logging
import shlex
import threading
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from rmdesk.device import DeviceError
from rmdesk.remote import input as ri
from rmdesk.remote.touch_watch import EventParser, Gesture

log = logging.getLogger(__name__)

BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0)  # seconds before reopening a channel that died
READ_SIZE = 4096


def default_opener(dev):
    """The touch device's stdout channel: find pt_mt by name, then `cat` it (worker thread)."""
    res = dev.run(f"cat {ri.PROC_DEVICES}", timeout=10)
    path = ri.find_device(getattr(res, "out", "") or "", ri.TOUCH_NAME)
    if path is None:
        raise DeviceError("the tablet has no touch device")
    return dev.open_reader(f"cat {shlex.quote(path)}")


def can_watch(dev) -> bool:
    return dev is not None and callable(getattr(dev, "open_reader", None))


class TouchWatcher(QObject):
    touched = Signal(object)  # a TouchEvent (on the GUI thread)
    _died = Signal(int, str)  # generation, error (from the reader thread)
    _alive = Signal(int)  # generation: events are coming

    def __init__(self, parent=None, opener: Callable = default_opener,
                 backoff: tuple = BACKOFF):
        super().__init__(parent)
        self._opener, self._backoff = opener, tuple(backoff)
        self.device = None  # the device being watched (or waiting to be reopened)
        self._gen = 0
        self._thread: threading.Thread | None = None
        self._stop: threading.Event | None = None
        self._reader_box: dict = {}
        self.fails = 0
        self.opens = 0  # channels started (for tests and the log)
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self._start)
        self._died.connect(self._on_died)
        self._alive.connect(self._on_alive)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def waiting(self) -> bool:
        return self._retry.isActive()

    def watch(self, dev) -> None:
        """Watch `dev` (None: stop). The same device again changes nothing."""
        if dev is self.device and dev is not None:
            return
        self.stop()
        self.device = dev
        if dev is not None:
            self.fails = 0
            self._start()

    def stop(self) -> None:
        self._retry.stop()
        self._gen += 1
        self.device = None
        if self._stop is not None:
            self._stop.set()
        reader = self._reader_box.get("reader")
        if reader is not None:
            try:
                reader.close()  # a blocked read returns now
            except Exception:
                pass
        self._stop, self._reader_box = None, {}

    def shutdown(self, timeout: float = 1.0) -> None:
        t = self._thread
        self.stop()
        if t is not None:
            t.join(timeout)
        self._thread = None

    @Slot()
    def _start(self) -> None:
        dev = self.device
        if dev is None:
            return
        self._gen += 1
        gen, stop, box = self._gen, threading.Event(), {}
        self._stop, self._reader_box = stop, box
        self.opens += 1
        self._thread = threading.Thread(target=self._run, args=(gen, dev, stop, box),
                                        daemon=True, name="rmdesk-touch-watch")
        self._thread.start()

    def _emit(self, signal, *args) -> None:
        try:
            signal.emit(*args)
        except RuntimeError:  # the watcher was deleted
            pass

    def _run(self, gen: int, dev, stop: threading.Event, box: dict) -> None:  # thread
        reader = None
        try:
            reader = self._opener(dev)
            box["reader"] = reader
            if stop.is_set():
                return
            parser, gesture = EventParser(), Gesture()
            said_alive = False
            while not stop.is_set():
                data = reader.read(READ_SIZE)
                if stop.is_set():
                    return
                if data is None:
                    continue
                if not data:
                    err = getattr(reader, "error", lambda: "")()
                    raise DeviceError(err or "the touch reader ended")
                if not said_alive:
                    said_alive = True
                    self._emit(self._alive, gen)
                for rep in parser.feed(data):
                    for ev in gesture.report(rep):
                        self._emit(self.touched, ev)
        except Exception as e:
            if not stop.is_set():
                self._emit(self._died, gen, str(e) or type(e).__name__)
        finally:
            if reader is not None:
                try:
                    reader.close()
                except Exception:
                    pass

    @Slot(int)
    def _on_alive(self, gen: int) -> None:
        if gen == self._gen:
            self.fails = 0

    @Slot(int, str)
    def _on_died(self, gen: int, error: str) -> None:
        if gen != self._gen or self.device is None:
            return
        delay = self._backoff[min(self.fails, len(self._backoff) - 1)]
        self.fails += 1
        log.info("touch watch stopped (%s); again in %.0f s", error, delay)
        self._retry.start(int(delay * 1000))
