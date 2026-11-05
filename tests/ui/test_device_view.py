import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication, QMessageBox

from rmdesk.config import Config
from rmdesk import connection
from rmdesk.connection import Connection
from rmdesk.device import CommandResult
from rmdesk.stream import install
from rmdesk.ui.device_view import DeviceView
from tests.fakes import FakeDevice

SERIAL = "RM110-000-11111"


@pytest.fixture(autouse=True)
def drain_workers(qtbot):
    yield
    QThreadPool.globalInstance().waitForDone(5000)
    QApplication.processEvents()


class NoSecrets:
    def get(self, device_id):
        return None

    def set(self, device_id, password):
        pass


def make_device(version="3.24.0.149", active=False, binary=False):
    files = {"/etc/os-release": f'IMG_VERSION="{version}"\n'.encode(),
             "/sys/devices/soc0/serial_number": SERIAL.encode()}
    if binary:
        files[install.GMS_PATH] = b"bin"
    dev = FakeDevice(files)
    dev.gms_active = active

    def handler(cmd):
        if cmd == "systemctl is-active goMarkableStream":
            return (CommandResult(0, "active\n", "") if dev.gms_active
                    else CommandResult(3, "inactive\n", ""))
        if cmd.startswith("df "):
            return CommandResult(0, "Filesystem Size Used Avail\n/dev/x 7G 1G 6G\n", "")
        return None
    dev.run_handler = handler
    return dev


def make_view(qtbot, dev, cfg=None, **kw):
    cfg = cfg or Config()
    conn = Connection(cfg, factory=lambda h, p: dev, secrets=NoSecrets(),
                      sleep=lambda s: None, persist=lambda c: None)
    saved = []
    view = DeviceView(conn, cfg, save_config=saved.append, **kw)
    qtbot.addWidget(view)
    view.connect_now()
    return view, cfg, saved


def test_stream_row_shows_status(qtbot):
    view, cfg, saved = make_view(qtbot, make_device(active=True, binary=True))
    qtbot.waitUntil(lambda: view.stream_label.text() == "Running", timeout=5000)
    assert view.stream_btn.text() == "Reinstall"
    assert view.stream_uninstall_btn.isVisible() or not view.isVisible()
    assert view.stream_uninstall_btn.isEnabled()
    assert cfg.last_firmware == "3.24.0.149" and saved


def test_stream_missing_offers_install(qtbot):
    view, _, _ = make_view(qtbot, make_device())
    qtbot.waitUntil(lambda: view.stream_label.text() == "Missing", timeout=5000)
    assert view.stream_btn.text() == "Install"
    assert view.stream_btn.isEnabled()
    assert not view.stream_uninstall_btn.isEnabled()
    assert view.stream_msg.text() == ""


def test_firmware_changed_prompt(qtbot):
    view, cfg, _ = make_view(qtbot, make_device(), cfg=Config(last_firmware="3.22.0.1"))
    qtbot.waitUntil(lambda: view.stream_label.text() == "Missing", timeout=5000)
    assert view.stream_msg.text() == "Firmware changed — reinstall screen streaming?"
    assert cfg.last_firmware == "3.24.0.149"


def test_firmware_changed_prompt_when_unit_wiped(qtbot):
    view, _, _ = make_view(qtbot, make_device(binary=True), cfg=Config(last_firmware="3.22.0.1"))
    qtbot.waitUntil(lambda: view.stream_label.text() == "Stopped", timeout=5000)
    assert view.stream_msg.text() == "Firmware changed — reinstall screen streaming?"
    assert view.stream_btn.text() == "Reinstall"


