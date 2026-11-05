"""The window shell: no sidebar; the Screen view is home, with Files and Connection buttons in
its header; Files and Device come back with "← Screen"; Ctrl+1/2/3, F11, Esc."""
import pytest
from PySide6.QtCore import QPoint, Qt, QThreadPool
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFrame, QPushButton, QStackedWidget

from rmdesk.config import Config, load
from rmdesk.connection import Connection, State
from rmdesk.device import DeviceError
from rmdesk.firmware import FirmwareInfo, XOCHITL_DIR
from rmdesk.ui.app import MainWindow
from tests.fakes import FakeDevice


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


class NoWeb:
    def available(self):
        return False


class FwConnection(Connection):
    def _after_connect(self, device, password):
        self.info = FirmwareInfo("3.29.1.0", "SN123", 6)


def _factory(host, password):
    raise DeviceError("down")


def _offline(qtbot):
    cfg = Config()
    conn = Connection(cfg, factory=_factory, secrets=NoSecrets(),
                      sleep=lambda s: None, persist=lambda c: None)
    win = MainWindow(conn, cfg, save_config=lambda c: None, web_factory=NoWeb)
    qtbot.addWidget(win)
    win.show()
    return win, conn


def _win(qtbot, cfg=None):
    """A main window on a connected (fake) tablet: the Screen view shows."""
    dev = FakeDevice({f"{XOCHITL_DIR}/.keep": b""})  # an empty library
    cfg = cfg or Config()
    conn = FwConnection(cfg, factory=lambda h, p: dev, secrets=NoSecrets(),
                        sleep=lambda s: None, persist=lambda c: None)
    conn.connect()
    win = MainWindow(conn, cfg, save_config=lambda c: None, web_factory=NoWeb)
    win.files_view.show_error = lambda msg: None
    qtbot.addWidget(win)
    win.resize(1000, 700)
    win.show()
    QApplication.processEvents()
    return win, conn


def test_main_window_opens_on_the_screen_view(qtbot):
    win, conn = _win(qtbot)
    stack = win.findChild(QStackedWidget, "stack")
    assert stack.currentWidget() is win.screen_view and win.current_view() == "Screen"
    assert not hasattr(win, "sidebar") and not hasattr(win, "nav")
    assert win.findChild(QFrame, "sidebar") is None
    assert win.centralWidget().layout().indexOf(win.stack) == 0  # the stack, then the bar


