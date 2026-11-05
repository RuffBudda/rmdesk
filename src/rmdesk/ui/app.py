"""Application shell: the stacked pages (Screen is home; Files and Device open from its
header and come back with "← Screen") and the status bar."""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow,
                               QStackedWidget, QVBoxLayout, QWidget)

from rmdesk import config
from rmdesk.ui import theme
from rmdesk.config import Config
from rmdesk.connection import BUSY_MSG, Connection, State
from rmdesk.library import ops
from rmdesk.ui.battery import BatteryMonitor
from rmdesk.ui.device_view import DeviceView
from rmdesk.ui.files_view import FilesView
from rmdesk.ui import onboarding as onb
from rmdesk.ui.onboarding import Onboarding
from rmdesk.ui.screen_view import CONN_STATE as PILL_STATE, CONN_TEXT as STATUS_TEXT, ScreenView

VIEWS = ["Files", "Screen", "Device"]  # the stack's pages (Ctrl+1/2/3)
HOME = "Screen"
CONNECT = "Connect"  # the onboarding page: shown for Screen until the tablet is reached


class _StateBridge(QObject):
    """Connection callbacks may fire on a worker thread; the queued signal hops to the GUI."""
    changed = Signal(object)
    job_changed = Signal()


class _Stack(QStackedWidget):
    """Minimum size follows the visible page only, so the window can shrink past the widest view."""

    def minimumSizeHint(self):
        w = self.currentWidget()
        return w.minimumSizeHint() if w is not None else super().minimumSizeHint()