@pytest.mark.parametrize("state", ["activating", "failed"])
def test_stream_row_shows_failed_to_start(qtbot, state):
    dev = make_device(binary=True)
    base = dev.run_handler

    def handler(cmd):
        if cmd == "systemctl is-active goMarkableStream":
            return CommandResult(3, state + "\n", "")
        return base(cmd)
    dev.run_handler = handler
    view, _, _ = make_view(qtbot, dev)
    qtbot.waitUntil(lambda: view.stream_label.text() == "Failed to start", timeout=5000)
    assert view.stream_btn.text() == "Reinstall"
    assert view.stream_uninstall_btn.isEnabled()


def test_no_prompt_when_firmware_unchanged(qtbot):
    view, _, saved = make_view(qtbot, make_device(), cfg=Config(last_firmware="3.24.0.149"))
    qtbot.waitUntil(lambda: view.stream_label.text() == "Missing", timeout=5000)
    assert view.stream_msg.text() == ""
    assert saved == []


def test_old_firmware_cannot_install(qtbot):
    view, _, _ = make_view(qtbot, make_device(version="3.20.0.92"))
    qtbot.waitUntil(lambda: view.stream_label.text() == "Missing", timeout=5000)
    assert not view.stream_btn.isEnabled()
    assert "3.24" in view.stream_msg.text()


def test_install_confirms_tag_and_firmware(qtbot, monkeypatch):
    calls, asked = [], []

    def fake_install(device, tag, downloader, *, serial=None, secrets=None):
        calls.append((tag, serial, downloader))
        dev.gms_active = True
        dev.files[install.GMS_PATH] = b"bin"

    def question(parent, title, text, *a, **k):
        asked.append(text)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(install, "install", fake_install)
    monkeypatch.setattr(QMessageBox, "question", question)
    dev = make_device()
    dl = lambda url: b""  # noqa: E731
    view, _, _ = make_view(qtbot, dev, downloader=dl)
    qtbot.waitUntil(lambda: view.stream_label.text() == "Missing", timeout=5000)
    view.stream_btn.click()
    qtbot.waitUntil(lambda: view.stream_label.text() == "Running", timeout=5000)
    assert calls == [("v1.4.0", SERIAL, dl)]
    assert "v1.4.0" in asked[0] and "3.24.0.149" in asked[0]


