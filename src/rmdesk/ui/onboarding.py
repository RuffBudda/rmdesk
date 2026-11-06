"""The connection screen: shown instead of the Screen view until the tablet is reached (at
start, and when the link drops while the Screen view is in use).

It has no connect logic of its own: it fills the Device view's password and Wi-Fi address
fields and calls DeviceView.connect_now (Connection.connect, the keyring via its secrets,
wifi_ip in the config). While it shows and the tablet is offline it tries again every
RETRY_MS, never while a connect is running, at most MAX_TRIES times in a row."""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

from rmdesk.connection import USB_HOST, Connection, State
from rmdesk.device import HostKeyChanged, UnknownHostKey
from rmdesk.ui.device_view import (HOST_KEY_MSG, PASSPHRASE_HINT, PASSWORD_HINT, DeviceView)
from rmdesk.ui.theme import TOKENS, icon
from rmdesk.ui.widgets import Card

RETRY_MS = 5000  # between automatic tries
MAX_TRIES = 60  # automatic tries in a row (about five minutes); then only Try again
CONNECTED_MS = 900  # "Connected" shows this long before the Screen view opens
CARD_WIDTH = 460

TAGLINE = "Your reMarkable on this computer: its files and its live screen."
LOOKING = "Looking for your reMarkable…"
CONNECTING = "Connecting…"
CONNECTED = "Connected"
RETRYING = "Trying again…"
RETRY_SOON = "Trying again every few seconds."
STOPPED = "Stopped trying. Press Try again when the tablet is ready."

WRONG_PASSWORD = "The password didn't work"
WRONG_PASSWORD_HELP = "Check it on the tablet and type it again below."
NEED_PASSWORD = "rmdesk needs the tablet's password"
NEED_PASSWORD_HELP = "Type it below and press Connect."
WIFI_SSH_OFF = "SSH over Wi-Fi is off"
WIFI_SSH_OFF_HELP = ("The tablet answers on Wi-Fi but doesn't let rmdesk in there. Connect "
                     "it with the USB cable, or turn on SSH over Wi-Fi on the tablet (over "
                     "USB, run rm-ssh-over-wlan on).")
KEY_CHANGED = "The tablet's security key changed"
UNKNOWN_KEY = "rmdesk doesn't know this tablet's key yet"
UNKNOWN_KEY_HELP = ("Over Wi-Fi rmdesk only sends your password to a tablet whose key it "
                    "knows. Connect once with the USB cable, or open settings to check and "
                    "trust the key.")
NOT_FOUND = "Couldn't find your reMarkable"
NOT_FOUND_HELP = "Plug it in with its USB cable and wake it up."
NOT_FOUND_WIFI = (" For Wi-Fi, check that it is awake on the same network as this computer "
                  "and that the address below is right.")
NOT_FOUND_NO_WIFI = " Or enter its Wi-Fi address below."
LOST = "The tablet disconnected"
LOST_HELP = "Plug it back in or check its Wi-Fi; rmdesk keeps looking."
OTHER = "Couldn't connect"


def hosts_line(wifi_ip: str | None) -> str:
    """Where rmdesk looks: "over USB (10.11.99.1) and Wi-Fi (192.168.1.50)"."""
    usb = f"over USB ({USB_HOST})"
    return f"{usb} and Wi-Fi ({wifi_ip})" if wifi_ip else usb


@dataclass
class Problem:
    title: str
    advice: str
    needs_password: bool = False


def _is_auth(e: Exception) -> bool:
    return "authentication" in str(e).lower()


def _is_refused(e: Exception) -> bool:
    t = str(e).lower()
    return "refused" in t or "unable to connect to port 22" in t


