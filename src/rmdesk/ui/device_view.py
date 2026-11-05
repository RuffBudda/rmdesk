"""Device view: connection setup, SSH key install, Wi-Fi IP, firmware info, screen streaming,
fast USB uploads (the tablet's USB web interface switch)."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QProgressBar, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from rmdesk import config, device, safety, webui
from rmdesk import connection
from rmdesk.connection import BUSY_MSG, Connection, State, install_ssh_key
from rmdesk.device import DeviceError, HostKeyChanged
from rmdesk.library import ops
from rmdesk.stream import install as gms
from rmdesk.ui.theme import TOKENS
from rmdesk.ui.widgets import Card, Chip, EmptyState, repolish
from rmdesk.ui.widgets.back import back_button
from rmdesk.ui.workers import run_in_thread

HOST_KEY_MSG = ("The tablet's security key changed. This happens after a factory reset or "
                "firmware reinstall — or if another device is pretending to be your tablet.")
PASSWORD_HINT = ("The password is on the tablet under "
                 "Settings → Help → Copyrights and licenses.")
PASSPHRASE_HINT = "If your SSH key has a passphrase, run ssh-add first."
# stream status -> (chip state, chip text)
STREAM_CHIPS = {"running": ("ok", "Running"), "stopped": ("neutral", "Stopped"),
                "failing": ("err", "Failed to start"), "missing": ("warn", "Missing")}
FAST_CHIPS = {True: ("ok", "On"), False: ("neutral", "Off"), None: ("warn", "Unknown")}
TWO_COLUMN_MIN_WIDTH = 900  # below this the cards stack in one column

FIRMWARE_CHANGED_MSG = "Firmware changed — reinstall screen streaming?"
STREAM_UNSUPPORTED_MSG = "Screen streaming needs tablet firmware 3.24 or newer."
FAST_USB_TIP = "Connect over USB to change this."
FAST_UNKNOWN_TIP = "rmdesk couldn't read this setting from the tablet."
FAST_CONFIRM_ON = ("rmdesk will turn on the tablet's USB web interface. The tablet's screen "
                   "restarts once (a few seconds). After that, USB uploads won't restart it. "
                   "Continue?")
FAST_CONFIRM_OFF = ("rmdesk will turn off the tablet's USB web interface. The tablet's screen "
                    "restarts once (a few seconds). After that, USB uploads restart it again. "
                    "Continue?")
FAST_DONE_ON = "Fast USB uploads are on. USB uploads won't restart the tablet."
FAST_DONE_OFF = "Fast USB uploads are off."


def parse_avail(df_out: str) -> str:
    """The 'Avail' column of `df -h /home` output."""
    lines = [ln.split() for ln in df_out.strip().splitlines()]
    if len(lines) < 2:
        return "unknown"
    header, row = lines[0], lines[-1]
    try:
        return row[header.index("Avail")]
    except (ValueError, IndexError):
        return row[3] if len(row) > 3 else "unknown"


def parse_storage(df_out: str) -> tuple[float, float] | None:
    """(avail GB, size GB) from `df -h /home` output, or None if unreadable."""
    lines = [ln.split() for ln in df_out.strip().splitlines()]
    if len(lines) < 2:
        return None
    header, row = lines[0], lines[-1]
    try:
        size, avail = _to_gb(row[header.index("Size")]), _to_gb(row[header.index("Avail")])
    except (ValueError, IndexError):
        return None
    return (avail, size) if size > 0 else None


def _to_gb(human: str) -> float:
    units = {"K": 1 / 1024 ** 2, "M": 1 / 1024, "G": 1.0, "T": 1024.0}
    unit = human[-1].upper()
    if unit in units:
        return float(human[:-1]) * units[unit]
    return float(human) / 1024 ** 3


def _gb(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".") + " GB"


def _mono(text: str) -> QLabel:
    lb = QLabel(text)
    lb.setProperty("role", "mono")
    lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lb


def _field_label(text: str) -> QLabel:
    lb = QLabel(text)
    lb.setProperty("role", "hint")
    return lb


def _card(title: str, sub: str) -> tuple[Card, QVBoxLayout]:
    card = Card()
    lay = QVBoxLayout(card)
    lay.setContentsMargins(18, 16, 18, 16)
    lay.setSpacing(10)
    h = QLabel(title)
    h.setObjectName("heading")
    s = QLabel(sub)
    s.setObjectName("hint")
    s.setWordWrap(True)
    lay.addWidget(h)
    lay.addWidget(s)
    return card, lay


class _CardGrid(QWidget):
    """Cards in two columns when wide, one column when narrow."""

    def __init__(self, cards: list[QWidget]):
        super().__init__()
        self._cards = cards
        self._cols = 0
        self._lay = QGridLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(16)
        self._place(1)

    def columns(self) -> int:
        return self._cols

    def _place(self, cols: int) -> None:
        if cols == self._cols:
            return
        self._cols = cols
        for c in self._cards:
            self._lay.removeWidget(c)
        for i, c in enumerate(self._cards):
            self._lay.addWidget(c, i // cols, i % cols, Qt.AlignmentFlag.AlignTop)
        for col in range(2):
            self._lay.setColumnStretch(col, 1 if col < cols else 0)
        self._lay.setRowStretch(len(self._cards), 1)

    def resizeEvent(self, e) -> None:
        self._place(2 if e.size().width() >= TWO_COLUMN_MIN_WIDTH else 1)
        super().resizeEvent(e)


def find_pubkey() -> Path | None:
    for name in ("id_ed25519.pub", "id_rsa.pub"):
        p = Path.home() / ".ssh" / name
        if p.exists():
            return p
    return None


class DeviceView(QWidget):
    fast_uploads_changed = Signal()  # the web interface setting was changed on the tablet
    go_screen = Signal()  # the back button
    connect_finished = Signal()  # a connect_now ended (connected or not)
    _job_changed = Signal()  # Connection.on_job fires on any thread; this hops to the GUI

    def __init__(self, conn: Connection, cfg: config.Config, save_config=config.save,
                 known_hosts: Path = device.KNOWN_HOSTS, downloader=gms.http_download,
                 stream_secrets: gms.SecretStore | None = None,
                 web_factory=None, backup_root: Path = safety.BACKUP_ROOT):
        super().__init__()
        self.setObjectName("page")
        self.conn, self.cfg = conn, cfg
        self._save = save_config
        self._known_hosts = known_hosts
        self._downloader = downloader
        self._stream_secrets = stream_secrets
        self.stream_status: str | None = None
        self._stream_loading = False
        # ops.UsbWeb looked up when used (tests replace it)
        self._web_factory = web_factory if web_factory is not None else (lambda: ops.UsbWeb())
        self._backup_root = backup_root
        self.fast_state: bool | None = None
        self._fast_loaded = False  # fast_state reflects the connected tablet
        self._fast_busy = False

        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setAccessibleName("Tablet password")
        self.hint = QLabel(PASSWORD_HINT)
        self.hint.setObjectName("hint")
        self.hint.setWordWrap(True)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setProperty("variant", "primary")
        self.key_btn = QPushButton("Install my SSH key")
        self.wifi_ip = QLineEdit(cfg.wifi_ip or "")
        self.wifi_ip.setPlaceholderText("e.g. 192.168.1.50")
        self.wifi_ip.setAccessibleName("Wi-Fi address")
        self.message = QLabel("")
        self.message.setObjectName("error")
        self.message.setWordWrap(True)
        self.forget_btn = QPushButton("Forget saved tablet key")
        self.forget_btn.hide()
        self.firmware_label = _mono("—")
        self.serial_label = _mono("—")
        self.ip_label = _mono("—")
        self.free_label = QLabel("—")
        self.storage_bar = QProgressBar()
        self.storage_bar.setRange(0, 100)
        self.storage_bar.setTextVisible(False)
        self.storage_bar.setValue(0)
        # the battery (MainWindow's BatteryMonitor calls set_battery); hidden while unknown
        self.battery = None
        self.battery_label = QLabel("")
        self.battery_label.setObjectName("batteryText")
        self.battery_bar = QProgressBar()
        self.battery_bar.setObjectName("batteryBar")
        self.battery_bar.setRange(0, 100)
        self.battery_bar.setTextVisible(False)
        self.battery_bar.setFixedWidth(56)
        self.battery_bar.setAccessibleName("Battery")
        self.stream_label = Chip("neutral", "—")
        self.stream_btn = QPushButton("Install")
        self.stream_btn.setEnabled(False)
        self.stream_uninstall_btn = QPushButton("Uninstall")
        self.stream_uninstall_btn.setEnabled(False)
        self.stream_msg = QLabel("")
        self.stream_msg.setObjectName("hint")
        self.stream_msg.setWordWrap(True)
        self.fast_label = Chip("neutral", "—")
        self.fast_btn = QPushButton("Turn on")
        self.fast_btn.setEnabled(False)

        # -- Connection card --
        conn_card, cl = _card("Connection", "How rmdesk signs in to your tablet.")
        cl.addWidget(_field_label("Tablet password"))
        cl.addWidget(self.password)
        cl.addWidget(self.hint)
        cl.addWidget(_field_label("Wi-Fi address"))
        cl.addWidget(self.wifi_ip)
        buttons = QHBoxLayout()
        buttons.addWidget(self.connect_btn)
        buttons.addWidget(self.key_btn)
        buttons.addStretch(1)
        cl.addLayout(buttons)
        cl.addWidget(self.message)
        cl.addWidget(self.forget_btn, 0, Qt.AlignmentFlag.AlignLeft)

        # -- Tablet info card --
        info_card, il = _card("reMarkable 2", "About this tablet.")
        self.info_box = QWidget()
        ib = QVBoxLayout(self.info_box)
        ib.setContentsMargins(0, 0, 0, 0)
        ib.setSpacing(8)
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setColumnStretch(1, 1)
        for row, (name, w) in enumerate((("Firmware", self.firmware_label),
                                         ("Serial", self.serial_label),
                                         ("Connection", self.ip_label))):
            grid.addWidget(_field_label(name), row, 0)
            w.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(w, row, 1)
        self.battery_field = _field_label("Battery")
        self.battery_value = QWidget()
        bv = QHBoxLayout(self.battery_value)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(8)
        bv.addStretch(1)
        bv.addWidget(self.battery_bar, 0, Qt.AlignmentFlag.AlignVCenter)
        bv.addWidget(self.battery_label)
        grid.addWidget(self.battery_field, 3, 0)
        grid.addWidget(self.battery_value, 3, 1)
        self.set_battery(None)
        ib.addLayout(grid)
        ib.addWidget(_field_label("Free space"))
        ib.addWidget(self.storage_bar)
        ib.addWidget(self.free_label)
        self.offline = EmptyState("offline", "Your reMarkable isn't connected", "Try again")
        self.offline.action.connect(self.connect_now)
        il.addWidget(self.info_box)
        il.addWidget(self.offline)
        self.offline.hide()
        il.addStretch(1)

        # -- Screen streaming card --
        stream_card, sl = _card("Screen streaming",
                                "Lets the Screen view show the tablet live.")
        stream_row = QHBoxLayout()
        stream_row.addWidget(self.stream_label)
        stream_row.addStretch(1)
        stream_row.addWidget(self.stream_btn)
        stream_row.addWidget(self.stream_uninstall_btn)
        sl.addLayout(stream_row)
        sl.addWidget(self.stream_msg)
        sl.addStretch(1)

        # -- Fast USB uploads card --
        fast_card, fl = _card("Fast USB uploads",
                              "Much quicker file transfers over the cable. USB only.")
        fast_row = QHBoxLayout()
        fast_row.addWidget(self.fast_label)
        fast_row.addStretch(1)
        fast_row.addWidget(self.fast_btn)
        fl.addLayout(fast_row)
        fl.addStretch(1)

        # -- Sending (Compose) card --
        send_card, sdl = _card("Sending", "How Send to tablet behaves.")
        self.confirm_check = QCheckBox("Ask before writing to the tablet")
        self.confirm_check.setToolTip("Show “Write to the tablet?” before each send. Off: "
                                      "clicking Send sends; Undo last send takes it back.")
        self.confirm_check.setChecked(bool(cfg.confirm_sends))
        self.confirm_check.toggled.connect(self._set_confirm_sends)
        sdl.addWidget(self.confirm_check)
        sdl.addStretch(1)

        self.cards = [conn_card, info_card, stream_card, fast_card, send_card]
        self._grid = _CardGrid(self.cards)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self._grid)
        self._grid.setObjectName("deviceGrid")
        scroll.viewport().setAutoFillBackground(False)
        scroll.setStyleSheet("QScrollArea, #deviceGrid { background: transparent; }")
        self.back_btn = back_button()
        self.back_btn.clicked.connect(self.go_screen.emit)
        title = QLabel("Device")
        title.setObjectName("title")
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(self.back_btn)
        head.addWidget(title)
        head.addStretch(1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(14)
        lay.addLayout(head)
        lay.addWidget(scroll, 1)

        QWidget.setTabOrder(self.back_btn, self.password)
        QWidget.setTabOrder(self.password, self.wifi_ip)
        QWidget.setTabOrder(self.wifi_ip, self.connect_btn)
        QWidget.setTabOrder(self.connect_btn, self.key_btn)
        QWidget.setTabOrder(self.key_btn, self.forget_btn)
        QWidget.setTabOrder(self.forget_btn, self.offline.button)
        QWidget.setTabOrder(self.offline.button, self.stream_btn)
        QWidget.setTabOrder(self.stream_btn, self.stream_uninstall_btn)
        QWidget.setTabOrder(self.stream_uninstall_btn, self.fast_btn)
        self._set_offline_look(True)

        self.connect_btn.clicked.connect(self.connect_now)
        self._connecting = False
        self._job_changed.connect(self._update_connect_btn)
        conn.on_job.append(self._job_changed.emit)
        self.key_btn.clicked.connect(self.install_key)
        self.forget_btn.clicked.connect(self.forget_key)
        self.wifi_ip.editingFinished.connect(self._save_ip)
        self.stream_btn.clicked.connect(self.install_stream)
        self.stream_uninstall_btn.clicked.connect(self.uninstall_stream)
        self.fast_btn.clicked.connect(self.toggle_fast_uploads)

    # -- actions (all device work runs in a worker) --
    def _set_confirm_sends(self, on: bool) -> None:
        self.cfg.confirm_sends = bool(on)
        try:
            self._save(self.cfg)
        except OSError:
            pass

    def _save_ip(self) -> None:
        ip = self.wifi_ip.text().strip() or None
        if ip != self.cfg.wifi_ip:
            self.cfg.wifi_ip = ip
            try:
                self._save(self.cfg)
            except Exception as e:
                self.message.setText(f"Could not save settings: {e}")

    def _update_connect_btn(self) -> None:
        # a reconnect closes the link a running job (a send, an upload...) is using
        self.connect_btn.setEnabled(not self._connecting and self.conn.current_job is None)

    @property
    def connecting(self) -> bool:
        """A connect started here (Connect, Try again, the onboarding screen) is running."""
        return self._connecting

    def connect_now(self) -> None:
        job = self.conn.current_job
        if job is not None:
            self.message.setText(BUSY_MSG.format(job))
            self._update_connect_btn()
            return
        self._save_ip()
        if self.password.text():
            self.conn.set_password(self.password.text())
        self._connecting = True
        self._update_connect_btn()

        def done(_state):
            self._connecting = False
            self._update_connect_btn()
            self.refresh()
            self._load_free_space()
            self.connect_finished.emit()

        def err(e):
            self._connecting = False
            self._update_connect_btn()
            self.message.setText(str(e))
            self.refresh()
            self.connect_finished.emit()

        run_in_thread(self.conn.connect, done, err)

    def install_key(self) -> None:
        pub = find_pubkey()
        if pub is None:
            self.message.setText("No SSH key found. Create one with: ssh-keygen -t ed25519")
            return
        self.key_btn.setEnabled(False)

        def work():
            try:
                install_ssh_key(self.conn.ensure(), pub)
            except DeviceError as e:
                self.conn.connection_lost(e)
                raise

        def done(_):
            self.key_btn.setEnabled(True)
            self.message.setText("")
            QMessageBox.information(self, "rmdesk", "SSH key installed.")

        def err(e):
            self.key_btn.setEnabled(True)
            self.message.setText(f"Could not install key: {e}")
            self.refresh()

        run_in_thread(work, done, err)

    def _load_free_space(self) -> None:
        dev = self.conn.device
        if dev is None:
            self._show_free(None)
            return

        def work():
            try:
                return dev.run("df -h /home").out
            except DeviceError as e:
                self.conn.connection_lost(e)
                raise

        run_in_thread(work, self._show_free, lambda e: self._show_free(None))

    def _show_free(self, df_out: str | None) -> None:
        st = parse_storage(df_out) if df_out else None
        if st is None:
            avail = parse_avail(df_out) if df_out else "unknown"
            self.free_label.setText("—" if avail == "unknown" else f"{avail} free")
            self.storage_bar.setValue(0)
            return
        avail, size = st
        self.free_label.setText(f"{_gb(avail)} free of {_gb(size)}")
        self.storage_bar.setValue(round((size - avail) / size * 100))

    # -- screen streaming (goMarkableStream) --
    def load_stream_status(self) -> None:
        dev, info = self.conn.device, self.conn.info
        if dev is None:
            self._show_stream_status(None)
            return
        if self._stream_loading:
            return
        self._stream_loading = True

        def work():
            try:
                return gms.status(dev)
            except DeviceError as e:
                self.conn.connection_lost(e)
                raise

        def done(status):
            self._stream_loading = False
            self._show_stream_status(status)
            if info is None or not info.version:
                return
            # a firmware update wipes /etc (the unit file) but keeps the binary in
            # /home, which reads as "stopped" rather than "missing"
            if (status != "running" and self.cfg.last_firmware
                    and self.cfg.last_firmware != info.version
                    and gms.stream_supported(info.version)):
                self.stream_msg.setText(FIRMWARE_CHANGED_MSG)
            if self.cfg.last_firmware != info.version:
                self.cfg.last_firmware = info.version
                try:
                    self._save(self.cfg)
                except Exception as e:
                    self.message.setText(f"Could not save settings: {e}")

        def err(e):
            self._stream_loading = False
            self._show_stream_status(None)
            self.stream_msg.setText(f"Could not check screen streaming: {e}")

        run_in_thread(work, done, err)

    def _show_stream_status(self, status: str | None) -> None:
        self.stream_status = status
        info = self.conn.info
        supported = bool(info and gms.stream_supported(info.version))
        state, text = STREAM_CHIPS.get(status, ("neutral", status)) if status else ("neutral", "—")
        self.stream_label.set_state(state, text)
        self.stream_btn.setText("Install" if status in (None, "missing") else "Reinstall")
        self.stream_btn.setEnabled(status is not None and supported)
        self.stream_uninstall_btn.setEnabled(status in ("stopped", "failing", "running"))
        if status is not None and info is not None and not supported:
            self.stream_msg.setText(STREAM_UNSUPPORTED_MSG)
        else:
            self.stream_msg.setText("")

    def _stream_done(self, status: str) -> None:
        self._stream_loading = False
        self._show_stream_status(status)

    def _stream_busy(self, text: str) -> None:
        self._stream_loading = True  # no status reload while install/uninstall runs
        self.stream_btn.setEnabled(False)
        self.stream_uninstall_btn.setEnabled(False)
        self.stream_msg.setText(text)

    def install_stream(self) -> None:
        info = self.conn.info
        if info is None or not gms.stream_supported(info.version):
            self.stream_msg.setText(STREAM_UNSUPPORTED_MSG)
            return
        tag = gms.pick_release(info.version)
        verb = "Install" if self.stream_status in (None, "missing") else "Reinstall"
        ok = QMessageBox.question(
            self, f"{verb} screen streaming",
            f"{verb} goMarkableStream {tag} for firmware {info.version}?\n\n"
            "It runs on the tablet as a service on port 2001, protected by a new "
            "random password kept in your keyring.")
        if ok != QMessageBox.StandardButton.Yes:
            return
        if not self.conn.try_begin_job("installing screen streaming"):
            self.stream_msg.setText(BUSY_MSG.format(self.conn.current_job))
            return
        self._stream_busy(f"Installing goMarkableStream {tag}…")
        serial, downloader, secrets = info.serial, self._downloader, self._stream_secrets

        def work():
            dev = self.conn.ensure()
            try:
                gms.install(dev, tag, downloader, serial=serial, secrets=secrets)
                return gms.status(dev)
            except DeviceError as e:
                self.conn.connection_lost(e)
                raise

        def done(status):
            self.conn.end_job()
            self._stream_done(status)

        def err(e):
            self.conn.end_job()
            self._stream_loading = False
            self._show_stream_status(self.stream_status)
            self.stream_msg.setText(f"Could not install screen streaming: {e}")

        self._start_job(work, done, err)

    def uninstall_stream(self) -> None:
        ok = QMessageBox.question(
            self, "Uninstall screen streaming",
            "Stop goMarkableStream and remove it from the tablet?\n\n"
            "Its settings folder on the tablet is kept.")
        if ok != QMessageBox.StandardButton.Yes:
            return
        if not self.conn.try_begin_job("removing screen streaming"):
            self.stream_msg.setText(BUSY_MSG.format(self.conn.current_job))
            return
        self._stream_busy("Removing goMarkableStream…")

        def work():
            dev = self.conn.ensure()
            try:
                gms.uninstall(dev)
                return gms.status(dev)
            except DeviceError as e:
                self.conn.connection_lost(e)
                raise

        def done(status):
            self.conn.end_job()
            self._stream_done(status)

        def err(e):
            self.conn.end_job()
            self._stream_loading = False
            self._show_stream_status(self.stream_status)
            self.stream_msg.setText(f"Could not uninstall screen streaming: {e}")

        self._start_job(work, done, err)

    # -- fast USB uploads (WebInterfaceEnabled in xochitl.conf) --
    def load_fast_uploads(self) -> None:
        dev = self.conn.device
        if dev is None or self._fast_busy:
            return
        self._fast_busy = True
        self._show_fast()

        def work():
            try:
                return webui.read_state(dev)
            except DeviceError as e:
                self.conn.connection_lost(e)
                raise

        def done(state):
            self._fast_busy = False
            self._fast_loaded = self.conn.device is not None
            self.fast_state = state
            self._show_fast()

        def err(e):
            self._fast_busy = False
            self._fast_loaded = self.conn.device is not None
            self.fast_state = None
            self._show_fast()

        run_in_thread(work, done, err)

    def _show_fast(self) -> None:
        online = self.conn.device is not None
        state = self.fast_state
        if not online or not self._fast_loaded:
            self.fast_label.set_state("neutral", "—")
        else:
            self.fast_label.set_state(*FAST_CHIPS[state])
        self.fast_btn.setText("Turn off" if state else "Turn on")
        tip = ""
        enabled = online and self._fast_loaded and not self._fast_busy and state is not None
        if online and self._fast_loaded and state is None:
            tip = FAST_UNKNOWN_TIP
        elif self.conn.state != State.USB:
            enabled = False  # either way: the change is only checkable over USB
            if online:
                tip = FAST_USB_TIP
        self.fast_btn.setEnabled(enabled)
        self.fast_btn.setToolTip(tip)

    def toggle_fast_uploads(self) -> None:
        if self._fast_busy or self.fast_state is None or self.conn.device is None:
            return
        enable = not self.fast_state
        if self.conn.state != State.USB:
            self.message.setText(FAST_USB_TIP)
            return
        if self.conn.current_job is not None:
            self.message.setText(BUSY_MSG.format(self.conn.current_job))
            return
        title = "Turn on fast USB uploads" if enable else "Turn off fast USB uploads"
        ok = QMessageBox.question(self, title, FAST_CONFIRM_ON if enable else FAST_CONFIRM_OFF)
        if ok != QMessageBox.StandardButton.Yes:
            return
        # re-check after the modal dialog: the link may have changed meanwhile
        if self._fast_busy or self.conn.device is None or self.conn.state != State.USB:
            self.message.setText("The tablet disconnected. Connect it and try again.")
            return
        if not self.conn.try_begin_job("changing fast USB uploads"):
            self.message.setText(BUSY_MSG.format(self.conn.current_job))
            return
        self._fast_busy = True
        self._show_fast()
        self.message.setText("Turning fast USB uploads on… the tablet's screen restarts."
                             if enable else
                             "Turning fast USB uploads off… the tablet's screen restarts.")
        web_factory, root = self._web_factory, self._backup_root

        def work():
            dev = self.conn.ensure()
            try:
                webui.set_enabled(dev, enable, backup_root=root,
                                  check_web=lambda: web_factory().available())
            except DeviceError as e:
                self.conn.connection_lost(e)
                raise

        def done(_):
            self.conn.end_job()
            self.conn.clear_pending()  # set_enabled restarted xochitl
            self._fast_busy = False
            self.message.setText(FAST_DONE_ON if enable else FAST_DONE_OFF)
            self.load_fast_uploads()
            self.fast_uploads_changed.emit()

        def err(e):
            self.conn.end_job()
            self._fast_busy = False
            self.message.setText(f"Could not change fast USB uploads: {e}"
                                 if not isinstance(e, webui.WebUiError) else str(e))
            self.load_fast_uploads()
            self._show_fast()
            self.fast_uploads_changed.emit()

        self._start_job(work, done, err)

    def _start_job(self, work, done, err) -> None:
        """Start a worker for a claimed device job; release the job if it can't start."""
        try:
            run_in_thread(work, done, err)
        except BaseException:
            self.conn.end_job()
            raise

    def forget_key(self) -> None:
        hosts = [h for h, e in self.conn.host_errors.items() if isinstance(e, HostKeyChanged)]
        if not hosts:
            return
        ok = QMessageBox.question(
            self, "Forget saved tablet key",
            "Forget the saved security key for " + ", ".join(hosts) + "?\n\n"
            "Only do this if you reset or reinstalled the tablet.")
        if ok != QMessageBox.StandardButton.Yes:
            return
        try:
            for h in hosts:
                device.forget_host_key(h, self._known_hosts)
        except OSError as e:
            self.message.setText(f"Could not edit known_hosts: {e}")
            return
        self.conn.last_error = None
        self.refresh()
        self.connect_now()

    # -- display --
    def set_battery(self, b) -> None:
        """The Battery row: "56% · Discharging" and a small bar; hidden when unknown."""
        self.battery = b
        known = b is not None
        self.battery_label.setText(b.detail() if known else "")
        self.battery_bar.setValue(b.percent if known else 0)
        self.battery_bar.setProperty("low", "true" if known and b.low else "false")
        repolish(self.battery_bar)
        self.battery_field.setVisible(known)
        self.battery_value.setVisible(known)

    def _set_offline_look(self, offline: bool) -> None:
        self.info_box.setVisible(not offline)
        self.offline.setVisible(offline)

    def refresh(self) -> None:
        info = self.conn.info
        self.firmware_label.setText(info.version if info else "—")
        self.serial_label.setText(info.serial if info and info.serial else "—")
        host = (connection.USB_HOST if self.conn.transport == "usb"
                else self.cfg.wifi_ip if self.conn.transport == "wifi" else None)
        self.ip_label.setText(f"{self.conn.transport.upper()} · {host}" if host else "—")
        self._set_offline_look(self.conn.device is None)
        err = self.conn.last_error
        changed = isinstance(err, HostKeyChanged)
        self.forget_btn.setVisible(changed)
        if changed:
            self.message.setText(HOST_KEY_MSG)
        elif err is not None and self.conn.state == State.OFFLINE:
            text = f"Could not connect: {err}"
            if not self.password.text():  # key-only login failed
                text += "\n" + PASSPHRASE_HINT
            self.message.setText(text)
        elif self.conn.state != State.OFFLINE:
            self.message.setText(self.conn.notice or "")  # e.g. the screen was restarted
        if self.conn.device is None:
            if self.stream_status is not None:
                self._show_stream_status(None)
            self._fast_loaded = False
            self.fast_state = None
            self._show_fast()
        else:
            if self.stream_status is None:
                self.load_stream_status()
            if not self._fast_loaded:
                self.load_fast_uploads()
            else:
                self._show_fast()