def test_install_declined_does_nothing(qtbot, monkeypatch):
    calls = []
    monkeypatch.setattr(install, "install", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    view, _, _ = make_view(qtbot, make_device())
    qtbot.waitUntil(lambda: view.stream_label.text() == "Missing", timeout=5000)
    view.stream_btn.click()
    QThreadPool.globalInstance().waitForDone(2000)
    assert calls == []


def test_install_error_is_shown(qtbot, monkeypatch):
    def boom(*a, **k):
        raise install.InstallError("checksum mismatch")
    monkeypatch.setattr(install, "install", boom)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    view, _, _ = make_view(qtbot, make_device())
    qtbot.waitUntil(lambda: view.stream_label.text() == "Missing", timeout=5000)
    view.stream_btn.click()
    qtbot.waitUntil(lambda: "checksum mismatch" in view.stream_msg.text(), timeout=5000)
    assert view.stream_btn.isEnabled()


def test_uninstall(qtbot, monkeypatch):
    dev = make_device(active=True, binary=True)
    done = []

    def fake_uninstall(device):
        done.append(device)
        dev.gms_active = False
        dev.files.pop(install.GMS_PATH)

    monkeypatch.setattr(install, "uninstall", fake_uninstall)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    view, _, _ = make_view(qtbot, dev)
    qtbot.waitUntil(lambda: view.stream_label.text() == "Running", timeout=5000)
    view.stream_uninstall_btn.click()
    qtbot.waitUntil(lambda: view.stream_label.text() == "Missing", timeout=5000)
    assert done == [dev]


def test_key_connect_failure_without_password_hints_ssh_add(qtbot):
    from rmdesk.device import DeviceError
    cfg = Config()

    def factory(h, p):
        raise DeviceError("Authentication failed.")
    conn = Connection(cfg, factory=factory, secrets=NoSecrets(),
                      sleep=lambda s: None, persist=lambda c: None)
    view = DeviceView(conn, cfg, save_config=lambda c: None)
    qtbot.addWidget(view)
    view.connect_now()
    qtbot.waitUntil(lambda: "Could not connect" in view.message.text(), timeout=5000)
    assert "If your SSH key has a passphrase, run ssh-add first." in view.message.text()


def test_connect_failure_with_password_has_no_ssh_add_hint(qtbot):
    from rmdesk.device import DeviceError
    cfg = Config()

    def factory(h, p):
        raise DeviceError("Authentication failed.")
    conn = Connection(cfg, factory=factory, secrets=NoSecrets(),
                      sleep=lambda s: None, persist=lambda c: None)
    view = DeviceView(conn, cfg, save_config=lambda c: None)
    qtbot.addWidget(view)
    view.password.setText("pw")
    view.connect_now()
    qtbot.waitUntil(lambda: "Could not connect" in view.message.text(), timeout=5000)
    assert "ssh-add" not in view.message.text()


# --- fast USB uploads (USB web interface switch) ---

@pytest.fixture
def no_modal(monkeypatch):
    """A modal box would block an offscreen test forever; record it instead."""
    shown = []
    for name in ("warning", "information", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: shown.append(a[1:3]))
    return shown


import threading

from rmdesk import webui

FAST_CONF = b"[General]\nA=1\nWebInterfaceEnabled=false\nB=2\n"


def fast_device(conf=FAST_CONF):
    dev = make_device()
    if conf is not None:
        dev.files[webui.XOCHITL_CONF] = conf
    return dev


class OkWeb:
    def available(self):
        return True


def fast_view(qtbot, dev, tmp_path, cfg=None, factory=None):
    cfg = cfg or Config()
    conn = Connection(cfg, factory=factory or (lambda h, p: dev), secrets=NoSecrets(),
                      sleep=lambda s: None, persist=lambda c: None)
    view = DeviceView(conn, cfg, save_config=lambda c: None, web_factory=OkWeb,
                      backup_root=tmp_path)
    qtbot.addWidget(view)
    view.connect_now()
    return view


def test_fast_uploads_off_over_usb(qtbot, tmp_path):
    view = fast_view(qtbot, fast_device(), tmp_path)
    qtbot.waitUntil(lambda: view.fast_label.text() == "Off", timeout=5000)
    assert view.fast_btn.text() == "Turn on"
    assert view.fast_btn.isEnabled()
    assert view.fast_btn.toolTip() == ""


def test_fast_uploads_on_offers_turn_off(qtbot, tmp_path):
    conf = FAST_CONF.replace(b"=false", b"=true")
    view = fast_view(qtbot, fast_device(conf), tmp_path)
    qtbot.waitUntil(lambda: view.fast_label.text() == "On", timeout=5000)
    assert view.fast_btn.text() == "Turn off"
    assert view.fast_btn.isEnabled()


def test_fast_uploads_unknown_without_key(qtbot, tmp_path):
    view = fast_view(qtbot, fast_device(b"[General]\nA=1\n"), tmp_path)
    qtbot.waitUntil(lambda: view.fast_label.text() == "Unknown", timeout=5000)
    assert not view.fast_btn.isEnabled()


def test_fast_uploads_turn_on_disabled_over_wifi(qtbot, tmp_path):
    dev = fast_device()

    def factory(host, password):
        if host == "10.11.99.1":
            raise OSError("no usb")
        return dev
    view = fast_view(qtbot, dev, tmp_path, cfg=Config(wifi_ip="192.168.1.50"),
                     factory=factory)
    qtbot.waitUntil(lambda: view.fast_label.text() == "Off", timeout=5000)
    assert view.conn.transport == "wifi"
    assert view.fast_btn.text() == "Turn on"
    assert not view.fast_btn.isEnabled()
    assert view.fast_btn.toolTip() == "Connect over USB to change this."


def test_fast_uploads_declined_does_nothing(qtbot, tmp_path, monkeypatch):
    dev = fast_device()
    view = fast_view(qtbot, dev, tmp_path)
    qtbot.waitUntil(lambda: view.fast_label.text() == "Off", timeout=5000)
    calls, asked = [], []
    monkeypatch.setattr(webui, "set_enabled", lambda *a, **k: calls.append(1))

    def question(parent, title, text, *a, **k):
        asked.append(text)
        return QMessageBox.StandardButton.No
    monkeypatch.setattr(QMessageBox, "question", question)
    view.fast_btn.click()
    QThreadPool.globalInstance().waitForDone(5000)
    QApplication.processEvents()
    assert calls == []
    assert asked == ["rmdesk will turn on the tablet's USB web interface. The tablet's "
                     "screen restarts once (a few seconds). After that, USB uploads won't "
                     "restart it. Continue?"]
    assert dev.files[webui.XOCHITL_CONF] == FAST_CONF
    assert view.fast_label.text() == "Off"


def test_fast_uploads_confirmed_runs_in_worker(qtbot, tmp_path, monkeypatch):
    dev = fast_device()
    view = fast_view(qtbot, dev, tmp_path)
    qtbot.waitUntil(lambda: view.fast_label.text() == "Off", timeout=5000)
    calls = []
    real = webui.set_enabled

    def spy(device, enabled, **kw):
        calls.append((threading.current_thread() is threading.main_thread(), enabled, kw))
        real(device, enabled, sleep=lambda s: None, backup_root=kw["backup_root"],
             check_web=kw["check_web"])
    monkeypatch.setattr(webui, "set_enabled", spy)
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes)
    changed = []
    view.fast_uploads_changed.connect(lambda: changed.append(1))
    view.fast_btn.click()
    qtbot.waitUntil(lambda: view.fast_label.text() == "On", timeout=5000)
    (on_main, enabled, kw), = calls
    assert not on_main and enabled is True
    assert kw["backup_root"] == tmp_path
    assert kw["check_web"]() is True
    assert dev.files[webui.XOCHITL_CONF] == FAST_CONF.replace(b"=false", b"=true")
    assert view.fast_btn.text() == "Turn off"
    assert "on" in view.message.text()
    assert changed == [1]


