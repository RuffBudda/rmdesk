"""The tablet battery on the connection pill and in the Device view (BatteryMonitor)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import QApplication

from rmdesk import battery
from rmdesk.battery import Battery
from rmdesk.config import Config
from rmdesk.connection import Connection, State
from rmdesk.device import DeviceError
from rmdesk.firmware import FirmwareInfo
from rmdesk.ui.app import MainWindow
from rmdesk.ui.battery import BatteryMonitor
from tests.fakes import FakeDevice
from tests.test_battery import supplies


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


class FwConnection(Connection):
    def _after_connect(self, device, password):
        self.info = FirmwareInfo("3.29.1.0", "RM110-000-11111", None)


def tablet(cap="56", status="Discharging"):
    files = supplies(max77818_battery=("Battery", cap, status),
                     max77818_charger=("USB", "0", "Charging"))
    return FakeDevice(files)


def make_conn(dev):
    return FwConnection(Config(), factory=lambda h, p: dev, secrets=NoSecrets(),
                        sleep=lambda s: None, persist=lambda c: None)


def reads(dev):
    return sum(1 for m, a in dev.log if m == "run" and a == battery.READ_CMD)


def test_monitor_reads_right_after_connecting(qtbot):
    dev = tablet()
    conn = make_conn(dev)
    mon = BatteryMonitor(conn, interval=60.0)
    got = []
    mon.changed.connect(got.append)
    conn.connect()
    qtbot.waitUntil(lambda: mon.battery is not None, timeout=3000)
    assert mon.battery == Battery(56, "Discharging") and got == [Battery(56, "Discharging")]
    assert mon._timer.interval() == 60_000 and mon._timer.isActive()


def test_default_interval_is_60_s():
    import inspect
    assert inspect.signature(BatteryMonitor).parameters["interval"].default == 60.0


def test_monitor_polls_and_follows_changes(qtbot):
    dev = tablet()
    conn = make_conn(dev)
    mon = BatteryMonitor(conn, interval=0.05)
    conn.connect()
    qtbot.waitUntil(lambda: mon.battery == Battery(56, "Discharging"), timeout=3000)
    dev.files[f"{battery.POWER_SUPPLY}/max77818_battery/status"] = b"Charging\n"
    qtbot.waitUntil(lambda: mon.battery == Battery(56, "Charging"), timeout=3000)
    n = reads(dev)
    qtbot.waitUntil(lambda: reads(dev) >= n + 2, timeout=3000)


def test_poll_skips_during_a_job(qtbot):
    dev = tablet()
    conn = make_conn(dev)
    mon = BatteryMonitor(conn, interval=0.05)
    conn.connect()
    qtbot.waitUntil(lambda: mon.battery is not None, timeout=3000)
    QThreadPool.globalInstance().waitForDone(3000)
    assert conn.try_begin_job("sending to the tablet")
    try:
        QApplication.processEvents()
        QThreadPool.globalInstance().waitForDone(3000)
        n = reads(dev)
        qtbot.wait(300)  # several ticks
        assert reads(dev) == n
        assert mon.battery == Battery(56, "Discharging")  # kept, not cleared
    finally:
        conn.end_job()
    qtbot.waitUntil(lambda: reads(dev) > n, timeout=3000)  # back on the next tick


def test_poll_stops_when_disconnected(qtbot):
    dev = tablet()
    conn = make_conn(dev)
    mon = BatteryMonitor(conn, interval=0.05)
    got = []
    mon.changed.connect(got.append)
    conn.connect()
    qtbot.waitUntil(lambda: mon.battery is not None, timeout=3000)
    conn.disconnect()
    qtbot.waitUntil(lambda: mon.battery is None, timeout=3000)
    assert not mon._timer.isActive() and got[-1] is None
    QThreadPool.globalInstance().waitForDone(3000)
    QApplication.processEvents()
    n = reads(dev)
    qtbot.wait(300)
    assert reads(dev) == n


def test_unreadable_battery_is_nothing(qtbot):
    dev = FakeDevice({})  # no power supplies at all
    conn = make_conn(dev)
    mon = BatteryMonitor(conn, interval=60.0)
    conn.connect()
    qtbot.waitUntil(lambda: reads(dev) == 1, timeout=3000)
    QThreadPool.globalInstance().waitForDone(3000)
    QApplication.processEvents()
    assert mon.battery is None and conn.state == State.USB and conn.last_error is None


def test_poll_runs_off_the_gui_thread(qtbot):
    import threading
    threads = []
    dev = tablet()

    def handler(cmd):
        if cmd == battery.READ_CMD:
            threads.append(threading.current_thread())
        return None
    dev.run_handler = handler
    conn = make_conn(dev)
    mon = BatteryMonitor(conn, interval=60.0)
    conn.connect()
    qtbot.waitUntil(lambda: mon.battery is not None, timeout=3000)
    assert threads and threading.main_thread() not in threads


def test_lost_link_during_a_read_is_reported(qtbot):
    dev = tablet()
    conn = make_conn(dev)
    mon = BatteryMonitor(conn, interval=60.0)
    conn.connect()
    qtbot.waitUntil(lambda: mon.battery is not None, timeout=3000)
    dev.fail_on["run"] = DeviceError("gone")
    mon.poll()
    qtbot.waitUntil(lambda: conn.state == State.OFFLINE, timeout=3000)
    qtbot.waitUntil(lambda: mon.battery is None, timeout=3000)


# ---------------------------------------------------------------- the pill and the Device view

class NoWeb:
    def available(self):
        return False


def make_win(qtbot, dev):
    from rmdesk.firmware import XOCHITL_DIR
    dev.files.setdefault(f"{XOCHITL_DIR}/.keep", b"")  # an empty library, not a lost link
    conn = make_conn(dev)
    cfg = conn.cfg
    # never the real tablet's web interface (10.11.99.1) from a test
    win = MainWindow(conn, cfg, save_config=lambda c: None, web_factory=NoWeb)
    win.files_view.show_error = lambda msg: None  # the fake tablet has no library: no modal
    qtbot.addWidget(win)
    win.show()
    conn.connect()
    return win, conn


def test_pill_shows_firmware_and_battery_without_serial(qtbot):
    win, conn = make_win(qtbot, tablet())
    qtbot.waitUntil(lambda: "56%" in win.pill.sub_text(), timeout=3000)
    assert win.pill.sub_text() == "fw 3.29.1.0 · 56%"
    assert "RM110" not in win.pill.sub_text() and not win.pill.battery_low()


def test_pill_without_battery_shows_firmware_only(qtbot):
    win, conn = make_win(qtbot, FakeDevice({}))
    qtbot.waitUntil(lambda: win.pill.label.text() == "USB", timeout=3000)
    QThreadPool.globalInstance().waitForDone(3000)
    QApplication.processEvents()
    assert win.pill.sub_text() == "fw 3.29.1.0"


def test_pill_charging(qtbot):
    win, conn = make_win(qtbot, tablet("80", "Charging"))
    qtbot.waitUntil(lambda: "80%" in win.pill.sub_text(), timeout=3000)
    assert win.pill.sub_text() == "fw 3.29.1.0 · 80% ⚡"
    assert "80% ⚡" in win.pill.toolTip() and not win.pill.battery_low()


def test_pill_low_battery_uses_the_warn_colour(qtbot):
    win, conn = make_win(qtbot, tablet("12", "Discharging"))
    qtbot.waitUntil(lambda: "12%" in win.pill.sub_text(), timeout=3000)
    assert win.pill.battery_low()
    badge = win.screen_view.battery_badge  # the header's battery shows it in the warn colour
    assert badge.low()
    win.monitor._set(Battery(12, "Charging"))  # plugged in: no longer a warning
    assert not win.pill.battery_low() and not badge.low()


def test_pill_tooltip_has_the_battery(qtbot):
    win, conn = make_win(qtbot, tablet())
    qtbot.waitUntil(lambda: "56%" in win.pill.sub_text(), timeout=3000)
    assert "56%" in win.pill.toolTip() and "USB" in win.pill.toolTip()
    assert "<" not in win.pill.toolTip()  # plain text, no markup


def test_pill_battery_goes_when_disconnected(qtbot):
    win, conn = make_win(qtbot, tablet())
    qtbot.waitUntil(lambda: "56%" in win.pill.sub_text(), timeout=3000)
    conn.disconnect()
    qtbot.waitUntil(lambda: win.pill.label.text() == "Offline", timeout=3000)
    QApplication.processEvents()
    assert "%" not in win.pill.sub_text()


def test_device_view_battery_row(qtbot):
    win, conn = make_win(qtbot, tablet())
    dv = win.device_view
    qtbot.waitUntil(lambda: dv.battery_label.text() == "56% · Discharging", timeout=3000)
    assert dv.battery_bar.value() == 56 and dv.battery_bar.maximum() == 100
    win.show_view("Device")
    QApplication.processEvents()
    assert dv.battery_label.isVisible() and dv.battery_bar.isVisible()
    assert dv.serial_label.text() == "RM110-000-11111"  # the serial stays here


def test_device_view_battery_row_hidden_when_unknown(qtbot):
    win, conn = make_win(qtbot, FakeDevice({}))
    dv = win.device_view
    qtbot.waitUntil(lambda: win.pill.label.text() == "USB", timeout=3000)
    QThreadPool.globalInstance().waitForDone(3000)
    QApplication.processEvents()
    win.show_view("Device")
    QApplication.processEvents()
    assert not dv.battery_label.isVisible() and not dv.battery_bar.isVisible()
    assert not dv.battery_field.isVisible()


def test_first_read_waits_for_a_job_that_holds_the_slot(qtbot):
    dev = tablet()
    conn = make_conn(dev)
    mon = BatteryMonitor(conn, interval=60.0)
    assert conn.try_begin_job("loading the library")
    conn.connect()  # the connect reaches the monitor while the job runs
    QThreadPool.globalInstance().waitForDone(3000)
    QApplication.processEvents()
    assert reads(dev) == 0 and mon.battery is None
    conn.end_job()
    qtbot.waitUntil(lambda: mon.battery == Battery(56, "Discharging"), timeout=3000)


# ---------------------------------------------------------------- the Screen header and Expand

def screen_view(qtbot, tmp_path):
    from tests.ui.test_screen_view import make_view
    view, _ = make_view(qtbot, tmp_path)
    view.resize(1000, 700)
    view.show()
    QApplication.processEvents()
    return view


def test_screen_header_battery_next_to_undo(qtbot, tmp_path):
    view = screen_view(qtbot, tmp_path)
    badge = view.battery_badge
    assert not badge.isVisible()  # unknown: nothing
    view.set_battery(Battery(56, "Discharging"))
    QApplication.processEvents()
    assert badge.isVisible() and badge.text.text() == "56%"
    assert not badge.icon.pixmap().isNull()
    assert not view.send_cluster.isVisible()  # nothing to send: the battery still shows
    r = badge.mapTo(view, badge.rect().topRight())
    assert view.width() - r.x() < 40  # at the right end of the header
    assert badge.parentWidget() is view.header
    assert view.hint.text_for(badge) == "Tablet battery: 56%, discharging"
    assert badge.accessibleName() == "Tablet battery: 56%, discharging"
    view.set_pointer("place")  # Undo / Send show: the battery sits just left of Undo
    QApplication.processEvents()
    sc = view.send_cluster
    u = sc.mapTo(view, sc.rect().topLeft())
    b = badge.mapTo(view, badge.rect().topRight())
    assert badge.isVisible() and 0 <= u.x() - b.x() <= 24
    assert abs((u.y() + sc.height() / 2) - (b.y() + badge.height() / 2)) <= 1  # same row


def test_screen_header_battery_charging_and_low(qtbot, tmp_path):
    view = screen_view(qtbot, tmp_path)
    view.set_battery(Battery(80, "Charging"))
    assert view.battery_badge.text.text() == "80% ⚡" and not view.battery_badge.low()
    assert view.hint.text_for(view.battery_badge) == "Tablet battery: 80%, charging"
    view.set_battery(Battery(12, "Discharging"))
    assert view.battery_badge.low() and view.battery_overlay.low()
    view.set_battery(None)
    assert not view.battery_badge.isVisible()


def test_expand_shows_a_small_battery_in_the_corner(qtbot, tmp_path):
    view = screen_view(qtbot, tmp_path)
    view.set_battery(Battery(56, "Discharging"))
    ov = view.battery_overlay
    assert not ov.isVisible()  # only in Expand
    view.set_fullscreen(True)
    QApplication.processEvents()
    assert ov.isVisible() and ov.text.text() == "56%"
    c = view.canvas
    top_right = c.mapTo(view, c.rect().topRight())
    g = ov.geometry()
    assert g.top() == top_right.y() + 8 and top_right.x() - g.right() == 8
    assert g.width() < 90 and 14 <= g.height() < 26  # small: it must not cover much
    assert g.height() >= ov.sizeHint().height()  # not squashed
    assert ov.text.font().pixelSize() in (11, -1)
    assert ov.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    view.set_fullscreen(False)
    assert not ov.isVisible()


def test_expand_without_battery_shows_nothing(qtbot, tmp_path):
    view = screen_view(qtbot, tmp_path)
    view.set_fullscreen(True)
    assert not view.battery_overlay.isVisible()


def test_main_window_feeds_the_screen_view(qtbot):
    win, conn = make_win(qtbot, tablet())
    qtbot.waitUntil(lambda: win.screen_view.battery == Battery(56, "Discharging"),
                    timeout=3000)
    assert win.screen_view.battery_badge.text.text() == "56%"