def test_header_has_files_and_connection_buttons(qtbot):
    win, conn = _win(qtbot)
    s = win.screen_view
    assert s.files_btn.parentWidget() is s.nav_cluster
    assert s.conn_btn.parentWidget() is s.nav_cluster and win.pill is s.conn_btn
    assert s.nav_cluster.parentWidget() is s.header
    assert s.files_btn.accessibleName() == "Files" and not s.files_btn.icon().isNull()
    assert s.conn_btn.label.text() == "USB" and s.conn_btn.state() == "usb"
    # [Files] [Connection], then the battery, Undo and Send at the right end (one row when
    # the window is wide enough)
    win.resize(1200, 700)
    from rmdesk.battery import Battery
    QThreadPool.globalInstance().waitForDone(3000)  # the first battery read (none here)
    QApplication.processEvents()
    s.set_battery(Battery(56, "Discharging"))
    s.compose.add_ink_text("Hi")
    QApplication.processEvents()
    qtbot.wait(50)
    assert s.battery_badge.isVisible() and s.send_cluster.isVisible()
    order = [s.nav_cluster, s.battery_badge, s.send_cluster]
    xs = [w.mapTo(s, QPoint(0, 0)).x() for w in order]
    assert xs == sorted(xs) and len({w.mapTo(s, QPoint(0, 0)).y() // 50 for w in order}) == 1
    s.files_btn.click()
    assert win.stack.currentWidget() is win.files_view
    win.files_view.back_btn.click()
    assert win.stack.currentWidget() is win.screen_view
    QTest.mouseClick(s.conn_btn, Qt.MouseButton.LeftButton)
    assert win.stack.currentWidget() is win.device_view
    win.device_view.back_btn.click()
    assert win.stack.currentWidget() is win.screen_view


def test_back_buttons(qtbot):
    win, _ = _win(qtbot)
    for view in (win.files_view, win.device_view):
        b = view.back_btn
        assert b.text() == "Screen" and not b.icon().isNull()  # ‹ Screen
        assert b.accessibleName() == "Back to Screen"
        assert b.property("variant") == "ghost"
        r = b.mapTo(view, QPoint(0, 0))
        assert r.x() <= 24 and r.y() <= 24  # at the header's left


def test_hover_hints_for_the_new_buttons(qtbot):
    win, conn = _win(qtbot)
    s = win.screen_view
    assert s.hint.text_for(s.files_btn) == "Files on the tablet"
    assert s.hint.text_for(s.conn_btn) == "Connection and settings: USB"
    win._on_state(State.WIFI)
    assert s.hint.text_for(s.conn_btn) == "Connection and settings: Wi-Fi"


def test_connection_button_follows_the_state(qtbot):
    win, conn = _win(qtbot)
    for state, key, text in ((State.USB, "usb", "USB"), (State.WIFI, "wifi", "Wi-Fi"),
                             (State.CONNECTING, "connecting", "Connecting…"),
                             (State.OFFLINE, "offline", "Offline")):
        win._update_pill(state)
        assert win.pill.state() == key and win.pill.label.text() == text
        assert win.pill.dot.property("dot") == key


def test_connection_button_is_a_compact_button(qtbot):
    win, conn = _win(qtbot)
    pill = win.pill
    assert pill.cursor().shape() == Qt.CursorShape.PointingHandCursor
    assert pill.focusPolicy() & Qt.FocusPolicy.TabFocus
    assert pill.height() <= 36 and pill.width() < 140
    assert pill.accessibleName() == "Connection: USB — open device settings"
    assert "fw 3.29.1.0" in pill.toolTip() and "Ctrl+3" in pill.toolTip()
    for k in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
        win.show_view("Screen")
        pill.setFocus(Qt.FocusReason.TabFocusReason)
        QTest.keyClick(pill, k)
        qtbot.waitUntil(lambda: win.stack.currentWidget() is win.device_view, timeout=1000)


def test_shortcuts_switch_views(qtbot):
    win, _ = _win(qtbot)
    stack = win.stack
    for i, name in ((0, "Files"), (2, "Device"), (1, "Screen")):
        assert win.shortcuts[name].key().toString() == f"Ctrl+{i + 1}"
        win.shortcuts[name].activated.emit()
        assert stack.currentIndex() == i and win.current_view() == name


def test_ctrl_keys_from_the_keyboard(qtbot):
    win, _ = _win(qtbot)
    win.activateWindow()
    btn = win.screen_view.files_btn
    btn.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(btn.hasFocus, timeout=1000)
    QTest.keyClick(btn, Qt.Key.Key_3, Qt.KeyboardModifier.ControlModifier)
    assert win.stack.currentWidget() is win.device_view
    QTest.keyClick(win.device_view.back_btn, Qt.Key.Key_1, Qt.KeyboardModifier.ControlModifier)
    assert win.stack.currentWidget() is win.files_view
    QTest.keyClick(win.files_view.back_btn, Qt.Key.Key_2, Qt.KeyboardModifier.ControlModifier)
    assert win.stack.currentWidget() is win.screen_view


def test_narrow_window_no_clipping(qtbot):
    win, _ = _win(qtbot)
    win.resize(700, 600)
    for name in ("Files", "Device", "Screen"):
        win.show_view(name)
        QApplication.processEvents()
        qtbot.wait(30)
        assert win.width() == 700 and win.stack.width() == 700
        view = win.stack.currentWidget()
        for b in view.findChildren(QPushButton):
            if not b.isVisible():
                continue
            r = b.mapTo(win, QPoint(0, 0))
            assert r.x() >= 0 and r.x() + b.width() <= 700, (name, b.text(), b.accessibleName())
    s = win.screen_view
    for w in (s.files_btn, s.conn_btn):
        r = w.mapTo(win, QPoint(0, 0))
        assert r.x() >= 0 and r.x() + w.width() <= 700
        assert w.width() >= w.minimumSizeHint().width()
    assert win.status_bar.height() == 30
    assert win.status_bar.isHidden()  # nothing to say: no empty bar


def test_tab_order_reaches_the_header_buttons(qtbot):
    win, _ = _win(qtbot)
    s = win.screen_view
    chain, w = [], s.control_btn
    while w not in chain:
        chain.append(w)
        w = w.nextInFocusChain()
    want = [s.control_btn, s.full_btn, s.files_btn, s.conn_btn, s.canvas]
    pos = [chain.index(x) for x in want]
    assert pos == sorted(pos)
    for v in (win.files_view, win.device_view):
        chain, w = [], v.back_btn
        while w not in chain:
            chain.append(w)
            w = w.nextInFocusChain()
        assert chain.index(v.back_btn) == 0 and v.back_btn.focusPolicy() & Qt.FocusPolicy.TabFocus
    assert chain.index(win.device_view.password) == 1


def test_status_bar_follows_job(qtbot):
    win, conn = _offline(qtbot)
    assert win.status_label.text() == ""
    assert conn.try_begin_job("upload")
    qtbot.waitUntil(lambda: "upload" in win.status_label.text(), timeout=2000)
    assert win.status_label.text().startswith("The tablet is busy with: upload")
    assert not win.status_bar.isHidden()  # shown while it has a message
    conn.end_job()
    qtbot.waitUntil(lambda: win.status_label.text() == "", timeout=2000)
    assert win.status_bar.isHidden()


def test_old_config_with_sidebar_collapsed_still_loads(qtbot, tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('wifi_ip = "192.168.1.9"\nsidebar_collapsed = true\n', encoding="utf-8")
    cfg = load(p)
    assert cfg.wifi_ip == "192.168.1.9"
    saved = []
    conn = Connection(cfg, factory=_factory, secrets=NoSecrets(), sleep=lambda s: None,
                      persist=lambda c: None)
    win = MainWindow(conn, cfg, save_config=saved.append, web_factory=NoWeb)
    qtbot.addWidget(win)
    win.show()
    assert "Ctrl+B" not in [s.key().toString() for s in win.shortcuts.values()]
    assert saved == []  # nothing about the sidebar is written back


# ---------------------------------------------------------------- Expand (F11), Esc

def test_fullscreen_hides_chrome_and_esc_leaves(qtbot):
    win, _ = _win(qtbot)
    win.show_view("Files")
    win.toggle_fullscreen()  # F11: switches to Screen first
    sv = win.screen_view
    assert win.stack.currentWidget() is sv and sv.fullscreen
    assert not win.status_bar.isVisible() and not sv.header.isVisible()
    assert not sv.files_btn.isVisible() and not sv.conn_btn.isVisible()
    assert not sv.exit_full_btn.isVisible()  # nothing over the picture: Esc or F11 leave
    QTest.keyClick(sv, Qt.Key.Key_Escape)
    assert not sv.fullscreen and win.status_bar.isHidden()
    assert sv.header.isVisible() and sv.files_btn.isVisible() and not sv.exit_full_btn.isVisible()


def test_fullscreen_exit_and_view_switch(qtbot):
    win, _ = _win(qtbot)
    sv = win.screen_view
    sv.full_btn.click()
    assert sv.fullscreen
    win.toggle_fullscreen()  # F11 leaves
    assert not sv.fullscreen
    sv.full_btn.click()
    win.show_view("Device")  # Ctrl+3 leaves fullscreen
    assert not sv.fullscreen


def test_expand_is_control_only_and_keeps_the_window(qtbot):
    win, _ = _win(qtbot)
    sv = win.screen_view
    sv.set_fullscreen(True)
    assert sv.fullscreen and not win.isFullScreen()  # the window itself is not fullscreen
    sv.set_pointer("place")  # Place brings the controls back
    assert not sv.fullscreen and not sv.full_btn.isVisible() and sv.header.isVisible()
    sv.set_fullscreen(True)  # ignored in Place
    assert not sv.fullscreen
    win.toggle_fullscreen()  # F11 goes to Control and expands
    assert sv.fullscreen and sv.pointer == "control" and not win.isFullScreen()


# ---------------------------------------------------------------- Device paths

def test_device_is_still_a_page(qtbot):
    win, _ = _win(qtbot)
    assert win.stack.indexOf(win.device_view) == win._index["Device"]


def test_open_device_paths_still_work(qtbot):
    win, _ = _win(qtbot)
    for emit in (win.files_view.go_device.emit, win.screen_view.open_device.emit,
                 win.screen_view.install_btn.click, win.files_view.offline.button.click,
                 win.files_view.banner.button.click):
        win.show_view("Files")
        emit()
        assert win.stack.currentWidget() is win.device_view


def test_screen_view_shows_no_second_busy_line(qtbot):
    win, conn = _offline(qtbot)
    win.show_view("Device")
    assert conn.try_begin_job("sending to the tablet")
    qtbot.waitUntil(lambda: "sending" in win.status_label.text(), timeout=2000)
    win.stack.setCurrentIndex(win._index["Screen"])  # as when online
    win.refresh_status()
    assert win.status_label.text() == "" and win.status_bar.isHidden()
    conn.end_job()