def test_fast_uploads_turn_off_confirm_text(qtbot, tmp_path, monkeypatch):
    dev = fast_device(FAST_CONF.replace(b"=false", b"=true"))
    view = fast_view(qtbot, dev, tmp_path)
    qtbot.waitUntil(lambda: view.fast_label.text() == "On", timeout=5000)
    asked = []
    monkeypatch.setattr(webui, "set_enabled", lambda *a, **k: None)

    def question(parent, title, text, *a, **k):
        asked.append(text)
        return QMessageBox.StandardButton.No
    monkeypatch.setattr(QMessageBox, "question", question)
    view.fast_btn.click()
    assert asked and "turn off the tablet's USB web interface" in asked[0]


def test_fast_uploads_error_shown_and_state_reloaded(qtbot, tmp_path, monkeypatch):
    dev = fast_device()
    view = fast_view(qtbot, dev, tmp_path)
    qtbot.waitUntil(lambda: view.fast_label.text() == "Off", timeout=5000)

    def fake(device, enabled, **kw):
        device.files[webui.XOCHITL_CONF] = FAST_CONF.replace(b"=false", b"=true")
        raise webui.WebUiError(webui.WEB_NO_ANSWER)
    monkeypatch.setattr(webui, "set_enabled", fake)
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes)
    view.fast_btn.click()
    qtbot.waitUntil(lambda: view.fast_label.text() == "On", timeout=5000)
    assert webui.WEB_NO_ANSWER in view.message.text()
    assert view.conn.device is dev  # a WebUiError does not drop the link



