import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path

import numpy as np
import pytest
from rmdesk.stream import install as gms
from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, QThreadPool
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from rmdesk.config import Config
from rmdesk.connection import Connection, State
from rmdesk.firmware import FirmwareInfo
from rmdesk.stream.client import Frame, StreamError
from rmdesk.ui import screen_view as sv
from rmdesk.ui.screen_view import PresentWindow, ScreenView, crop_rect
from tests.fakes import FakeDevice

MAIN = threading.main_thread()


@pytest.fixture(autouse=True)
def drain_workers(qtbot):
    yield
    QThreadPool.globalInstance().waitForDone(5000)
    QApplication.processEvents()


class NoSecrets:
    def get(self, k):
        return None

    def set(self, k, v):
        pass


class Creds:
    def __init__(self, value="admin:pw"):
        self.value = value

    def get(self, serial):
        return self.value

    def set(self, k, v):
        pass


class FakeClient:
    instances: list = []

    def __init__(self, host, password, on_frame, *, username, dial=None, on_error=None):
        self.host, self.password, self.username = host, password, username
        self.on_frame, self.on_error = on_frame, on_error
        self.dial = dial
        self.calls: list = []
        self.alive = False
        FakeClient.instances.append(self)

    def start(self):
        self.calls.append(("start", threading.current_thread()))
        self.alive = True

    def stop(self):
        self.calls.append(("stop", threading.current_thread()))
        self.alive = False

    def is_alive(self):
        return self.alive

    def resync(self):
        self.calls.append(("resync", threading.current_thread()))

    def resyncs(self):
        return self.names().count("resync")

    def names(self):
        return [c[0] for c in self.calls]


class FakePen(FakeClient):
    instances: list = []

    def __init__(self, host, password, on_pen, *, username, dial=None, on_error=None):
        self.host, self.password, self.username = host, password, username
        self.on_pen = on_pen
        self.calls = []
        self.alive = False
        FakePen.instances.append(self)


@pytest.fixture(autouse=True)
def reset_fakes():
    FakeClient.instances = []
    FakePen.instances = []


class FwConnection(Connection):
    def _after_connect(self, device, password):
        self.info = FirmwareInfo("3.24.0.100", "SN123", None)


def make_conn():
    conn = FwConnection(Config(), factory=lambda h, p: FakeDevice({}), secrets=NoSecrets(),
                      sleep=lambda s: None, persist=lambda c: None)
    conn.connect()
    conn.info = FirmwareInfo("3.24.0.100", "SN123", None)
    return conn


def make_view(qtbot, tmp_path, conn=None, status="running", secrets=None, version="3.24.0.100",
              catchup_delay=60.0, keep_interval=1.0, restart_fn=None, refresh_timeout=15.0):
    conn = conn or make_conn()
    conn.info = FirmwareInfo(version, "SN123", None)
    kw = {} if restart_fn is None else {"restart_fn": restart_fn}
    view = ScreenView(conn, keep_default=False, control_default=False, client_factory=FakeClient, pen_factory=FakePen,
                      status_fn=lambda dev: status,
                      secrets=secrets or Creds(), pictures_dir=tmp_path / "Pictures",
                      catchup_delay=catchup_delay, keep_interval=keep_interval,
                      refresh_timeout=refresh_timeout, **kw)
    qtbot.addWidget(view)
    return view, conn


def started(cls=FakeClient):
    return [c for c in cls.instances if c.alive]


def frame(value=255, w=40, h=60):
    return Frame(w, h, np.full((h, w), value, np.uint8))


def show_and_wait(qtbot, view):
    view.show()
    qtbot.waitUntil(lambda: len(started()) == 1, timeout=5000)
    c = started()[0]
    # `alive` flips in the worker; refresh/catch-up need the GUI side (`_started`) to have run
    # too, so wait for it (bounded) instead of racing it
    qtbot.waitUntil(lambda: view._stream.handle is c and view._streaming() is c, timeout=5000)
    return c


# ---------------------------------------------------------------- crop_rect

def test_crop_rect_maps_to_frame():
    r = crop_rect(QRectF(0, 0, 702, 936), QSize(1404, 1872), QRectF(100, 100, 200, 200))
    assert r == QRect(200, 200, 400, 400)


def test_crop_rect_letterboxed_and_clamped():
    # a 100x200 frame in a 400x200 window is centred: x offset 150, scale 1
    r = crop_rect(QRectF(0, 0, 400, 200), QSize(100, 200), QRectF(100, 10, 120, 50))
    assert r == QRect(0, 10, 70, 50)


@pytest.mark.parametrize("rot", [0, 90, 180, 270])
def test_crop_rect_inverts_rotation(rot):
    # frame 100x200 (w x h); pick a frame-space box, render its rotated drag, expect it back
    frame_size = QSize(100, 200)
    box = QRect(10, 20, 30, 40)
    view = QRectF(0, 0, 200, 200) if rot in (90, 270) else QRectF(0, 0, 100, 200)
    g = sv.Geometry(view, QRect(0, 0, 100, 200), rot)
    drag = QRectF(g.to_view(QPointF(box.left(), box.top())),
                  g.to_view(QPointF(box.right() + 1, box.bottom() + 1))).normalized()
    assert crop_rect(view, frame_size, drag, rotation=rot) == box


def test_crop_composes_with_existing_crop():
    r = crop_rect(QRectF(0, 0, 100, 100), QSize(200, 200), QRectF(0, 0, 50, 50),
                  region=QRect(100, 100, 100, 100))
    assert r == QRect(100, 100, 50, 50)


# ---------------------------------------------------------------- lifecycle