class MainWindow(QMainWindow):
    def __init__(self, conn: Connection, cfg: Config, save_config=config.save,
                 web_factory=None):
        """`web_factory` makes the tablet's USB web interface client (Files and Device; the
        real one, ops.UsbWeb, when None)."""
        super().__init__()
        self.setWindowTitle("rmdesk")
        self.conn, self.cfg = conn, cfg
        self.shortcuts: dict[str, QShortcut] = {}
        self.stack = _Stack()
        self.stack.setObjectName("stack")
        self._index: dict[str, int] = {}
        web = web_factory if web_factory is not None else (lambda: ops.UsbWeb())
        for name in VIEWS:
            if name == "Device":
                widget: QWidget = DeviceView(conn, cfg, save_config, web_factory=web)
            else:
                widget = QLabel("Coming in a later task")
            self._add(name, widget)
        self.files_view = FilesView(conn, web_factory=web)
        self.files_view.go_device.connect(lambda: self.show_view("Device"))
        self.files_view.go_screen.connect(lambda: self.show_view(HOME))
        self.register_view("Files", self.files_view)
        # files dropped on the Screen view upload like Files → Upload (to My files)
        self.screen_view = ScreenView(conn, cfg=cfg, save_config=save_config,
                                      upload_fn=self.files_view.upload_dropped)
        self.screen_view.open_device.connect(lambda: self.show_view("Device"))
        self.screen_view.open_files.connect(lambda: self.show_view("Files"))
        self.register_view("Screen", self.screen_view)
        self.device_view: DeviceView = self.stack.widget(self._index["Device"])  # type: ignore[assignment]
        self.device_view.go_screen.connect(lambda: self.show_view(HOME))
        # until the tablet connects, the Screen view's place shows the connection screen
        self.onboarding = Onboarding(conn, self.device_view)
        self.onboarding.open_settings.connect(lambda: self.show_view("Device"))
        self._add(CONNECT, self.onboarding)
        self._enter_timer = QTimer(self)  # "Connected" stays up a moment, then Screen
        self._enter_timer.setSingleShot(True)
        self._enter_timer.setInterval(onb.CONNECTED_MS)
        self._enter_timer.timeout.connect(self._enter_screen)
        # the web interface changed: re-check it (and reload the restarted tablet's library)
        self.device_view.fast_uploads_changed.connect(self._fast_uploads_changed)
        # the connection state in the Screen header; a click opens Device
        self.pill = self.screen_view.conn_btn
        self._save_config = save_config
        for i, name in enumerate(VIEWS):
            sc = QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(lambda n=name: self.show_view(n))
            self.shortcuts[name] = sc
        sc = QShortcut(QKeySequence("F11"), self)
        sc.setContext(Qt.ShortcutContext.WindowShortcut)
        sc.activated.connect(self.toggle_fullscreen)
        self.shortcuts["fullscreen"] = sc
        self.screen_view.fullscreen_changed.connect(self._on_fullscreen)
        self.stack.setMinimumWidth(0)
        self.show_view(HOME)

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.status_bar = QFrame()
        self.status_bar.setObjectName("statusBar")
        self.status_bar.setFixedHeight(30)
        sl = QHBoxLayout(self.status_bar)
        sl.setContentsMargins(16, 0, 16, 0)
        self.status_label = QLabel()
        self.status_label.setObjectName("statusMessage")
        sl.addWidget(self.status_label, 1)
        outer.addWidget(self.status_bar)  # one 1 px top line; no QStatusBar chrome
        self.refresh_status()
        self._bridge = _StateBridge()
        self._bridge.changed.connect(self._on_state)
        conn.on_state.append(self._bridge.changed.emit)
        self._bridge.job_changed.connect(self.refresh_status)
        conn.on_job.append(self._bridge.job_changed.emit)
        # the tablet's battery: read after connecting, then every minute (header + Device)
        self.monitor = BatteryMonitor(conn, parent=self)
        self.monitor.changed.connect(self._battery_changed)
        self._update_pill(conn.state)
        self.resize(1000, 700)

    def _add(self, name: str, widget: QWidget) -> None:
        self._index[name] = self.stack.addWidget(widget)

    def register_view(self, name: str, widget: QWidget) -> None:
        """Replace the placeholder for `name` (a sidebar entry) with a real view."""
        idx = self._index[name]
        old = self.stack.widget(idx)
        current = self.stack.currentIndex()
        self.stack.removeWidget(old)
        old.deleteLater()
        self.stack.insertWidget(idx, widget)
        self.stack.setCurrentIndex(current)

    def toggle_fullscreen(self) -> None:
        """F11: expand the Screen view to fill the window (switches to Screen first)."""
        if self.screen_view.fullscreen:
            self.screen_view.set_fullscreen(False)
        else:
            self.show_view("Screen")
            if self.current_view() != "Screen":
                return  # not connected: the connection screen shows instead
            if self.screen_view.pointer == "place":  # Expand needs Control
                self.screen_view.set_pointer("control")
            self.screen_view.set_fullscreen(True)

    def _on_fullscreen(self, on: bool) -> None:
        """Expanded Screen view: the picture fills the window (the window itself stays as it
        is); the status bar hides meanwhile."""
        self._show_status_bar()

    def _online(self) -> bool:
        return self.conn.state in (State.USB, State.WIFI)

    def show_view(self, name: str) -> None:
        """Show a page; "Screen" shows the connection screen while the tablet isn't
        connected."""
        if name == "Screen" and CONNECT in self._index and not self._online():
            name = CONNECT
        if name != "Screen" and getattr(self, "screen_view", None) is not None \
                and self.screen_view.fullscreen:
            self.screen_view.set_fullscreen(False)
        self.stack.setCurrentIndex(self._index[name])
        if getattr(self, "status_label", None) is not None:
            self.refresh_status()  # job messages show on Files/Device, not on Screen

    def current_view(self) -> str:
        """The page shown: "Files", "Screen", "Device" or "Connect" (the connection
        screen)."""
        i = self.stack.currentIndex()
        return next((n for n, k in self._index.items() if k == i), "")

    def refresh_status(self) -> None:
        """Shell-level message: the tablet's current job, if any."""
        job = self.conn.current_job
        if job and self.current_view() == "Screen":
            job = None  # the Screen view says what it's doing under the picture (and its bar)
        # otherwise what the last connect did on its own (e.g. restarted a stopped screen)
        self.status_label.setText(BUSY_MSG.format(job) if job else (self.conn.notice or ""))
        self._show_status_bar()

    def _show_status_bar(self) -> None:
        """The bottom bar only takes room while it has something to say (and never while the
        Screen view is expanded)."""
        expanded = getattr(self, "screen_view", None) is not None and self.screen_view.fullscreen
        self.status_bar.setVisible(bool(self.status_label.text()) and not expanded)

    def _on_state(self, state: State) -> None:
        self._update_pill(state)
        self.refresh_status()
        self.device_view.refresh()
        view = self.current_view()
        if state in (State.USB, State.WIFI) and view == CONNECT:
            self._enter_timer.start()  # "Connected", then the Screen view by itself
        elif state == State.OFFLINE and view == "Screen":
            self.show_view(CONNECT)  # lost while watching: back to the connection screen
        # on Files or Device their own offline states stay

    def _enter_screen(self) -> None:
        if self.current_view() == CONNECT and self._online():
            self.show_view("Screen")

    def _update_pill(self, state: State) -> None:
        """The Connection button: the state, and "fw 3.29.1.0 · 56%" in its tooltip while
        connected (the serial is in the Device view)."""
        info = self.conn.info
        sub = None
        b = self.monitor.battery if state in (State.USB, State.WIFI) else None
        if state in (State.USB, State.WIFI) and info is not None and info.version:
            sub = f"fw {info.version}"
        self.pill.set_state(PILL_STATE[state], STATUS_TEXT[state], sub,
                            battery=b.short() if b else None, battery_low=bool(b and b.low))
        self.screen_view.hint.refresh()

    def _battery_changed(self, b) -> None:
        self._update_pill(self.conn.state)
        self.device_view.set_battery(b)
        self.screen_view.set_battery(b)

    def _fast_uploads_changed(self) -> None:
        if self.conn.state in (State.USB, State.WIFI):
            self.files_view.refresh()

    def closeEvent(self, e) -> None:
        self.screen_view.shutdown()
        super().closeEvent(e)

    def start(self) -> None:
        """Initial connect, off the GUI thread."""
        self.device_view.connect_now()


def load_theme() -> str:
    return theme.qss()


LOG_PATH = Path.home() / ".local" / "share" / "rmdesk" / "rmdesk.log"


def setup_logging(path: Path = LOG_PATH) -> None:
    """rmdesk's own log file (the app menu sends stderr nowhere): INFO and up, 1 MB x 3,
    readable only by the user. Never raises."""
    import logging
    import logging.handlers
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        h = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3,
                                                 encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root = logging.getLogger()
        root.addHandler(h)
        root.setLevel(logging.INFO)
        logging.getLogger("rmscene").setLevel(logging.ERROR)  # newer-format notices
        logging.getLogger("paramiko").setLevel(logging.WARNING)
        path.chmod(0o600)
    except OSError:
        pass


def main() -> int:
    setup_logging()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyleSheet(load_theme())
    cfg = config.load()
    conn = Connection(cfg, persist=config.save)
    win = MainWindow(conn, cfg)
    win.show()
    win.start()
    return app.exec()