def test_fast_uploads_turn_off_disabled_over_wifi(qtbot, tmp_path):
    dev = fast_device(FAST_CONF.replace(b"=false", b"=true"))

    def factory(host, password):
        if host == "10.11.99.1":
            raise OSError("no usb")
        return dev
    view = fast_view(qtbot, dev, tmp_path, cfg=Config(wifi_ip="192.168.1.50"),
                     factory=factory)
    qtbot.waitUntil(lambda: view.fast_label.text() == "On", timeout=5000)
    assert view.fast_btn.text() == "Turn off"
    assert not view.fast_btn.isEnabled()
    assert view.fast_btn.toolTip() == "Connect over USB to change this."


def test_fast_uploads_refused_while_other_job_runs(qtbot, no_modal, tmp_path, monkeypatch):
    view = fast_view(qtbot, fast_device(), tmp_path)
    qtbot.waitUntil(lambda: view.fast_label.text() == "Off", timeout=5000)
    calls, asked = [], []
    monkeypatch.setattr(webui, "set_enabled", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: asked.append(1) or QMessageBox.StandardButton.Yes)
    assert view.conn.try_begin_job("uploading files")
    view.fast_btn.click()
    QThreadPool.globalInstance().waitForDone(5000)
    QApplication.processEvents()
    assert calls == [] and asked == []
    assert view.message.text() == ("The tablet is busy with: uploading files. "
                                   "Try again when it's done.")
    assert view.conn.current_job == "uploading files"


@pytest.mark.parametrize("fail", [False, True])
def test_fast_uploads_releases_job(qtbot, no_modal, tmp_path, monkeypatch, fail):
    view = fast_view(qtbot, fast_device(), tmp_path)
    qtbot.waitUntil(lambda: view.fast_label.text() == "Off", timeout=5000)
    seen = []

    def fake(device, enabled, **kw):
        seen.append(view.conn.current_job)
        if fail:
            raise webui.WebUiError("nope")
    monkeypatch.setattr(webui, "set_enabled", fake)
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes)
    view.fast_btn.click()
    qtbot.waitUntil(lambda: bool(seen) and not view._fast_busy, timeout=5000)
    QThreadPool.globalInstance().waitForDone(5000)
    QApplication.processEvents()
    assert seen == ["changing fast USB uploads"]
    assert view.conn.current_job is None


@pytest.mark.parametrize("fail", [False, True])
def test_stream_install_holds_and_releases_job(qtbot, no_modal, monkeypatch, fail):
    seen = []

    def fake_install(dev, tag, downloader, **kw):
        seen.append(view.conn.current_job)
        if fail:
            raise install.InstallError("bad")
    monkeypatch.setattr(install, "install", fake_install)
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes)
    view, _, _ = make_view(qtbot, make_device())
    qtbot.waitUntil(lambda: view.stream_label.text() == "Missing", timeout=5000)
    view.stream_btn.click()
    qtbot.waitUntil(lambda: bool(seen) and not view._stream_loading, timeout=5000)
    assert seen == ["installing screen streaming"]
    assert view.conn.current_job is None


def test_stream_install_refused_while_other_job_runs(qtbot, no_modal, monkeypatch):
    calls = []
    monkeypatch.setattr(install, "install", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes)
    view, _, _ = make_view(qtbot, make_device())
    qtbot.waitUntil(lambda: view.stream_label.text() == "Missing", timeout=5000)
    view.conn.try_begin_job("sending to a notebook")
    view.stream_btn.click()
    QThreadPool.globalInstance().waitForDone(5000)
    assert calls == []
    assert "busy with: sending to a notebook" in view.stream_msg.text()