def connect_problem(conn: Connection) -> Problem:
    """What went wrong with the last connect, in words, and what to do (the Device view's
    texts where it has them)."""
    errs = dict(conn.host_errors)
    err = conn.last_error
    every = list(errs.values()) + ([err] if err is not None and err not in errs.values() else [])
    if any(isinstance(e, HostKeyChanged) for e in every):
        return Problem(KEY_CHANGED, f"{HOST_KEY_MSG} Open settings to forget the saved key.")
    if any(isinstance(e, UnknownHostKey) for e in every) and not any(
            _is_auth(e) for e in every):
        return Problem(UNKNOWN_KEY, UNKNOWN_KEY_HELP)
    if any(_is_auth(e) for e in every):
        if conn.password_used:
            return Problem(WRONG_PASSWORD, WRONG_PASSWORD_HELP, True)
        return Problem(NEED_PASSWORD, f"{NEED_PASSWORD_HELP} {PASSPHRASE_HINT}", True)
    wifi = conn.cfg.wifi_ip
    if wifi and wifi in errs and _is_refused(errs[wifi]):
        return Problem(WIFI_SSH_OFF, WIFI_SSH_OFF_HELP)
    if not errs and err is not None:  # no connect since: the link went down
        return Problem(LOST, LOST_HELP)
    if errs:
        return Problem(NOT_FOUND, NOT_FOUND_HELP + (NOT_FOUND_WIFI if wifi else NOT_FOUND_NO_WIFI))
    return Problem(OTHER, str(err) if err is not None else "")