def test_stream_starts_on_show_and_stops_on_hide(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    assert FakeClient.instances == []
    c = show_and_wait(qtbot, view)
    assert (c.host, c.password, c.username) == ("10.11.99.1", "pw", "admin")
    view.hide()
    qtbot.waitUntil(lambda: not c.alive, timeout=5000)
    assert c.names() == ["start", "stop"]


def test_start_and_stop_run_in_workers(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    c = show_and_wait(qtbot, view)
    view.hide()
    qtbot.waitUntil(lambda: not c.alive, timeout=5000)
    assert [t for _, t in c.calls if t is MAIN] == []


def test_stream_runs_while_present_visible_and_screen_hidden(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    c = show_and_wait(qtbot, view)
    view.present_btn.click()
    pw = view.present
    qtbot.addWidget(pw)
    view.hide()
    QThreadPool.globalInstance().waitForDone(5000)
    QApplication.processEvents()
    assert c.alive and c.names() == ["start"]
    pw.hide()
    qtbot.waitUntil(lambda: not c.alive, timeout=5000)
    assert c.names() == ["start", "stop"]


def test_closing_present_does_not_stop_visible_screen_view(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    c = show_and_wait(qtbot, view)
    view.present_btn.click()
    view.present.close()
    QThreadPool.globalInstance().waitForDone(5000)
    QApplication.processEvents()
    assert c.alive and c.names() == ["start"]


def test_offline_stops_stream_and_reconnect_restarts(qtbot, tmp_path):
    view, conn = make_view(qtbot, tmp_path)
    c = show_and_wait(qtbot, view)
    conn.disconnect()
    qtbot.waitUntil(lambda: not c.alive, timeout=5000)
    assert "not connected" in view.status.text().lower()
    conn.connect()
    qtbot.waitUntil(lambda: len(started()) == 1, timeout=5000)


# ---------------------------------------------------------------- frames

def test_frame_drawn_and_waiting_message(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    c = show_and_wait(qtbot, view)
    assert "Waiting for the tablet" in view.status.text()
    c.on_frame(frame(100))
    qtbot.waitUntil(lambda: view.canvas.frame is not None, timeout=2000)
    assert view.status.text() == ""
    img = view.canvas.grab().toImage()
    assert img.pixelColor(img.width() // 2, img.height() // 2).red() == 100


def test_frames_drop_to_latest_under_load(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    c = show_and_wait(qtbot, view)
    seen = []
    view.frame_changed.connect(lambda: seen.append(view.frame))
    t = threading.Thread(target=lambda: [c.on_frame(frame(i)) for i in range(200)])
    t.start()
    t.join()
    QApplication.processEvents()
    assert int(view.frame.gray[0, 0]) == 199
    assert len(seen) < 5  # not one per frame; the GUI saw only the freshest


def test_rotation_cycles_and_applies(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    for expect in (90, 180, 270, 0):
        view.rotate_btn.click()
        assert view.rotation == expect
    assert sv.rotated_size(QSize(10, 20), 90) == QSize(20, 10)


def test_screenshot_saved_under_pictures(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    c = show_and_wait(qtbot, view)
    c.on_frame(frame(128, 30, 50))
    qtbot.waitUntil(lambda: view.frame is not None, timeout=2000)
    path = view.screenshot()
    assert path.parent == tmp_path / "Pictures" and path.exists()
    assert path.name.startswith("rmdesk-") and path.suffix == ".png"
    assert QImage(str(path)).size() == QSize(30, 50)
    view.rotate_btn.click()
    assert QImage(str(view.screenshot())).size() == QSize(50, 30)


def test_screenshot_without_frame_says_so(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    assert view.screenshot() is None
    assert "no frame" in view.status.text().lower()


# ---------------------------------------------------------------- errors / install state

def test_auth_failure_message(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    c = show_and_wait(qtbot, view)
    c.on_error(StreamError("login failed: HTTP 401", 401))
    qtbot.waitUntil(lambda: "login failed" in view.status.text(), timeout=2000)
    assert view.status.text() == ("Screen streaming login failed — "
                                  "reinstall screen streaming in Device.")


def test_other_error_message_then_cleared_by_frame(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    c = show_and_wait(qtbot, view)
    c.on_error(ConnectionRefusedError("refused"))
    qtbot.waitUntil(lambda: "refused" in view.status.text(), timeout=2000)
    c.on_frame(frame())
    qtbot.waitUntil(lambda: view.status.text() == "", timeout=2000)


def test_not_installed_shows_button_to_device(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path, status="missing")
    hits = []
    view.open_device.connect(lambda: hits.append(1))
    view.show()
    qtbot.waitUntil(lambda: not view.install_btn.isHidden(), timeout=5000)
    assert "Install screen streaming in Device" in view.status.text()
    assert FakeClient.instances == []
    view.install_btn.click()
    assert hits == [1]


def test_unsupported_firmware_shows_button(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path, version="3.5.2.1")
    view.show()
    qtbot.waitUntil(lambda: not view.install_btn.isHidden(), timeout=5000)
    assert "3.24" in view.status.text()
    assert FakeClient.instances == []


def test_no_stored_credentials_counts_as_not_installed(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path, secrets=NoSecrets())
    view.show()
    qtbot.waitUntil(lambda: not view.install_btn.isHidden(), timeout=5000)
    assert FakeClient.instances == []


def test_wifi_host_used(qtbot, tmp_path):
    cfg = Config()
    cfg.wifi_ip = "192.168.1.50"

    def factory(h, p):
        if h == "10.11.99.1":
            raise OSError("no usb")
        return FakeDevice({})
    conn = FwConnection(cfg, factory=factory, secrets=NoSecrets(), sleep=lambda s: None,
                      persist=lambda c: None)
    conn.connect()
    view, _ = make_view(qtbot, tmp_path, conn=conn)
    c = show_and_wait(qtbot, view)
    assert c.host == "192.168.1.50"


def test_stream_dials_through_the_ssh_connection(qtbot, tmp_path):
    view, conn = make_view(qtbot, tmp_path)
    tunnels = []
    conn.device.open_tunnel = lambda port, timeout=10: tunnels.append((port, timeout)) or "sock"
    c = show_and_wait(qtbot, view)
    assert c.dial is not None
    assert c.dial(5.0) == "sock"
    assert tunnels == [(2001, 5.0)]  # goMarkableStream on the tablet's loopback


def test_stream_start_moves_an_old_install_to_loopback(qtbot, tmp_path):
    conn = make_conn()
    conn.connect()
    conn.info = FirmwareInfo("3.24.0.100", "SN123", None)
    seen = []
    view = ScreenView(conn, keep_default=False, control_default=False,
                      client_factory=FakeClient, pen_factory=FakePen,
                      status_fn=lambda dev: "running", local_only_fn=seen.append,
                      secrets=Creds(), pictures_dir=tmp_path / "Pictures")
    qtbot.addWidget(view)
    show_and_wait(qtbot, view)
    assert seen == [conn.device]


def test_dial_when_offline_raises(qtbot, tmp_path):
    from rmdesk.stream.client import StreamError
    view, conn = make_view(qtbot, tmp_path)
    conn.device = None
    with pytest.raises(StreamError):
        view._dial(1.0)


# ---------------------------------------------------------------- present window

def make_present(qtbot, view):
    view.present_btn.click()
    pw = view.present
    qtbot.addWidget(pw)
    return pw


def test_present_window_title_and_frameless(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    pw = make_present(qtbot, view)
    assert pw.windowTitle() == "rmdesk — Present"
    assert pw.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert pw.isVisible()


def test_invert_toggle(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    c = show_and_wait(qtbot, view)
    pw = make_present(qtbot, view)
    pw.resize(200, 300)
    c.on_frame(frame(255))
    qtbot.waitUntil(lambda: pw.frame is not None, timeout=2000)

    def centre():
        img = pw.grab().toImage()
        return img.pixelColor(img.width() // 2, img.height() // 2).red()

    assert centre() == 255
    QTest.keyClick(pw, Qt.Key.Key_I)
    assert centre() == 0
    QTest.keyClick(pw, Qt.Key.Key_I)
    assert centre() == 255


def test_present_shares_frames_and_client(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    c = show_and_wait(qtbot, view)
    pw = make_present(qtbot, view)
    c.on_frame(frame(7))
    qtbot.waitUntil(lambda: pw.frame is not None, timeout=2000)
    assert pw.frame is view.frame
    assert len(FakeClient.instances) == 1


def test_drag_crops_and_escape_resets(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    c = show_and_wait(qtbot, view)
    pw = make_present(qtbot, view)
    pw.resize(200, 300)  # frame 40x60 -> scale 5
    c.on_frame(frame(255, 40, 60))
    qtbot.waitUntil(lambda: pw.frame is not None, timeout=2000)
    QTest.mousePress(pw, Qt.MouseButton.LeftButton, pos=QPointF(50, 50).toPoint())
    QTest.mouseMove(pw, QPointF(150, 150).toPoint())
    QTest.mouseRelease(pw, Qt.MouseButton.LeftButton, pos=QPointF(150, 150).toPoint())
    assert pw.crop == QRect(10, 10, 20, 20)
    QTest.keyClick(pw, Qt.Key.Key_Escape)
    assert pw.crop is None


def test_tiny_drag_is_ignored(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    c = show_and_wait(qtbot, view)
    pw = make_present(qtbot, view)
    pw.resize(200, 300)
    c.on_frame(frame())
    qtbot.waitUntil(lambda: pw.frame is not None, timeout=2000)
    QTest.mouseClick(pw, Qt.MouseButton.LeftButton, pos=QPointF(50, 50).toPoint())
    assert pw.crop is None


def test_fullscreen_key_toggles(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    pw = make_present(qtbot, view)
    QTest.keyClick(pw, Qt.Key.Key_F)
    assert pw.windowState() & Qt.WindowState.WindowFullScreen
    QTest.keyClick(pw, Qt.Key.Key_F)
    assert not pw.windowState() & Qt.WindowState.WindowFullScreen


def test_pen_dot_toggle_starts_event_client_and_draws(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    c = show_and_wait(qtbot, view)
    pw = make_present(qtbot, view)
    pw.resize(200, 300)
    c.on_frame(frame(255, 40, 60))
    qtbot.waitUntil(lambda: pw.frame is not None, timeout=2000)
    assert FakePen.instances == []
    QTest.keyClick(pw, Qt.Key.Key_D)
    qtbot.waitUntil(lambda: len(started(FakePen)) == 1, timeout=5000)
    pen = started(FakePen)[0]
    assert (pen.host, pen.password) == ("10.11.99.1", "pw")
    pen.on_pen(20.0, 30.0, True)  # frame centre
    qtbot.waitUntil(lambda: view.pen is not None, timeout=2000)
    img = pw.grab().toImage()
    col = img.pixelColor(100, 150)
    assert col.red() > 200 and col.green() < 80
    pen.on_pen(20.0, 30.0, False)
    qtbot.waitUntil(lambda: view.pen is None, timeout=2000)
    QTest.keyClick(pw, Qt.Key.Key_D)
    qtbot.waitUntil(lambda: not pen.alive, timeout=5000)


def test_pen_client_stops_when_present_hidden(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    show_and_wait(qtbot, view)
    pw = make_present(qtbot, view)
    QTest.keyClick(pw, Qt.Key.Key_D)
    qtbot.waitUntil(lambda: len(started(FakePen)) == 1, timeout=5000)
    pw.hide()
    qtbot.waitUntil(lambda: not started(FakePen), timeout=5000)


def test_registered_in_main_window(qtbot):
    from rmdesk.ui.app import MainWindow
    win = MainWindow(make_conn(), Config(), save_config=lambda c: None)
    qtbot.addWidget(win)
    assert isinstance(win.stack.widget(win._index["Screen"]), ScreenView)


def test_newer_frame_always_painted_even_if_ids_collide(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    view.resize(200, 300)
    view.frame = frame(50)
    first = view.canvas._image()
    assert first is not None
    view.frame = frame(200)  # old Frame freed; a new one may reuse its address
    img = view.canvas.grab().toImage()
    assert img.pixelColor(img.width() // 2, img.height() // 2).red() == 200


def test_port_4010_is_not_a_login_failure(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    c = show_and_wait(qtbot, view)
    c.on_error(OSError("cannot reach port 4010"))
    qtbot.waitUntil(lambda: "4010" in view.status.text(), timeout=2000)
    assert "login failed" not in view.status.text()
    c.on_error(StreamError("stream: HTTP 401 (token rejected)", 401))
    qtbot.waitUntil(lambda: "login failed" in view.status.text(), timeout=2000)


@pytest.mark.parametrize("rot", [90, 180, 270])
def test_rotated_paint_puts_marked_corner_where_geometry_says(qtbot, tmp_path, rot):
    view, _ = make_view(qtbot, tmp_path)
    arr = np.zeros((40, 20), np.uint8)  # 20 wide, 40 tall
    arr[:8, :8] = 255  # marked top-left corner of the frame
    view.frame = Frame(20, 40, arr)
    view.rotation = rot
    img = view.canvas.grab().toImage()
    g = sv.Geometry(QRectF(0, 0, img.width(), img.height()), QRect(0, 0, 20, 40), rot)
    p = g.to_view(QPointF(3, 3))
    assert img.pixelColor(int(p.x()), int(p.y())).red() > 200
    q = g.to_view(QPointF(15, 35))  # unmarked area
    assert img.pixelColor(int(q.x()), int(q.y())).red() < 50


def test_closing_main_window_closes_present_and_stops_clients(qtbot, tmp_path):
    from rmdesk.ui.app import MainWindow
    conn = make_conn()
    win = MainWindow(conn, Config(), save_config=lambda c: None)
    qtbot.addWidget(win)
    sview = ScreenView(conn, keep_default=False, control_default=False, client_factory=FakeClient, pen_factory=FakePen,
                       status_fn=lambda d: "running", secrets=Creds(),
                       pictures_dir=tmp_path)
    qtbot.addWidget(sview)
    win.screen_view = sview
    sview.show()
    qtbot.waitUntil(lambda: len(started()) == 1, timeout=5000)
    sview.show_present()
    pw = sview.present
    qtbot.addWidget(pw)
    win.show()
    win.close()
    assert not pw.isVisible()
    assert started() == []


# ---------------------------------------------------------------- refresh / catch-up / 429

def test_refresh_button_resyncs(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    c = show_and_wait(qtbot, view)
    assert view.refresh_btn.accessibleName() == "Refresh"  # an icon-only button
    assert not view.refresh_btn.icon().isNull() and "fresh picture" in view.refresh_btn.toolTip()
    view.refresh_btn.click()
    qtbot.waitUntil(lambda: c.resyncs() == 1, timeout=3000)  # after the service restart


def test_refresh_button_restarts_only_the_stream_service(qtbot, tmp_path):
    """A resync can't fix a picture the service latched wrong (a band of noise); the button
    restarts goMarkableStream, never xochitl, holding the tablet job slot meanwhile."""
    seen = []
    view, conn = make_view(qtbot, tmp_path)
    dev = conn.device
    real = gms.restart

    def restart(d):
        seen.append(conn.current_job)
        real(d)
    view._restart_fn = restart
    c = show_and_wait(qtbot, view)
    start = len(dev.log)
    view.refresh_btn.click()
    qtbot.waitUntil(lambda: c.resyncs() == 1, timeout=3000)
    cmds = [a for m, a in dev.log[start:] if m == "run"]
    assert cmds == ["systemctl restart goMarkableStream"]
    assert not any("xochitl" in a for a in cmds)
    assert seen == [sv.REFRESH_JOB] and conn.current_job is None  # used, then released


def test_refresh_shows_progress_until_the_first_new_frame(qtbot, tmp_path):
    gate = threading.Event()
    view, conn = make_view(qtbot, tmp_path, restart_fn=lambda d: gate.wait(5))
    view.resize(700, 600)
    c = show_and_wait(qtbot, view)
    assert not view.refresh_bar.isVisible()
    view.refresh_btn.click()
    assert view.refresh_bar.isVisible() and view.refreshing
    bar = view.refresh_bar
    assert bar.minimum() == bar.maximum() == 0  # indeterminate
    assert 2 <= bar.height() <= 3
    top = view.canvas.mapTo(view, view.canvas.rect().topLeft())
    assert bar.geometry().topLeft() == top and bar.width() == view.canvas.width()
    assert not view.refresh_btn.isEnabled()
    assert view.refresh_btn.toolTip() == sv.REFRESHING_TIP
    assert view.hint.text_for(view.refresh_btn) == sv.REFRESHING_TIP
    c.on_frame(frame(1))  # the old (bad) stream: not the fresh picture
    QApplication.processEvents()
    assert view.refresh_bar.isVisible()
    gate.set()
    qtbot.waitUntil(lambda: c.resyncs() == 1, timeout=3000)
    assert view.refresh_bar.isVisible()
    c.on_frame(frame(2))  # the restarted service's first frame
    qtbot.waitUntil(lambda: not view.refresh_bar.isVisible(), timeout=2000)
    assert view.refresh_btn.isEnabled() and not view.refreshing
    assert view.hint.text_for(view.refresh_btn) == sv.HINTS["refresh"]
    assert "fresh picture" in view.refresh_btn.toolTip()
    assert view.status.text() == ""


def test_refresh_times_out_with_a_message(qtbot, tmp_path):
    view, conn = make_view(qtbot, tmp_path, refresh_timeout=0.3)
    c = show_and_wait(qtbot, view)
    view.refresh_btn.click()
    qtbot.waitUntil(lambda: c.resyncs() == 1, timeout=3000)
    qtbot.waitUntil(lambda: not view.refreshing, timeout=3000)  # no frame ever came
    assert sv.REFRESH_FAILED_MSG == "Couldn't refresh the picture; try again."
    assert view.status.text() == sv.REFRESH_FAILED_MSG and view.status.isVisible()
    assert view.status.property("chip") == "err"
    assert not view.refresh_bar.isVisible() and view.refresh_btn.isEnabled()
    c.on_frame(frame(3))  # a later frame clears it
    qtbot.waitUntil(lambda: view.status.text() == "", timeout=2000)


def test_refresh_times_out_while_the_restart_hangs(qtbot, tmp_path):
    gate = threading.Event()
    view, conn = make_view(qtbot, tmp_path, restart_fn=lambda d: gate.wait(5),
                           refresh_timeout=0.2)
    c = show_and_wait(qtbot, view)
    view.refresh_btn.click()
    qtbot.waitUntil(lambda: not view.refreshing, timeout=3000)
    assert view.status.text() == sv.REFRESH_FAILED_MSG
    gate.set()
    qtbot.waitUntil(lambda: conn.current_job is None, timeout=3000)  # still released
    QThreadPool.globalInstance().waitForDone(3000)
    QApplication.processEvents()
    assert view.refresh_btn.isEnabled() and not view.refresh_bar.isVisible()


def test_refresh_failure_shows_the_message_and_releases_the_job(qtbot, tmp_path):
    def boom(d):
        raise gms.InstallError("`systemctl restart goMarkableStream` failed (1): nope")
    view, conn = make_view(qtbot, tmp_path, restart_fn=boom)
    show_and_wait(qtbot, view)
    view.refresh_btn.click()
    qtbot.waitUntil(lambda: not view.refreshing, timeout=3000)
    assert view.status.text() == sv.REFRESH_FAILED_MSG
    assert conn.current_job is None and view.refresh_btn.isEnabled()


def test_refresh_refused_while_a_job_runs(qtbot, tmp_path):
    calls = []
    view, conn = make_view(qtbot, tmp_path, restart_fn=calls.append)
    c = show_and_wait(qtbot, view)
    assert conn.try_begin_job("sending to the tablet")
    try:
        view.refresh_btn.click()
        assert view.status.text() == sv.JOB_BUSY_MSG.format("sending to the tablet")
        assert "busy with: sending to the tablet" in view.status.text()
        QThreadPool.globalInstance().waitForDone(3000)
        assert calls == [] and c.resyncs() == 0
        assert not view.refreshing and not view.refresh_bar.isVisible()
        assert view.refresh_btn.isEnabled()
    finally:
        conn.end_job()


def test_refresh_pauses_keep_refreshing(qtbot, tmp_path):
    gate = threading.Event()
    view, conn = make_view(qtbot, tmp_path, restart_fn=lambda d: gate.wait(5),
                           keep_interval=0.05)
    c = show_and_wait(qtbot, view)
    view.refresh_btn.click()
    view.set_keep_refreshing(True)
    qtbot.wait(300)
    assert c.resyncs() == 0  # no keep-refreshing resyncs pile up during the refresh
    gate.set()
    qtbot.waitUntil(lambda: c.resyncs() == 1, timeout=3000)  # the refresh's own
    qtbot.wait(300)
    assert c.resyncs() == 1  # still waiting for the fresh frame
    c.on_frame(frame(1))
    qtbot.waitUntil(lambda: not view.refreshing, timeout=2000)
    qtbot.waitUntil(lambda: c.resyncs() == 2, timeout=3000)  # keep refreshing resumes


def test_cheap_resyncs_do_not_restart_the_service(qtbot, tmp_path):
    """Keep refreshing, catch-up and Present's R key stay a plain resync."""
    calls = []
    view, conn = make_view(qtbot, tmp_path, restart_fn=calls.append, keep_interval=0.05,
                           catchup_delay=0.1)
    c = show_and_wait(qtbot, view)
    view.set_keep_refreshing(True)
    qtbot.waitUntil(lambda: c.resyncs() >= 1, timeout=3000)
    view.set_keep_refreshing(False)
    c.on_frame(frame(1))  # the keep-refreshing resync's full frame
    qtbot.wait(50)
    c.on_frame(frame(2))  # a burst: catch-up resyncs after it
    qtbot.waitUntil(lambda: c.resyncs() >= 2, timeout=3000)
    pw = make_present(qtbot, view)
    n = c.resyncs()
    QTest.keyClick(pw, Qt.Key.Key_R)
    assert c.resyncs() == n + 1
    QThreadPool.globalInstance().waitForDone(3000)
    assert calls == [] and not view.refreshing


def test_refresh_without_stream_does_nothing(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    view.refresh_btn.click()  # never shown: no client
    assert FakeClient.instances == []


def test_present_r_key_resyncs(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    c = show_and_wait(qtbot, view)
    pw = make_present(qtbot, view)
    QTest.keyClick(pw, Qt.Key.Key_R)
    assert c.resyncs() == 1


def test_http_429_shows_another_viewer_message(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    c = show_and_wait(qtbot, view)
    c.on_error(StreamError("stream: HTTP 429", 429))
    qtbot.waitUntil(lambda: view.status.text() == sv.BUSY_MSG, timeout=2000)
    assert sv.BUSY_MSG == ("The tablet's stream is busy: another viewer is connected, or a "
                           "connection is left over from switching between USB and Wi-Fi. "
                           "Close other viewers or press Restart stream.")
    assert view.restart_btn.isVisible()
    assert c.alive and "stop" not in c.names()  # the client keeps retrying on its own
    c.on_frame(frame(9))  # the other viewer left; frames arrive again
    qtbot.waitUntil(lambda: view.status.text() == "", timeout=2000)


def test_catch_up_resyncs_once_after_the_last_frame(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path, catchup_delay=0.15)
    c = show_and_wait(qtbot, view)
    c.on_frame(frame(1))
    qtbot.waitUntil(lambda: c.resyncs() == 1, timeout=3000)
    qtbot.wait(400)
    assert c.resyncs() == 1  # not repeating
    c.on_frame(frame(2))  # the full frame the resync produced
    qtbot.wait(400)
    assert c.resyncs() == 1
    c.on_frame(frame(3))  # a new burst
    qtbot.waitUntil(lambda: c.resyncs() == 2, timeout=3000)


def test_no_catch_up_while_frames_keep_coming(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path, catchup_delay=0.3)
    c = show_and_wait(qtbot, view)
    for i in range(12):
        c.on_frame(frame(i))
        qtbot.wait(50)
    assert c.resyncs() == 0
    qtbot.waitUntil(lambda: c.resyncs() == 1, timeout=3000)


def test_refresh_full_frame_does_not_trigger_catch_up(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path, catchup_delay=0.15)
    c = show_and_wait(qtbot, view)
    view.refresh()
    c.on_frame(frame(4))  # the refresh's full frame
    qtbot.wait(450)
    assert c.resyncs() == 1


def test_no_catch_up_once_the_stream_is_stopped(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path, catchup_delay=0.15)
    c = show_and_wait(qtbot, view)
    c.on_frame(frame(5))
    view.hide()
    qtbot.waitUntil(lambda: not c.alive, timeout=5000)
    qtbot.wait(400)
    assert c.resyncs() == 0


def test_default_catch_up_delay_is_2_5_s(qtbot, tmp_path):
    conn = make_conn()
    view = ScreenView(conn, keep_default=False, control_default=False, client_factory=FakeClient, pen_factory=FakePen,
                      status_fn=lambda d: "running", secrets=Creds(), pictures_dir=tmp_path)
    qtbot.addWidget(view)
    assert view._catchup.delay == 2.5


# ---------------------------------------------------------------- aspect ratio

def _bright_span(img, *, row=None, col=None):
    """First and last index along a row/column whose pixel is the white frame."""
    if row is not None:
        vals = [img.pixelColor(x, row).red() for x in range(img.width())]
    else:
        vals = [img.pixelColor(col, y).red() for y in range(img.height())]
    hits = [i for i, v in enumerate(vals) if v > 200]
    return (hits[0], hits[-1]) if hits else None


def _page():
    return Frame(1404, 1872, np.full((1872, 1404), 255, np.uint8))


def _canvas(qtbot, view, w, h):
    """A FrameCanvas of exactly w x h (the Screen view's own canvas is sized by its layout)."""
    c = sv.FrameCanvas(view)
    qtbot.addWidget(c)
    c.resize(w, h)
    return c


def test_portrait_frame_letterboxed_in_wide_canvas(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    canvas = _canvas(qtbot, view, 1084, 812)
    view.frame = _page()
    img = canvas.grab().toImage()
    assert (img.width(), img.height()) == (1084, 812)
    x0, x1 = _bright_span(img, row=406)
    assert abs((x1 - x0 + 1) - 609) <= 2  # 1404 * 812 / 1872 = 609
    assert abs(x0 - (1084 - 609) / 2) <= 2 and abs((1083 - x1) - (1084 - 609) / 2) <= 2
    y0, y1 = _bright_span(img, col=542)
    assert y0 <= 1 and y1 >= 810  # full height
    assert img.pixelColor(100, 406).red() < 50 and img.pixelColor(1000, 406).red() < 50  # bars


@pytest.mark.parametrize("rot", [90, 270])
def test_rotated_frame_letterboxed_in_tall_canvas(qtbot, tmp_path, rot):
    # rotated, the page is landscape (1872 x 1404): in a tall canvas the bars are top/bottom
    view, _ = make_view(qtbot, tmp_path)
    canvas = _canvas(qtbot, view, 812, 1084)
    view.frame = _page()
    view.rotation = rot
    img = canvas.grab().toImage()
    y0, y1 = _bright_span(img, col=406)
    assert abs((y1 - y0 + 1) - 609) <= 2  # 1404 * 812 / 1872
    assert abs(y0 - (1084 - 609) / 2) <= 2
    x0, x1 = _bright_span(img, row=542)
    assert x0 <= 1 and x1 >= 810


def test_rotated_frame_in_wide_canvas_keeps_4_3(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    canvas = _canvas(qtbot, view, 1300, 812)
    view.frame = _page()
    view.rotation = 90
    img = canvas.grab().toImage()
    x0, x1 = _bright_span(img, row=406)
    assert abs((x1 - x0 + 1) - 1083) <= 2  # 1872 * 812 / 1404
    assert abs(x0 - (1300 - 1083) / 2) <= 2


def test_present_letterboxes_too(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    pw = make_present(qtbot, view)
    pw.resize(1084, 812)
    view.frame = _page()
    img = pw.grab().toImage()
    x0, x1 = _bright_span(img, row=406)
    assert abs((x1 - x0 + 1) - 609) <= 2


def test_crop_mapping_matches_the_letterboxed_paint(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    canvas = _canvas(qtbot, view, 1084, 812)
    view.frame = _page()
    img = canvas.grab().toImage()
    x0, x1 = _bright_span(img, row=406)
    # a drag over exactly the painted area selects (almost) the whole frame
    r = crop_rect(QRectF(0, 0, 1084, 812), QSize(1404, 1872), QRectF(x0, 0, x1 - x0 + 1, 812))
    assert abs(r.left()) <= 3 and abs(r.right() - 1403) <= 3
    assert r.top() == 0 and abs(r.bottom() - 1871) <= 3


# ---------------------------------------------------------------- keep refreshing (R49)

def test_keep_refreshing_button_defaults_off_with_tooltip(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    assert view.keep_btn.isCheckable() and not view.keep_btn.isChecked()
    assert view.keep_btn.accessibleName() == "Keep refreshing"
    assert view.keep_btn.toolTip() == ("Keep refreshing: Fetches a fresh picture every second — "
                                       "use while typing on the keyboard or presenting. Uses "
                                       "more tablet battery.")
    assert view._keep_timer.interval() == 1000


def test_keep_refreshing_resyncs_every_interval_and_stops_when_off(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path, keep_interval=0.1)
    c = show_and_wait(qtbot, view)
    qtbot.wait(300)
    assert c.resyncs() == 0  # off by default
    view.keep_btn.click()
    for n in (1, 2, 3):
        qtbot.waitUntil(lambda: c.resyncs() == n, timeout=3000)
        c.on_frame(frame(n))  # the resync's full frame arrives
    view.keep_btn.click()  # off
    n = c.resyncs()
    qtbot.wait(400)
    assert c.resyncs() == n


def test_keep_refreshing_does_not_pile_up_while_a_resync_is_pending(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path, keep_interval=0.05)
    c = show_and_wait(qtbot, view)
    view.keep_btn.click()
    qtbot.waitUntil(lambda: c.resyncs() == 1, timeout=3000)
    qtbot.wait(400)  # many ticks, but no frame came back yet
    assert c.resyncs() == 1
    c.on_frame(frame(1))
    qtbot.waitUntil(lambda: c.resyncs() == 2, timeout=3000)


def test_keep_refreshing_frames_do_not_schedule_catch_up(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path, keep_interval=0.1, catchup_delay=0.15)
    c = show_and_wait(qtbot, view)
    view.keep_btn.click()
    qtbot.waitUntil(lambda: c.resyncs() == 1, timeout=3000)
    view.keep_btn.click()  # off again, then the full frame lands
    c.on_frame(frame(1))
    qtbot.wait(450)
    assert c.resyncs() == 1


def test_keep_refreshing_shared_with_present_k_key(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path, keep_interval=0.1)
    c = show_and_wait(qtbot, view)
    pw = make_present(qtbot, view)
    QTest.keyClick(pw, Qt.Key.Key_K)
    assert view.keep_btn.isChecked() and view.keep_refreshing
    qtbot.waitUntil(lambda: c.resyncs() == 1, timeout=3000)
    QTest.keyClick(pw, Qt.Key.Key_K)
    assert not view.keep_btn.isChecked() and not view.keep_refreshing
    view.keep_btn.click()
    assert view.keep_refreshing


def test_keep_refreshing_stops_when_the_stream_stops(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path, keep_interval=0.05)
    c = show_and_wait(qtbot, view)
    view.keep_btn.click()
    qtbot.waitUntil(lambda: c.resyncs() == 1, timeout=3000)
    c.on_frame(frame(1))
    view.hide()
    qtbot.waitUntil(lambda: not c.alive, timeout=5000)
    n = c.resyncs()
    qtbot.wait(300)
    assert c.resyncs() == n
    # shown again: a new client, refreshing resumes for it (the toggle stays on)
    view.show()
    qtbot.waitUntil(lambda: len(started()) == 1, timeout=5000)
    c2 = started()[0]
    assert c2 is not c
    qtbot.waitUntil(lambda: c2.resyncs() >= 1, timeout=3000)


# ---------------------------------------------------------------- R50

@pytest.mark.parametrize("key", [Qt.Key.Key_R, Qt.Key.Key_K])
@pytest.mark.parametrize("mod", [Qt.KeyboardModifier.ControlModifier,
                                 Qt.KeyboardModifier.AltModifier,
                                 Qt.KeyboardModifier.MetaModifier])
def test_present_r_k_ignore_modifiers(qtbot, tmp_path, key, mod):
    view, _ = make_view(qtbot, tmp_path)
    c = show_and_wait(qtbot, view)
    pw = make_present(qtbot, view)
    QTest.keyClick(pw, key, mod)
    assert c.resyncs() == 0 and not view.keep_refreshing


@pytest.mark.parametrize("key", [Qt.Key.Key_R, Qt.Key.Key_K])
def test_present_r_k_ignore_autorepeat(qtbot, tmp_path, key):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent
    view, _ = make_view(qtbot, tmp_path)
    c = show_and_wait(qtbot, view)
    pw = make_present(qtbot, view)
    ev = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier, "", True)
    QApplication.sendEvent(pw, ev)
    assert c.resyncs() == 0 and not view.keep_refreshing
    QTest.keyClick(pw, key)  # a real press still works
    assert c.resyncs() == 1 or view.keep_refreshing


def test_catch_up_wait_reset_on_host_change(qtbot, tmp_path, monkeypatch):
    view, _ = make_view(qtbot, tmp_path)
    show_and_wait(qtbot, view)
    view.refresh()
    assert view._catchup._awaiting_full
    monkeypatch.setattr(view, "_host", lambda: "192.168.1.77")
    view._retarget()
    assert not view._catchup._awaiting_full


def test_rotate_twice_saves_180(qtbot, tmp_path):
    cfg, saved = Config(), []
    conn = make_conn()
    view = ScreenView(conn, keep_default=False, control_default=False, client_factory=FakeClient, pen_factory=FakePen,
                      status_fn=lambda dev: "running", secrets=Creds(),
                      pictures_dir=tmp_path / "Pictures", cfg=cfg,
                      save_config=lambda c: saved.append(c.screen_rotation))
    qtbot.addWidget(view)
    view.rotate_btn.click()
    view.rotate_btn.click()
    assert saved == [90, 180]
    assert cfg.screen_rotation == 180


def test_new_view_applies_saved_rotation(qtbot, tmp_path):
    for saved, expect in ((90, 90), (45, 0)):
        view = ScreenView(make_conn(), keep_default=False, control_default=False, client_factory=FakeClient, pen_factory=FakePen,
                          status_fn=lambda dev: "running", secrets=Creds(),
                          pictures_dir=tmp_path / "Pictures",
                          cfg=Config(screen_rotation=saved), save_config=lambda c: None)
        qtbot.addWidget(view)
        assert view.rotation == expect


# ---------------------------------------------------------------- redesign (Task 4)

def test_one_picture_for_every_pointer(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    view.show()
    assert [b.accessibleName() for b in view.pointer_seg.buttons] == ["Control", "Place"]
    assert not hasattr(view, "look_btn") and sv.POINTERS == ["control", "place"]
    assert all(not b.icon().isNull() and b.toolTip() for b in view.pointer_seg.buttons)
    assert view.pointer == "control" and view.pointer_seg.index() == 0
    assert not view.control.active  # control_default=False: waits until Control is chosen
    assert view.canvas is view.compose.view and view.canvas.isVisible()
    for btn, name in ((view.place_btn, "place"), (view.control_btn, "control"),
                      (view.place_btn, "place")):
        btn.click()
        assert view.pointer == name and view.canvas.isVisible() and view.compose.isVisible()
        assert view.compose.toolbar.isVisible() == (name == "place")
        assert view.canvas.isInteractive() == (name == "place")
    assert not any(isinstance(w, sv.FrameCanvas) for w in view.findChildren(sv.FrameCanvas))


def test_toggle_switch_drives_keep_refreshing(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path, keep_interval=0.1)
    assert view.keep_btn.isCheckable() and view.keep_btn.accessibleName() == "Keep refreshing"
    assert view.keep_btn.text() == "" and not view.keep_btn.icon().isNull()  # a small icon toggle
    c = show_and_wait(qtbot, view)
    view.keep_btn.click()
    assert view.keep_refreshing and view._keep_timer.isActive()
    qtbot.waitUntil(lambda: c.resyncs() == 1, timeout=3000)
    view.keep_btn.click()
    assert not view.keep_refreshing and not view._keep_timer.isActive()
    view.set_keep_refreshing(True)  # programmatic (Present's K) moves the switch too
    assert view.keep_btn.isChecked()


def test_expand_is_primary_outside_place_and_present_is_gone(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    view.show()
    assert view.full_btn.property("variant") == "primary" and view.full_btn.isVisible()
    assert not view.present_btn.isVisible()
    view.set_compose_mode(True)
    assert not view.full_btn.isVisible()
    view.set_compose_mode(False)
    assert view.full_btn.isVisible()


def test_status_is_a_chip_over_the_picture(qtbot, tmp_path):
    from rmdesk.ui.widgets import Chip
    view, _ = make_view(qtbot, tmp_path)
    view.resize(800, 600)
    c = show_and_wait(qtbot, view)
    assert isinstance(view.status, Chip)
    qtbot.waitUntil(lambda: view.status.text() == sv.WAITING_MSG, timeout=2000)
    assert view.status.isVisible() and view.status.state() == "neutral"
    host = view.canvas.geometry()
    assert host.contains(view.overlay.geometry())  # sits over the picture
    c.on_error(StreamError("stream: HTTP 429", 429))
    qtbot.waitUntil(lambda: view.status.text() == sv.BUSY_MSG, timeout=2000)
    assert view.status.state() == "warn" and host.contains(view.overlay.geometry())
    c.on_frame(frame(9))
    qtbot.waitUntil(lambda: not view.status.isVisible(), timeout=2000)
    assert not view.overlay.isVisible()


def test_present_key_hints_are_in_the_present_tooltip(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    view.resize(800, 600)
    view.show()
    tip = view.present_btn.toolTip()
    for key, what in sv.PRESENT_KEYS:
        assert f"{key}  {what}" in tip
    assert not hasattr(view, "keys")  # no hints row under the picture any more
    texts = {lbl.text() for lbl in view.findChildren(QLabel)}
    assert "In Present:" not in texts


def test_narrow_window_no_clipping(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    view.resize(700 - 208, 600)
    view.show()
    QApplication.processEvents()
    assert view.minimumSizeHint().width() <= view.width()
    view.set_pointer("control")
    QApplication.processEvents()
    ws = (view.pointer_seg, view.zoom_reset_btn, view.refresh_btn, view.keep_btn,
          view.rotate_btn, view.shot_btn, view.full_btn)
    for w in ws:
        assert w.isVisible(), w
        r = w.mapTo(view, w.rect().topLeft())
        assert r.x() >= 0 and r.x() + w.width() <= view.width(), w
        assert w.width() >= w.minimumSizeHint().width(), w
        assert w.height() >= w.minimumSizeHint().height(), w
    assert len({w.mapTo(view, w.rect().topLeft()).y() for w in ws}) > 1  # wrapped
    assert view.canvas.height() >= 200


def test_tab_order(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    view.show()
    chain, w = [], view.control_btn
    while w not in chain:
        chain.append(w)
        w = w.nextInFocusChain()
    want = [view.control_btn, view.place_btn, view.refresh_btn, view.keep_btn, view.rotate_btn,
            view.shot_btn, view.full_btn]
    pos = [chain.index(x) for x in want]
    assert pos == sorted(pos)
    assert all(x.focusPolicy() & Qt.FocusPolicy.TabFocus for x in want)


# ---------------------------------------------------------------- restart stream

def make_restart_view(qtbot, tmp_path, restart):
    conn = make_conn()
    conn.info = FirmwareInfo("3.24.0.100", "SN123", None)
    view = ScreenView(conn, keep_default=False, control_default=False, client_factory=FakeClient, pen_factory=FakePen,
                      status_fn=lambda dev: "running", restart_fn=restart,
                      secrets=Creds(), pictures_dir=tmp_path / "Pictures", catchup_delay=60.0)
    qtbot.addWidget(view)
    return view, conn


def test_restart_button_only_on_busy(qtbot, tmp_path):
    view, _ = make_restart_view(qtbot, tmp_path, lambda dev: None)
    c = show_and_wait(qtbot, view)
    assert not view.restart_btn.isVisible()
    c.on_error(StreamError("stream: HTTP 429", 429))
    qtbot.waitUntil(lambda: view.restart_btn.isVisible(), timeout=2000)
    c.on_error(StreamError("stream: HTTP 401", 401))
    qtbot.waitUntil(lambda: view.status.text() == sv.AUTH_MSG, timeout=2000)
    assert not view.restart_btn.isVisible()


def test_restart_stream_restarts_service_and_resyncs(qtbot, tmp_path):
    calls = []
    view, conn = make_restart_view(qtbot, tmp_path, lambda dev: calls.append(dev))
    c = show_and_wait(qtbot, view)
    c.on_error(StreamError("stream: HTTP 429", 429))
    qtbot.waitUntil(lambda: view.restart_btn.isVisible(), timeout=2000)
    before = c.resyncs()
    view.restart_btn.click()
    qtbot.waitUntil(lambda: len(calls) == 1 and not view._restarting, timeout=3000)
    assert conn.current_job is None  # job slot released
    assert view.status.text() != sv.BUSY_MSG
    assert c.resyncs() == before + 1


def test_restart_stream_failure_is_shown_and_releases_job(qtbot, tmp_path):
    def boom(dev):
        raise gms.InstallError("`systemctl restart goMarkableStream` failed (1): nope")
    view, conn = make_restart_view(qtbot, tmp_path, boom)
    c = show_and_wait(qtbot, view)
    c.on_error(StreamError("stream: HTTP 429", 429))
    qtbot.waitUntil(lambda: view.restart_btn.isVisible(), timeout=2000)
    view.restart_btn.click()
    qtbot.waitUntil(lambda: view.status.text().startswith("Could not restart"), timeout=3000)
    assert conn.current_job is None and not view._restarting


def test_restart_stream_refused_while_a_job_runs(qtbot, tmp_path):
    calls = []
    view, conn = make_restart_view(qtbot, tmp_path, lambda dev: calls.append(dev))
    show_and_wait(qtbot, view)
    assert conn.try_begin_job("sending to the tablet")
    try:
        view.restart_stream()
        assert "busy with: sending to the tablet" in view.status.text()
        assert calls == [] and not view._restarting
    finally:
        conn.end_job()


def test_progress_bar_shows_while_a_send_or_undo_runs(qtbot, tmp_path):
    view, _ = make_view(qtbot, tmp_path)
    view.show()
    assert not view.refresh_bar.isVisible()
    view.compose._begin("Undoing…")
    assert view.refresh_bar.isVisible()
    view.compose._end()
    assert not view.refresh_bar.isVisible()