def test_fast_uploads_toggle_clears_pending_refresh(qtbot, tmp_path, monkeypatch):
    dev = fast_device()
    view = fast_view(qtbot, dev, tmp_path)
    qtbot.waitUntil(lambda: view.fast_label.text() == "Off", timeout=5000)
    view.conn.add_pending(4)
    real = webui.set_enabled
    monkeypatch.setattr(webui, "set_enabled", lambda d, e, **kw: real(
        d, e, sleep=lambda s: None, backup_root=kw["backup_root"], check_web=kw["check_web"]))
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes)
    view.fast_btn.click()
    qtbot.waitUntil(lambda: view.fast_label.text() == "On", timeout=5000)
    assert view.conn.pending_refresh == 0


# -- redesign: chips, offline state, layout --
def _chip(view):
    return view.stream_label.state(), view.stream_label.text()


def test_stream_chip_running_and_missing(qtbot):
    view, _, _ = make_view(qtbot, make_device(active=True, binary=True))
    qtbot.waitUntil(lambda: view.stream_label.text() == "Running", timeout=5000)
    assert _chip(view) == ("ok", "Running")
    view2, _, _ = make_view(qtbot, make_device())
    qtbot.waitUntil(lambda: view2.stream_label.text() == "Missing", timeout=5000)
    assert _chip(view2) == ("warn", "Missing")


def test_stream_chip_stopped(qtbot):
    view, _, _ = make_view(qtbot, make_device(binary=True))
    qtbot.waitUntil(lambda: view.stream_label.text() == "Stopped", timeout=5000)
    assert _chip(view) == ("neutral", "Stopped")


def test_stream_chip_failed(qtbot):
    dev = make_device(binary=True)
    base = dev.run_handler
    dev.run_handler = lambda cmd: (CommandResult(3, "failed\n", "")
                                   if cmd == "systemctl is-active goMarkableStream" else base(cmd))
    view, _, _ = make_view(qtbot, dev)
    qtbot.waitUntil(lambda: view.stream_label.text() == "Failed to start", timeout=5000)
    assert _chip(view) == ("err", "Failed to start")


def test_fast_chip_on_off(qtbot, tmp_path):
    view = fast_view(qtbot, fast_device(), tmp_path)
    qtbot.waitUntil(lambda: view.fast_label.text() == "Off", timeout=5000)
    assert view.fast_label.state() == "neutral"
    view2 = fast_view(qtbot, fast_device(FAST_CONF.replace(b"=false", b"=true")), tmp_path)
    qtbot.waitUntil(lambda: view2.fast_label.text() == "On", timeout=5000)
    assert view2.fast_label.state() == "ok"


def test_tablet_info_and_storage(qtbot):
    view, _, _ = make_view(qtbot, make_device())
    qtbot.waitUntil(lambda: "free of" in view.free_label.text(), timeout=5000)
    assert view.free_label.text() == "6 GB free of 7 GB"
    assert view.storage_bar.value() == 14
    assert view.serial_label.text() == SERIAL
    assert view.serial_label.property("role") == "mono"
    assert view.ip_label.text() == "USB · 10.11.99.1"
    assert view.connect_btn.property("variant") == "primary"


def test_offline_state_renders_with_action(qtbot):
    def boom(host, pw):
        raise OSError("no route")
    cfg = Config()
    conn = Connection(cfg, factory=boom, secrets=NoSecrets(), sleep=lambda s: None,
                      persist=lambda c: None)
    view = DeviceView(conn, cfg, save_config=lambda c: None)
    qtbot.addWidget(view)
    view.resize(1000, 700)
    view.show()
    view.refresh()
    assert view.offline.isVisible() and not view.info_box.isVisible()
    assert view.offline.button.text() == "Try again"
    assert not view.stream_btn.isEnabled() and not view.fast_btn.isEnabled()
    clicked = []
    view.connect_now = lambda: clicked.append(1)
    view.offline.action.disconnect()
    view.offline.action.connect(view.connect_now)
    view.offline.button.click()
    assert clicked == [1]


def test_offline_hidden_when_connected(qtbot):
    view, _, _ = make_view(qtbot, make_device())
    view.show()
    qtbot.waitUntil(lambda: view.info_box.isVisible(), timeout=5000)
    assert not view.offline.isVisible()


