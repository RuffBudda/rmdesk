"""Polls the tablet's battery while connected (the connection pill and the Device view)."""
from __future__ import annotations

import shiboken6
from PySide6.QtCore import QObject, QTimer, Signal

from rmdesk import battery
from rmdesk.battery import Battery
from rmdesk.connection import Connection, State
from rmdesk.device import DeviceError
from rmdesk.ui.workers import run_in_thread

INTERVAL = 60.0  # seconds between reads while connected


class BatteryMonitor(QObject):
    """Reads the battery right after connecting, then every `interval` seconds while
    connected: one cheap command on the worker pool, never while a tablet job holds the slot
    (that tick is skipped), one read at a time. `battery` is None when unknown or offline."""

    changed = Signal(object)  # Battery | None
    _state = Signal(object)  # Connection.on_state fires on any thread; this hops to the GUI
    _job = Signal()  # likewise Connection.on_job

    def __init__(self, conn: Connection, interval: float = INTERVAL, parent: QObject | None = None):
        super().__init__(parent)
        self.conn = conn
        self.battery: Battery | None = None
        self._reading = False
        self._missed = False  # the first read after connecting was skipped for a job
        self._timer = QTimer(self)
        self._timer.setInterval(round(interval * 1000))
        self._timer.timeout.connect(self.poll)
        self._state.connect(self._on_state)
        sig = self._state

        def on_state(state):
            if shiboken6.isValid(self):
                sig.emit(state)
        conn.on_state.append(on_state)
        job = self._job
        self._job.connect(self._on_job)

        def on_job():
            if shiboken6.isValid(self):
                job.emit()
        conn.on_job.append(on_job)
        if self._connected():
            self._on_state(conn.state)

    def _connected(self) -> bool:
        return self.conn.state in (State.USB, State.WIFI) and self.conn.device is not None

    def _on_state(self, _state) -> None:
        if self._connected():
            if not self._timer.isActive():
                self._timer.start()
            self._missed = self.battery is None
            self.poll()
        else:
            self._timer.stop()
            self._missed = False
            self._set(None)

    def _on_job(self) -> None:
        """A job ended: the read right after connecting may still be owed."""
        if self._missed and self.conn.current_job is None:
            self.poll()

    def poll(self) -> None:
        conn = self.conn
        if self._reading or not self._connected() or conn.current_job is not None:
            return
        self._missed = False
        dev = conn.device
        self._reading = True

        def work():
            try:
                return battery.read(dev)
            except DeviceError as e:
                conn.connection_lost(e)
                raise

        run_in_thread(work, self._got, lambda _e: self._got(None))

    def _got(self, b: Battery | None) -> None:
        if not shiboken6.isValid(self):
            return  # the window closed during the read
        self._reading = False
        self._set(b if self._connected() else None)

    def _set(self, b: Battery | None) -> None:
        if b != self.battery:
            self.battery = b
            self.changed.emit(b)
