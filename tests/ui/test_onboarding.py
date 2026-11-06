"""The connection screen shown instead of the Screen view until the tablet is reached."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading

import pytest
from PySide6.QtCore import QPoint, Qt, QThreadPool
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from rmdesk import firmware
from rmdesk.config import Config
from rmdesk.connection import USB_HOST, Connection, State
from rmdesk.device import DeviceError, HostKeyChanged, UnknownHostKey

_KEY = []


def _wifi_key():
    import paramiko
    if not _KEY:
        _KEY.append(paramiko.RSAKey.generate(1024))
    return _KEY[0]
from rmdesk.firmware import FirmwareInfo, XOCHITL_DIR
from rmdesk.ui import onboarding as onb
from rmdesk.ui.app import MainWindow
from rmdesk.ui.device_view import PASSWORD_HINT
from tests.fakes import FakeDevice

WIFI = "192.168.1.50"


@pytest.fixture(autouse=True)
def drain_workers(qtbot, monkeypatch):
    monkeypatch.setattr(firmware, "detect", lambda dev: FirmwareInfo("3.29.1.0", "SN123", 6))
    yield
    QThreadPool.globalInstance().waitForDone(5000)
    QApplication.processEvents()


class Secrets:
    def __init__(self, stored=None):
        self.store = dict(stored or {})

    def get(self, k):
        return self.store.get(k)

    def set(self, k, v):
        self.store[k] = v


class NoWeb:
    def available(self):
        return False


class Tablet:
    """The connection factory: `mode` says how the tablet answers."""

    def __init__(self, mode="down", password="secret"):
        self.mode, self.password = mode, password
        self.calls: list[tuple[str, str | None]] = []
        self.gate: threading.Event | None = None

    def usb_calls(self) -> int:
        return sum(1 for h, _p in self.calls if h == USB_HOST)

    def __call__(self, host, password):
        self.calls.append((host, password))
        if self.gate is not None:
            self.gate.wait(5)
        mode = self.mode
        if mode == "down" or (mode == "wifi-off" and host == USB_HOST):
            raise DeviceError("[Errno 113] No route to host")
        if mode == "wifi-off":
            raise DeviceError(f"[Errno None] Unable to connect to port 22 on {host}")
        if mode == "hostkey":
            raise HostKeyChanged(f"host key for {host} has changed")
        if mode == "unknown-key":
            if host == USB_HOST:
                raise DeviceError("[Errno 113] No route to host")
            raise UnknownHostKey(host, _wifi_key())
        if mode == "auth" and password != self.password:
            raise DeviceError("Authentication failed.")
        return FakeDevice({f"{XOCHITL_DIR}/.keep": b""})


def make(qtbot, mode="down", wifi=WIFI, secrets=None, retry_ms=None, max_tries=None,
         serial=None):
    tablet = Tablet(mode)
    cfg = Config(wifi_ip=wifi, serial=serial)
    saved = []
    conn = Connection(cfg, factory=tablet, secrets=secrets or Secrets(), sleep=lambda s: None,
                      persist=lambda c: None)
    win = MainWindow(conn, cfg, save_config=saved.append, web_factory=NoWeb)
    win.files_view.show_error = lambda msg: None
    ob = win.onboarding
    if retry_ms is not None:
        ob.retry_timer.setInterval(retry_ms)
    if max_tries is not None:
        ob.max_tries = max_tries
    qtbot.addWidget(win)
    win.resize(1000, 700)
    win.show()
    QApplication.processEvents()
    return win, conn, tablet, ob


def settled(conn, ob):
    return lambda: conn.state != State.CONNECTING and not ob.device_view.connecting


# ---------------------------------------------------------------- at start

def test_shown_at_start_while_offline(qtbot):
    win, conn, tablet, ob = make(qtbot)
    assert win.current_view() == "Connect" and win.stack.currentWidget() is ob
    assert ob.headline.text() == "Looking for your reMarkable…" and ob.spinner.spinning()
    assert ob.sub.text() == f"over USB (10.11.99.1) and Wi-Fi ({WIFI})"
    assert ob.wifi_ip.text() == WIFI  # prefilled from the config
    assert ob.card.width() <= onb.CARD_WIDTH
    c = ob.card.geometry().center()
    assert abs(c.x() - ob.width() / 2) <= 2  # centred
    win.shortcuts["Screen"].activated.emit()  # Ctrl+2: still the connection screen
    assert win.current_view() == "Connect"
    win.toggle_fullscreen()  # F11 can't expand a Screen view that isn't there
    assert win.current_view() == "Connect" and not win.screen_view.fullscreen


def test_without_wifi_it_looks_over_usb_only(qtbot):
    win, conn, tablet, ob = make(qtbot, wifi=None)
    assert ob.sub.text() == "over USB (10.11.99.1)"


def test_switches_to_screen_when_connected(qtbot):
    win, conn, tablet, ob = make(qtbot, mode="ok")
    win.start()  # the app's first connect
    qtbot.waitUntil(lambda: ob.headline.text() == "Connected", timeout=3000)
    assert win.current_view() == "Connect" and not ob.spinner.spinning()
    assert ob.sub.text() == "over USB" and not ob.form.isVisible()
    qtbot.waitUntil(lambda: win.current_view() == "Screen", timeout=3000)  # by itself


def test_connecting_after_try_again(qtbot):
    win, conn, tablet, ob = make(qtbot, mode="down")
    win.start()
    qtbot.waitUntil(settled(conn, ob), timeout=3000)
    QApplication.processEvents()
    tablet.gate = threading.Event()
    ob.password.clear()
    btn = ob.connect_btn if ob.connect_btn.isVisible() else ob.retry_btn
    btn.click()
    qtbot.waitUntil(lambda: ob.headline.text() == "Connecting…", timeout=2000)
    assert ob.spinner.spinning() and not btn.isEnabled()
    tablet.gate.set()
    qtbot.waitUntil(settled(conn, ob), timeout=3000)


# ---------------------------------------------------------------- errors

def test_no_route_says_what_to_do(qtbot):
    win, conn, tablet, ob = make(qtbot, mode="down", secrets=Secrets({"SN1": "pw"}),
                                 serial="SN1")
    win.start()
    qtbot.waitUntil(lambda: ob.headline.text() == onb.NOT_FOUND, timeout=3000)
    assert "USB cable" in ob.advice.text() and "address below" in ob.advice.text()
    assert ob.advice.isVisible() and not ob.spinner.spinning()
    assert ob.sub.text() == onb.RETRY_SOON
    assert not ob.password.isVisible()  # a password is stored
    assert ob.retry_btn.isVisible() and ob.retry_btn.isEnabled()
    assert ob.settings_btn.isVisible()


def test_wifi_ssh_off(qtbot):
    win, conn, tablet, ob = make(qtbot, mode="wifi-off", secrets=Secrets({"SN1": "pw"}),
                                 serial="SN1")
    win.start()
    qtbot.waitUntil(lambda: ob.headline.text() == onb.WIFI_SSH_OFF, timeout=3000)
    assert "rm-ssh-over-wlan on" in ob.advice.text()


def test_host_key_changed(qtbot):
    win, conn, tablet, ob = make(qtbot, mode="hostkey")
    win.start()
    qtbot.waitUntil(lambda: ob.headline.text() == onb.KEY_CHANGED, timeout=3000)
    assert "security key changed" in ob.advice.text() and "Open settings" in ob.advice.text()


def test_unknown_wifi_key(qtbot):
    win, conn, tablet, ob = make(qtbot, mode="unknown-key")
    win.start()
    qtbot.waitUntil(lambda: ob.headline.text() == onb.UNKNOWN_KEY, timeout=3000)
    assert "USB cable" in ob.advice.text() and "trust" in ob.advice.text()


def test_password_path(qtbot):
    secrets = Secrets()
    win, conn, tablet, ob = make(qtbot, mode="auth", secrets=secrets)
    win.start()
    qtbot.waitUntil(lambda: ob.headline.text() == onb.NEED_PASSWORD, timeout=3000)
    assert ob.password.isVisible() and ob.password_hint.text() == PASSWORD_HINT
    assert "Settings → Help → Copyrights and licenses" in ob.password_hint.text()
    assert "ssh-add" in ob.advice.text()  # a key with a passphrase is the other way in
    assert PASSWORD_HINT not in ob.advice.text()  # said once, under the field
    assert ob.password.hasFocus() or not win.isActiveWindow()
    assert ob.connect_btn.isVisible() and not ob.retry_btn.isVisible()
    QTest.keyClicks(ob.password, "nope")
    ob.connect_btn.click()
    qtbot.waitUntil(lambda: ob.headline.text() == onb.WRONG_PASSWORD, timeout=3000)
    assert ob.password.isVisible()
    ob.password.clear()
    QTest.keyClicks(ob.password, "secret")
    QTest.keyClick(ob.password, Qt.Key.Key_Return)  # Enter connects too
    qtbot.waitUntil(lambda: win.current_view() == "Screen", timeout=4000)
    assert conn.state == State.USB
    assert tablet.calls[-1] == (USB_HOST, "secret")  # through Connection / DeviceView
    assert win.device_view.password.text() == "secret"
    assert secrets.store == {"SN123": "secret"}  # kept in the keyring by Connection


def test_wifi_address_is_saved_in_the_config(qtbot):
    win, conn, tablet, ob = make(qtbot, mode="down", wifi=None)
    win.start()
    qtbot.waitUntil(settled(conn, ob), timeout=3000)
    QApplication.processEvents()
    QTest.keyClicks(ob.wifi_ip, "10.0.0.7")
    (ob.connect_btn if ob.connect_btn.isVisible() else ob.retry_btn).click()
    qtbot.waitUntil(lambda: ("10.0.0.7", None) in tablet.calls, timeout=3000)
    assert conn.cfg.wifi_ip == "10.0.0.7" and win.device_view.wifi_ip.text() == "10.0.0.7"


# ---------------------------------------------------------------- retrying

def test_retry_is_timed_and_bounded(qtbot):
    win, conn, tablet, ob = make(qtbot, mode="down", secrets=Secrets({"SN1": "pw"}),
                                 serial="SN1", retry_ms=80, max_tries=3)
    assert ob.retry_timer.isSingleShot()
    win.start()
    qtbot.waitUntil(lambda: tablet.usb_calls() == 4, timeout=4000)  # the first + 3 retries
    qtbot.waitUntil(lambda: ob.sub.text() == onb.STOPPED, timeout=2000)
    qtbot.wait(300)
    assert tablet.usb_calls() == 4 and not ob.retry_timer.isActive()
    ob.retry_btn.click()  # Try again starts over
    qtbot.waitUntil(lambda: tablet.usb_calls() >= 6, timeout=4000)


def test_default_retry_is_five_seconds():
    assert onb.RETRY_MS == 5000 and 0 < onb.MAX_TRIES <= 120


def test_retry_never_overlaps_a_running_connect(qtbot):
    win, conn, tablet, ob = make(qtbot, mode="down", secrets=Secrets({"SN1": "pw"}),
                                 serial="SN1", retry_ms=30)
    tablet.gate = threading.Event()
    win.start()
    qtbot.waitUntil(lambda: tablet.usb_calls() == 1, timeout=2000)
    for _ in range(5):
        ob._auto_retry()  # the timer firing while the connect still runs
        qtbot.wait(40)
    assert tablet.usb_calls() == 1 and len(tablet.calls) == 1
    assert ob.try_now() is False  # Try again too
    tablet.gate.set()
    qtbot.waitUntil(lambda: tablet.usb_calls() >= 2, timeout=3000)  # then it goes on


def test_no_retry_while_a_password_is_being_typed(qtbot):
    win, conn, tablet, ob = make(qtbot, mode="auth", retry_ms=40)
    win.start()
    qtbot.waitUntil(lambda: ob.password.isVisible(), timeout=3000)
    QTest.keyClicks(ob.password, "sec")
    n = tablet.usb_calls()
    qtbot.wait(250)
    assert tablet.usb_calls() == n  # waits for Connect


def test_no_retry_when_not_shown(qtbot):
    win, conn, tablet, ob = make(qtbot, mode="down", retry_ms=40)
    win.show_view("Files")
    win.start()
    qtbot.waitUntil(settled(conn, ob), timeout=3000)
    qtbot.wait(250)
    assert tablet.usb_calls() == 1


# ---------------------------------------------------------------- losing the link

def _connected(qtbot):
    win, conn, tablet, ob = make(qtbot, mode="ok")
    win.start()
    qtbot.waitUntil(lambda: win.current_view() == "Screen", timeout=4000)
    return win, conn, tablet, ob


def test_comes_back_when_the_link_drops_on_screen(qtbot):
    win, conn, tablet, ob = _connected(qtbot)
    tablet.mode = "down"
    conn.connection_lost(DeviceError("socket closed"))
    qtbot.waitUntil(lambda: win.current_view() == "Connect", timeout=2000)
    assert ob.headline.text() == onb.LOST and "keeps looking" in ob.advice.text()


def test_stays_on_files_and_device_when_the_link_drops(qtbot):
    win, conn, tablet, ob = _connected(qtbot)
    tablet.mode = "down"
    for page in ("Files", "Device"):
        win.show_view(page)
        conn.connection_lost(DeviceError("socket closed"))
        qtbot.wait(100)
        assert win.current_view() == page
        conn.connect()
        qtbot.waitUntil(lambda: conn.state == State.OFFLINE, timeout=2000)
        qtbot.wait(50)
        assert win.current_view() == page  # the page's own offline state stays
        tablet.mode = "ok"
        conn.connect()
        qtbot.waitUntil(lambda: conn.state == State.USB, timeout=2000)
        tablet.mode = "down"
    assert win.files_view.pages.currentWidget() is not None


def test_expanded_screen_leaves_expand_when_the_link_drops(qtbot):
    win, conn, tablet, ob = _connected(qtbot)
    win.toggle_fullscreen()
    assert win.screen_view.fullscreen
    tablet.mode = "down"
    conn.connection_lost(DeviceError("socket closed"))
    qtbot.waitUntil(lambda: win.current_view() == "Connect", timeout=2000)
    assert not win.screen_view.fullscreen


# ---------------------------------------------------------------- the way to settings

def test_open_settings_and_back(qtbot):
    win, conn, tablet, ob = make(qtbot)
    assert ob.settings_btn.text() == "Open settings"
    ob.settings_btn.click()
    assert win.current_view() == "Device"
    win.device_view.back_btn.click()  # offline: back to the connection screen
    assert win.current_view() == "Connect"
    win.shortcuts["Files"].activated.emit()
    assert win.current_view() == "Files"


def test_tab_order_and_narrow_window(qtbot):
    win, conn, tablet, ob = make(qtbot, mode="auth")
    win.start()
    qtbot.waitUntil(lambda: ob.password.isVisible(), timeout=3000)
    chain, w = [], ob.password
    while w not in chain:
        chain.append(w)
        w = w.nextInFocusChain()
    want = [ob.password, ob.wifi_ip, ob.connect_btn, ob.settings_btn]
    pos = [chain.index(x) for x in want]
    assert pos == sorted(pos)
    win.resize(700, 600)
    QApplication.processEvents()
    qtbot.wait(30)
    for b in [*ob.findChildren(QPushButton), ob.password, ob.wifi_ip]:
        if b.isVisible():
            r = b.mapTo(win, QPoint(0, 0))
            assert r.x() >= 0 and r.x() + b.width() <= 700 and r.y() + b.height() <= 600, \
                b.text() or b.accessibleName()
            assert b.width() >= b.minimumSizeHint().width()
    assert ob.card.geometry().bottom() < ob.height()