def test_grid_columns_follow_width(qtbot):
    view, _, _ = make_view(qtbot, make_device())
    view.show()
    view.resize(1280 - 208, 800)
    qtbot.waitUntil(lambda: view._grid.columns() == 2, timeout=2000)
    qtbot.waitUntil(lambda: view.cards[0].x() < view.cards[1].x(), timeout=2000)
    assert view.cards[0].y() == view.cards[1].y()
    view.resize(700 - 208, 800)
    qtbot.waitUntil(lambda: view._grid.columns() == 1, timeout=2000)


def test_narrow_window_no_clipping(qtbot):
    from PySide6.QtWidgets import QPushButton
    view, _, _ = make_view(qtbot, make_device(active=True, binary=True))
    view.resize(700 - 208, 800)  # the area left of the sidebar in a 700 px window
    view.show()
    qtbot.waitUntil(lambda: view.stream_label.text() == "Running", timeout=5000)
    qtbot.wait(50)
    assert view.minimumSizeHint().width() <= view.width()
    assert not view._grid.parentWidget().parentWidget().horizontalScrollBar().isVisible()
    for w in (view.password, view.wifi_ip, view.connect_btn, view.key_btn, view.stream_btn,
              view.stream_uninstall_btn, view.fast_btn, view.storage_bar):
        r = w.mapTo(view, w.rect().topLeft())
        assert r.x() >= 0 and r.x() + w.width() <= view.width(), w
        if isinstance(w, QPushButton):
            assert w.width() >= w.minimumSizeHint().width(), w


def test_tab_order(qtbot):
    view, _, _ = make_view(qtbot, make_device(active=True, binary=True))
    view.show()
    qtbot.waitUntil(lambda: view.stream_label.text() == "Running", timeout=5000)
    chain, w = [], view.password
    while w not in chain:
        chain.append(w)
        w = w.nextInFocusChain()
    want = [view.password, view.wifi_ip, view.connect_btn, view.key_btn, view.stream_btn,
            view.stream_uninstall_btn]
    pos = [chain.index(x) for x in want]
    assert pos == sorted(pos)


def test_connect_disabled_while_a_job_runs(qtbot):
    view, cfg, _ = make_view(qtbot, make_device())
    qtbot.waitUntil(lambda: view.connect_btn.isEnabled(), timeout=5000)
    assert view.conn.try_begin_job("sending to the tablet")
    qtbot.waitUntil(lambda: not view.connect_btn.isEnabled(), timeout=5000)
    dev = view.conn.device
    view.connect_now()  # e.g. a stray call: refused, the job's device stays open
    assert view.conn.device is dev
    assert "busy with: sending to the tablet" in view.message.text()
    view.conn.end_job()
    qtbot.waitUntil(lambda: view.connect_btn.isEnabled(), timeout=5000)


def test_reconnect_that_restarted_the_screen_tells_the_user(qtbot):
    dev = make_device()
    dev.xochitl_active = False
    view, cfg, _ = make_view(qtbot, dev)
    qtbot.waitUntil(lambda: connection.SCREEN_RESTARTED in view.message.text(), timeout=5000)
    assert dev.xochitl_active


def test_ask_before_writing_setting(qtbot):
    from rmdesk.config import Config
    from rmdesk.connection import Connection
    from rmdesk.ui.device_view import DeviceView
    cfg = Config()
    saved = []
    conn = Connection(cfg, factory=lambda h, p: None, secrets=NoSecrets(), sleep=lambda s: None,
                      persist=lambda c: None)
    view = DeviceView(conn, cfg, save_config=lambda c: saved.append(c.confirm_sends))
    qtbot.addWidget(view)
    assert not view.confirm_check.isChecked()  # off by default: Send sends
    view.confirm_check.setChecked(True)
    assert cfg.confirm_sends is True and saved == [True]