class Spinner(QWidget):
    """A small turning arc (only turns while shown and on)."""

    def __init__(self, size: int = 22, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._angle = 0
        self._on = False
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._step)

    def spinning(self) -> bool:
        return self._on

    def set_on(self, on: bool) -> None:
        self._on = bool(on)
        self.setVisible(self._on)
        if self._on:
            self._timer.start()
        else:
            self._timer.stop()

    def hideEvent(self, e) -> None:
        self._timer.stop()
        super().hideEvent(e)

    def showEvent(self, e) -> None:
        super().showEvent(e)
        if self._on:
            self._timer.start()

    def _step(self) -> None:
        self._angle = (self._angle + 12) % 360
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(2.5, 2.5, -2.5, -2.5)
        p.setPen(QPen(QColor(TOKENS["surface-3"]), 2.5))
        p.drawEllipse(r)
        pen = QPen(QColor(TOKENS["accent"]), 2.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(r, -self._angle * 16, 100 * 16)


class _Bridge(QObject):
    changed = Signal(object)
    job = Signal()


class Onboarding(QWidget):
    """The full-window connection card. `open_settings` asks for the Device page."""
    open_settings = Signal()

    def __init__(self, conn: Connection, device_view: DeviceView, retry_ms: int = RETRY_MS,
                 max_tries: int = MAX_TRIES):
        super().__init__()
        self.setObjectName("page")
        self.conn, self.device_view = conn, device_view
        self.max_tries = max_tries
        self.tries = 0  # automatic tries since this screen showed (or Try again)
        self._user = False  # the running connect was asked for (Connect / Try again)
        self._attempted = False  # a connect ran since this screen showed
        self._sent_password = ""  # the password last sent with Connect

        self.card = Card()
        self.card.setObjectName("onboardCard")
        self.card.setMinimumWidth(300)
        self.card.setMaximumWidth(CARD_WIDTH)
        self.card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        name = QLabel("rmdesk")
        name.setObjectName("display")
        tagline = QLabel(TAGLINE)
        tagline.setObjectName("hint")
        tagline.setWordWrap(True)
        self.spinner = Spinner()
        self.state_icon = QLabel()
        self.state_icon.setFixedSize(22, 22)
        self.state_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state_icon.hide()
        self.headline = QLabel(LOOKING)
        self.headline.setObjectName("onboardStatus")
        self.headline.setWordWrap(True)
        self.headline.setAccessibleName("Connection status")
        self.sub = QLabel("")
        self.sub.setObjectName("hint")
        self.sub.setWordWrap(True)
        self.advice = QLabel("")
        self.advice.setObjectName("onboardAdvice")
        self.advice.setWordWrap(True)
        self.advice.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.password_label = QLabel("Tablet password")
        self.password_label.setProperty("role", "hint")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setAccessibleName("Tablet password")
        self.password_hint = QLabel(PASSWORD_HINT)
        self.password_hint.setObjectName("hint")
        self.password_hint.setWordWrap(True)
        self.wifi_label = QLabel("Wi-Fi address (optional)")
        self.wifi_label.setProperty("role", "hint")
        self.wifi_ip = QLineEdit(conn.cfg.wifi_ip or "")
        self.wifi_ip.setPlaceholderText("e.g. 192.168.1.50")
        self.wifi_ip.setAccessibleName("Wi-Fi address")
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setProperty("variant", "primary")
        self.retry_btn = QPushButton("Try again")
        self.retry_btn.setIcon(icon("refresh", 16))
        self.settings_btn = QPushButton("Open settings")
        self.settings_btn.setObjectName("linkButton")
        self.settings_btn.setProperty("variant", "ghost")
        self.settings_btn.setProperty("dense", True)
        self.settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_btn.setToolTip("The Device page: password, Wi-Fi, SSH key (Ctrl+3)")

        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(28, 26, 28, 20)
        lay.setSpacing(6)
        lay.addWidget(name)
        lay.addWidget(tagline)
        lay.addSpacing(18)
        status = QHBoxLayout()
        status.setSpacing(12)
        status.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignTop)
        status.addWidget(self.state_icon, 0, Qt.AlignmentFlag.AlignTop)
        status.addWidget(self.headline, 1)
        lay.addLayout(status)
        self._sub_row = QHBoxLayout()
        self._sub_row.setContentsMargins(34, 0, 0, 0)  # under the headline, past the spinner
        self._sub_row.addWidget(self.sub, 1)
        lay.addLayout(self._sub_row)
        lay.addSpacing(4)
        lay.addWidget(self.advice)
        lay.addSpacing(10)
        self.form = QWidget()
        fl = QVBoxLayout(self.form)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(6)
        fl.addWidget(self.password_label)
        fl.addWidget(self.password)
        fl.addWidget(self.password_hint)
        fl.addSpacing(6)
        fl.addWidget(self.wifi_label)
        fl.addWidget(self.wifi_ip)
        lay.addWidget(self.form)
        lay.addSpacing(10)
        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addWidget(self.connect_btn)
        buttons.addWidget(self.retry_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.settings_btn)
        self._buttons = buttons
        lay.addLayout(buttons)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.card, 100)  # as wide as it may be (CARD_WIDTH), centred
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(2)

        for a, b in ((self.password, self.wifi_ip), (self.wifi_ip, self.connect_btn),
                     (self.connect_btn, self.retry_btn), (self.retry_btn, self.settings_btn)):
            QWidget.setTabOrder(a, b)

        self.retry_timer = QTimer(self)
        self.retry_timer.setSingleShot(True)
        self.retry_timer.setInterval(retry_ms)
        self.retry_timer.timeout.connect(self._auto_retry)
        self._bridge = _Bridge()
        self._bridge.changed.connect(self._on_state)
        self._bridge.job.connect(self.render)
        device_view.connect_finished.connect(self._connect_finished)
        conn.on_state.append(self._bridge.changed.emit)
        conn.on_job.append(self._bridge.job.emit)
        self.connect_btn.clicked.connect(lambda: self.try_now(user=True))
        self.retry_btn.clicked.connect(lambda: self.try_now(user=True))
        self.password.returnPressed.connect(lambda: self.try_now(user=True))
        self.wifi_ip.returnPressed.connect(lambda: self.try_now(user=True))
        self.settings_btn.clicked.connect(self.open_settings.emit)
        self.render()

    # -- trying --
    def busy(self) -> bool:
        """A connect is running (from here, Device, or a reconnect), or a tablet job holds
        the link: don't start another."""
        return (self.device_view.connecting or self.conn.state == State.CONNECTING
                or self.conn.current_job is not None)

    def try_now(self, user: bool = True) -> bool:
        """Connect with what is filled in (through the Device view). False when a connect is
        already running."""
        if self.busy():
            self.render()
            return False
        dv = self.device_view
        dv.wifi_ip.setText(self.wifi_ip.text().strip())
        typed = self.password.text()
        if typed:
            dv.password.setText(typed)
            self._sent_password = typed
        if user:
            self.tries = 0
        self._user = user
        self._attempted = True
        dv.connect_now()
        if not dv.connecting:  # refused (a job started meanwhile): say why
            self.render()
            self.advice.setText(dv.message.text())
            self.advice.show()
            return False
        self.retry_timer.stop()
        self.render()
        return True

    def _auto_retry(self) -> None:
        if not self.isVisible() or self.conn.state in (State.USB, State.WIFI):
            return
        if self.busy() or self._typing():
            self.retry_timer.start()  # look again later; never on top of a running connect
            return
        if self.tries >= self.max_tries:
            self.render()
            return
        self.tries += 1
        self.try_now(user=False)

    def _typing(self) -> bool:
        """A password is typed but not sent yet: let the user press Connect."""
        t = self.password.text()
        return self.password.isVisible() and bool(t) and t != self._sent_password

    def _schedule(self) -> None:
        if (self.isVisible() and self.conn.state == State.OFFLINE
                and self.tries < self.max_tries and not self.retry_timer.isActive()):
            self.retry_timer.start()

    def _on_state(self, state) -> None:
        if state in (State.USB, State.WIFI):
            self.retry_timer.stop()
            self.password.clear()
            self._sent_password = ""
        self.render()
        if state == State.OFFLINE:
            self._schedule()

    def _connect_finished(self) -> None:
        self.render()
        if self.conn.state == State.OFFLINE:
            self._schedule()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.tries = 0
        self._attempted = False
        self.wifi_ip.setText(self.conn.cfg.wifi_ip or self.wifi_ip.text())
        self.render()
        self._schedule()

    def hideEvent(self, e) -> None:
        super().hideEvent(e)
        self.retry_timer.stop()

    # -- display --
    def _set_icon(self, name: str | None, color: str = "") -> None:
        if name is None:
            self.state_icon.hide()
            return
        self.state_icon.setPixmap(icon(name, 20, color).pixmap(20, 20))
        self.state_icon.show()

    def problem(self) -> Problem | None:
        """The problem shown now (None while looking, connecting or connected)."""
        return self._problem

    def render(self) -> None:
        conn = self.conn
        state = conn.state
        online = state in (State.USB, State.WIFI)
        running = state == State.CONNECTING or self.device_view.connecting
        failed = state == State.OFFLINE and conn.last_error is not None
        self._problem = connect_problem(conn) if failed and not running else None
        shown = self._problem
        if running and not self._user and conn.last_error is not None and self._attempted:
            shown = connect_problem(conn)  # keep the reason up while trying again
        self.spinner.set_on(running or (not online and shown is None))
        if online:
            self._set_icon("check", TOKENS["ok"])
            self.headline.setText(CONNECTED)
            self.sub.setText("over USB" if conn.transport == "usb"
                             else f"over Wi-Fi ({conn.cfg.wifi_ip})")
        elif shown is not None:
            self._set_icon(None if running else "alert", TOKENS["accent-text"])
            self.headline.setText(shown.title)
            if running:
                self.sub.setText(RETRYING)
            elif self.tries >= self.max_tries:
                self.sub.setText(STOPPED)
            else:
                self.sub.setText(RETRY_SOON)
        else:
            self._set_icon(None)
            self.headline.setText(CONNECTING if (running and self._user) else LOOKING)
            self.sub.setText(hosts_line(self.wifi_ip.text().strip() or conn.cfg.wifi_ip))
        self.advice.setText(shown.advice if shown is not None else "")
        self.advice.setVisible(bool(self.advice.text()))
        # the password field: no password was stored for the last try, or it was refused
        need_pw = bool(shown and shown.needs_password) or (
            self._attempted_any() and conn.password_used is False and not online)
        newly = need_pw and self.password.isHidden()
        for w in (self.password_label, self.password, self.password_hint):
            w.setVisible(need_pw)
        if newly and self.isVisible():
            self.password.setFocus(Qt.FocusReason.OtherFocusReason)
        self.form.setVisible(not online)
        self.connect_btn.setVisible(need_pw and not online)
        self.retry_btn.setVisible(not need_pw and not online)
        for b in (self.connect_btn, self.retry_btn):
            b.setEnabled(not running and conn.current_job is None)
        self.settings_btn.setVisible(not online)

    def _attempted_any(self) -> bool:
        return self.conn.password_used is not None and bool(self.conn.host_errors
                                                            or self.conn.last_error)

    def sizeHint(self) -> QSize:
        return QSize(CARD_WIDTH + 32, 520)
