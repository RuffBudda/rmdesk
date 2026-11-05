"""Control: the Screen view's mouse and keyboard drive the tablet (rmdesk.ui.remote_control)."""
import os
import struct
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, QThreadPool
from PySide6.QtGui import QFocusEvent, QKeyEvent, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication

from rmdesk.config import Config
from rmdesk.connection import Connection, State
from rmdesk.device import DeviceError
from rmdesk.firmware import FirmwareInfo
from rmdesk.remote import input as ri
from rmdesk.stream.client import Frame
from rmdesk.ui import remote_control as rc
from rmdesk.ui.screen_view import Geometry, ScreenView
from tests.fakes import FakeDevice
from tests.ui.test_screen_view import Creds, FakeClient, FakePen, NoSecrets

ABS, SYN, KEY = 3, 0, 1
TID, X, Y = 0x39, 0x35, 0x36

PROC_ALL = """I: Bus=0000
N: Name="Wacom I2C Digitizer"
H: Handlers=event1

I: Bus=0000
N: Name="pt_mt"
H: Handlers=event2

I: Bus=0005
N: Name="rM_Keyboard"
H: Handlers=sysrq kbd leds event3
"""
PROC_NO_FOLIO = PROC_ALL.split("I: Bus=0005")[0]


def ev(t, c, v):
    return struct.pack("<llHHi", 0, 0, t, c, v)


SYNR = ev(SYN, 0, 0)
SWEEP = b"".join(ev(KEY, c, 0) + SYNR for c in (42, 54, 29, 97, 56, 100, 125, 126))
UP = ev(ABS, 0x2F, 0) + ev(ABS, TID, -1) + SYNR


def events(data: bytes):
    return [struct.unpack("<llHHi", data[i:i + 16])[2:] for i in range(0, len(data), 16)]


class FakeWriter:
    def __init__(self, path, on_dead):
        self.path, self.on_dead = path, on_dead
        self.items: list = []
        self.closed = False

    def write(self, data):
        assert not self.closed, "write after close"
        self.items.append(bytes(data))

    def pause(self, s):
        self.items.append(("pause", s))

    def close(self):
        self.closed = True

    def raw(self):
        return b"".join(i for i in self.items if isinstance(i, bytes))

    def data(self):
        """What was written minus the modifier sweeps (asserted on their own below)."""
        return self.raw().replace(SWEEP, b"")

    def events(self):
        return events(self.data())

    def sweeps(self):
        return self.raw().count(SWEEP)


@pytest.fixture(autouse=True)
def drain(qtbot):
    yield
    QThreadPool.globalInstance().waitForDone(5000)
    QApplication.processEvents()


class FwConnection(Connection):
    def _after_connect(self, device, password):
        self.info = FirmwareInfo("3.29.1.0", "SN123", None)


def make(qtbot, proc=PROC_ALL, run_fail=None, defaults=False):
    def factory(h, p):
        dev = FakeDevice({ri.PROC_DEVICES: proc.encode()},
                         fail_on={"run": run_fail} if run_fail else None)
        dev.xochitl_active = True
        return dev
    conn = FwConnection(Config(), factory=factory, secrets=NoSecrets(), sleep=lambda s: None,
                        persist=lambda c: None)
    conn.connect()
    writers: dict[str, FakeWriter] = {}
    made: list[FakeWriter] = []

    def wf(dev, path, on_dead):
        w = FakeWriter(path, on_dead)
        writers[path] = w
        made.append(w)
        return w
    view = ScreenView(conn, keep_default=defaults, control_default=defaults,
                      client_factory=FakeClient, pen_factory=FakePen,
                      status_fn=lambda dev: "running", secrets=Creds(), catchup_delay=60.0,
                      input_writer_factory=wf)
    qtbot.addWidget(view)
    view.resize(900, 1100)
    view.show()
    QApplication.processEvents()
    return view, conn, writers, made


def full_frame():
    return Frame(1404, 1872, np.full((1872, 1404), 255, np.uint8))


def turn_on(qtbot, view, writers, want=("/dev/input/event2", "/dev/input/event3")):
    view.control_btn.click()
    qtbot.waitUntil(lambda: view.control.session is not None, timeout=5000)
    assert sorted(writers) == sorted(want)
    return writers.get("/dev/input/event2"), writers.get("/dev/input/event3")


def send_mouse(w, kind, pos, button=Qt.MouseButton.LeftButton):
    buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else button
    if kind == QEvent.Type.MouseMove:
        button = Qt.MouseButton.NoButton
    e = QMouseEvent(kind, QPointF(pos), w.mapToGlobal(QPointF(pos)), button, buttons,
                    Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(w, e)


def press(w, pos):
    send_mouse(w, QEvent.Type.MouseButtonPress, pos)


def move(w, pos):
    send_mouse(w, QEvent.Type.MouseMove, pos)


def release(w, pos):
    send_mouse(w, QEvent.Type.MouseButtonRelease, pos)


def key(w, k, down=True, mods=Qt.KeyboardModifier.NoModifier, text="", repeat=False):
    e = QKeyEvent(QEvent.Type.KeyPress if down else QEvent.Type.KeyRelease, k, mods, text,
                  repeat)
    QApplication.sendEvent(w, e)
    return e


def wheel(w, pos, dy):
    e = QWheelEvent(QPointF(pos), w.mapToGlobal(QPointF(pos)), QPoint(0, 0), QPoint(0, dy),
                    Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                    Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(w, e)


def view_point(view, fx, fy):
    """Where frame pixel (fx, fy) is drawn on the canvas now."""
    g, _ = view._canvas_geometry()
    return g.to_view(QPointF(fx, fy))


# ---------------------------------------------------------------- the switch

def test_control_waits_to_be_chosen_when_it_is_not_the_default(qtbot):
    view, conn, writers, made = make(qtbot)
    assert view.control_btn.accessibleName() == "Control" and view.control_btn.isCheckable()
    assert view.pointer == "control" and view.control_btn.isChecked()
    assert not view.control.active and made == []  # not on by itself (control_default=False)
    assert not view.control_overlay.isVisible()
    assert view.zoom_reset_btn.isVisible() and not view.zoom_reset_btn.isEnabled()
    view.set_pointer("place")
    assert not view.control.active and not view.zoom_reset_btn.isVisible()
    turn_on(qtbot, view, writers)  # chosen: on, and back on after jobs from now on
    assert conn.try_begin_job("sending")
    qtbot.waitUntil(lambda: not view.control.active, timeout=2000)
    conn.end_job()
    qtbot.waitUntil(lambda: view.control.active, timeout=2000)


def test_turning_on_shows_chip_and_focuses_canvas(qtbot):
    view, conn, writers, _ = make(qtbot)
    turn_on(qtbot, view, writers)
    assert view.control_btn.isChecked() and view.control.active and view.control.typing
    # the focus outline shows Control: no chip over the picture
    assert not view.control_chip.isVisible() and not view.typing_chip.isVisible()
    assert view.canvas.hasFocus() or QApplication.focusWidget() in (view.canvas, None)
    host = QRect(view.canvas.mapTo(view, QPoint(0, 0)), view.canvas.size())
    assert host.contains(view.control_overlay.geometry())


def test_nothing_sent_while_off(qtbot):
    view, conn, writers, made = make(qtbot)
    c = view.canvas
    p = QPointF(c.width() / 2, c.height() / 2)
    press(c, p)
    move(c, p + QPointF(30, 30))
    release(c, p + QPointF(30, 30))
    wheel(c, p, -120)
    key(c, Qt.Key.Key_A, text="a")
    key(c, Qt.Key.Key_A, down=False, text="a")
    QApplication.processEvents()
    assert made == []
    # and after an on/off round trip, nothing more goes out
    turn_on(qtbot, view, writers)
    view.place_btn.click()
    assert not view.control.active
    before = [len(w.items) for w in made]
    press(c, p)
    release(c, p)
    key(c, Qt.Key.Key_A, text="a")
    assert [len(w.items) for w in made] == before and all(w.closed for w in made)


def test_not_connected_refuses(qtbot):
    view, conn, writers, made = make(qtbot)
    conn.disconnect()
    QApplication.processEvents()
    view.control_btn.click()
    assert not view.control.active and made == []  # the pointer stays Control; Control is off
    assert view.control_chip.text() == rc.OFFLINE_MSG and view.control_chip.isVisible()
    assert rc.OFFLINE_MSG in view.control_btn.toolTip()  # and says why


# ---------------------------------------------------------------- mouse to touch

def test_click_is_a_tap_at_the_mapped_spot(qtbot):
    view, conn, writers, _ = make(qtbot)
    view.frame = full_frame()
    touch, _ = turn_on(qtbot, view, writers)
    c = view.canvas
    p = view_point(view, 709, 884)
    press(c, p)
    release(c, p)
    ev_ = touch.events()
    assert ev_[0] == (ABS, 0x2F, 0) and ev_[1] == (ABS, TID, 40000)  # slot 0, fresh id
    assert (ABS, X, 709) in ev_[:4] and (ABS, Y, 987) in ev_[:4]
    assert ev_[-2:] == [(ABS, TID, -1), (SYN, 0, 0)]
    # a second click gets a fresh tracking id
    press(c, p)
    release(c, p)
    assert (ABS, TID, 40001) in touch.events()


def test_click_off_the_picture_sends_nothing(qtbot):
    view, conn, writers, _ = make(qtbot)
    view.frame = full_frame()
    touch, _ = turn_on(qtbot, view, writers)
    g, _ = view._canvas_geometry()
    c = view.canvas
    off = QPointF(2, 2) if not g.target.contains(QPointF(2, 2)) else None
    if off is None:  # letterboxed sideways: use the far right
        off = QPointF(c.width() - 1, c.height() / 2)
    assert not g.target.contains(off)
    press(c, off)
    release(c, off)
    assert touch.items == []


def test_drag_is_a_coalesced_swipe(qtbot):
    view, conn, writers, _ = make(qtbot)
    view.frame = full_frame()
    touch, _ = turn_on(qtbot, view, writers)
    c = view.canvas
    a = view_point(view, 300, 1500)
    press(c, a)
    for i in range(1, 21):  # 20 moves inside one 16 ms window
        move(c, view_point(view, 300, 1500 - 50 * i))
    QApplication.processEvents()
    end = view_point(view, 300, 500)
    release(c, end)
    ev_ = touch.events()
    syns = ev_.count((SYN, 0, 0))
    assert syns < 10  # down + first move + release move + up, not 20 moves
    assert ev_[-2:] == [(ABS, TID, -1), (SYN, 0, 0)]
    ys = [v for t, code, v in ev_ if code == Y]
    assert ys[0] == 1871 - 1500 and ys[-1] == 1871 - 500


def test_drag_moves_paced_by_timer(qtbot):
    view, conn, writers, _ = make(qtbot)
    view.frame = full_frame()
    touch, _ = turn_on(qtbot, view, writers)
    c = view.canvas
    press(c, view_point(view, 300, 1500))
    move(c, view_point(view, 300, 1400))
    move(c, view_point(view, 300, 1300))
    move(c, view_point(view, 300, 1200))  # newest wins
    qtbot.waitUntil(lambda: (ABS, Y, 1871 - 1200) in touch.events(), timeout=2000)
    assert (ABS, Y, 1871 - 1300) not in touch.events()
    release(c, view_point(view, 300, 1200))


def test_wheel_is_a_vertical_swipe(qtbot):
    view, conn, writers, _ = make(qtbot)
    view.frame = full_frame()
    clock = [100.0]
    view.control._clock = lambda: clock[0]
    touch, _ = turn_on(qtbot, view, writers)
    c = view.canvas
    p = view_point(view, 700, 900)
    wheel(c, p, -120)  # wheel down: the finger moves up the screen
    ev_ = touch.events()
    assert ev_[0][1] == 0x2F and ev_[1][1] == TID and ev_[-2:] == [(ABS, TID, -1), (SYN, 0, 0)]
    ys = [v for t, code, v in ev_ if code == Y]
    xs = [v for t, code, v in ev_ if code == X]
    assert ys[0] == 1871 - 900 and ys[-1] > ys[0] + 300  # frame y went up the page
    assert set(xs) == {700}
    assert any(isinstance(i, tuple) and i[0] == "pause" for i in touch.items)
    n = len(touch.items)
    wheel(c, p, -120)  # same burst: coalesced
    assert len(touch.items) == n
    clock[0] += 1
    wheel(c, p, 120)  # wheel up: the finger moves down
    ys2 = [v for t, code, v in events(b"".join(i for i in touch.items[n:]
                                               if isinstance(i, bytes))) if code == Y]
    assert ys2[-1] < ys2[0]


@pytest.mark.parametrize("rot", [0, 90, 180, 270])
def test_mapping_corners_and_centre_under_rotation(rot):
    W, H = 1404, 1872
    view = QRectF(0, 0, 500, 500)
    g = Geometry(view, QRect(0, 0, W, H), rot)
    t = g.target
    tl, tr = QPointF(t.left(), t.top()), QPointF(t.right(), t.top())
    bl, br = QPointF(t.left(), t.bottom()), QPointF(t.right(), t.bottom())
    # which drawn corner shows frame (0,0), (W,0), (0,H), (W,H) under each clockwise rotation
    corners = {0: (tl, tr, bl, br), 90: (tr, br, tl, bl),
               180: (br, bl, tr, tl), 270: (bl, tl, br, tr)}[rot]
    want = [(0, 1871), (1403, 1871), (0, 0), (1403, 0)]
    for vp, touch in zip(corners, want):
        f = rc.view_to_frame(g, vp, (W, H), clamp=True)
        assert ri.frame_to_touch(*f) == touch, (rot, vp)
    f = rc.view_to_frame(g, t.center(), (W, H))
    assert ri.frame_to_touch(*f) == (702, 935)


def test_mapping_honours_a_crop_and_frame_size():
    g = Geometry(QRectF(0, 0, 100, 100), QRect(100, 200, 50, 50), 0)
    f = rc.view_to_frame(g, QPointF(0, 0), (1404, 1872))
    assert f == (100, 200)
    g2 = Geometry(QRectF(0, 0, 702, 936), QRect(0, 0, 702, 936), 0)
    assert rc.view_to_frame(g2, QPointF(351, 468), (702, 936)) == (702, 936)
    assert rc.view_to_frame(g2, QPointF(800, 10), (702, 936)) is None


def test_rotated_view_click_maps_through_rotation(qtbot):
    view, conn, writers, _ = make(qtbot)
    view.frame = full_frame()
    view.rotate()  # 90
    touch, _ = turn_on(qtbot, view, writers)
    c = view.canvas
    p = view_point(view, 100, 200)
    press(c, p)
    release(c, p)
    ev_ = touch.events()
    assert (ABS, X, 100) in ev_ and (ABS, Y, 1671) in ev_


# ---------------------------------------------------------------- keyboard to typing

def test_shift_letter_and_specials(qtbot):
    view, conn, writers, _ = make(qtbot)
    touch, kb = turn_on(qtbot, view, writers)
    c = view.canvas
    S = Qt.KeyboardModifier.ShiftModifier
    key(c, Qt.Key.Key_Shift, mods=S)
    key(c, Qt.Key.Key_A, mods=S, text="A")
    key(c, Qt.Key.Key_A, down=False, mods=S, text="A")
    key(c, Qt.Key.Key_Shift, down=False)
    for k in (Qt.Key.Key_Return, Qt.Key.Key_Backspace, Qt.Key.Key_Tab, Qt.Key.Key_Left,
              Qt.Key.Key_PageDown, Qt.Key.Key_Space):
        key(c, k)
        key(c, k, down=False)
    keys = [(code, v) for t, code, v in kb.events() if t == KEY]
    assert keys[:4] == [(42, 1), (30, 1), (30, 0), (42, 0)]
    assert keys[4:] == [(28, 1), (28, 0), (14, 1), (14, 0), (15, 1), (15, 0), (105, 1),
                        (105, 0), (109, 1), (109, 0), (57, 1), (57, 0)]
    assert kb.data().count(SYNR) == len(keys)
    assert touch.items == []


def test_shifted_punctuation_uses_its_key(qtbot):
    view, conn, writers, _ = make(qtbot)
    _, kb = turn_on(qtbot, view, writers)
    c = view.canvas
    S = Qt.KeyboardModifier.ShiftModifier
    key(c, Qt.Key.Key_Exclam, mods=S, text="!")
    key(c, Qt.Key.Key_1, down=False, text="1")  # released after Shift: same key
    # Shift goes with the key it modifies and is lifted with it
    assert [(code, v) for t, code, v in kb.events() if t == KEY] == [(42, 1), (2, 1), (2, 0),
                                                                    (42, 0)]


def test_esc_and_app_shortcuts_not_forwarded(qtbot):
    view, conn, writers, _ = make(qtbot)
    _, kb = turn_on(qtbot, view, writers)
    c = view.canvas
    C = Qt.KeyboardModifier.ControlModifier
    for k, m in ((Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier),
                 (Qt.Key.Key_F11, Qt.KeyboardModifier.NoModifier),
                 (Qt.Key.Key_1, C), (Qt.Key.Key_2, C), (Qt.Key.Key_3, C), (Qt.Key.Key_B, C)):
        key(c, k, mods=m)
        key(c, k, down=False, mods=m)
    assert kb.data() == b""
    key(c, Qt.Key.Key_C, mods=C, text="\x03")  # other Ctrl chords do go (e.g. copy)
    assert (KEY, 46, 1) in kb.events()


def test_autorepeat_ignored(qtbot):
    view, conn, writers, _ = make(qtbot)
    _, kb = turn_on(qtbot, view, writers)
    c = view.canvas
    key(c, Qt.Key.Key_X, text="x")
    for _ in range(5):
        key(c, Qt.Key.Key_X, down=False, text="x", repeat=True)
        key(c, Qt.Key.Key_X, text="x", repeat=True)
    key(c, Qt.Key.Key_X, down=False, text="x")
    assert [(code, v) for t, code, v in kb.events() if t == KEY] == [(45, 1), (45, 0)]


def test_typing_disabled_without_folio(qtbot):
    view, conn, writers, _ = make(qtbot, proc=PROC_NO_FOLIO)
    touch, kb = turn_on(qtbot, view, writers, want=("/dev/input/event2",))
    assert kb is None and view.control.active and not view.control.typing
    assert view.typing_chip.isVisible() and view.typing_chip.text() == rc.NO_FOLIO_MSG
    assert rc.NO_FOLIO_MSG in view.control_btn.toolTip()
    key(view.canvas, Qt.Key.Key_A, text="a")
    key(view.canvas, Qt.Key.Key_A, down=False, text="a")
    assert touch.items == [] and list(writers) == ["/dev/input/event2"]


def test_folio_writer_death_only_disables_typing(qtbot):
    view, conn, writers, _ = make(qtbot)
    touch, kb = turn_on(qtbot, view, writers)
    key(view.canvas, Qt.Key.Key_A, text="a")
    t = threading.Thread(target=lambda: kb.on_dead(DeviceError("No such device")))
    t.start()
    t.join(5)
    qtbot.waitUntil(lambda: not view.control.typing, timeout=2000)
    assert view.control.active and kb.closed and view.typing_chip.isVisible()


# ---------------------------------------------------------------- safety

def hold_everything(view, touch, kb):
    c = view.canvas
    press(c, view_point(view, 500, 500))
    key(c, Qt.Key.Key_Control, mods=Qt.KeyboardModifier.ControlModifier)
    key(c, Qt.Key.Key_Z, mods=Qt.KeyboardModifier.ControlModifier, text="\x1a")
    assert view.control.session.touching and view.control.session.pressed == [29, 44]
    touch.items.clear()
    kb.items.clear()


def assert_released(touch, kb):
    assert touch.data() == UP
    assert kb.data() == ev(KEY, 44, 0) + SYNR + ev(KEY, 29, 0) + SYNR
    assert touch.closed and kb.closed


def test_place_turns_off_and_releases(qtbot):
    view, conn, writers, _ = make(qtbot)
    view.frame = full_frame()
    touch, kb = turn_on(qtbot, view, writers)
    hold_everything(view, touch, kb)
    view.place_btn.click()
    assert_released(touch, kb)
    assert not view.control_overlay.isVisible() or view.control_chip.text() != rc.CONTROL_CHIP
    assert not view.control.active and not view.control_btn.isChecked()


def test_hide_turns_off_and_releases(qtbot):
    view, conn, writers, _ = make(qtbot)
    view.frame = full_frame()
    touch, kb = turn_on(qtbot, view, writers)
    hold_everything(view, touch, kb)
    view.hide()
    assert_released(touch, kb)
    assert not view.control.active and view.pointer == "control"  # back on when shown


def test_disconnect_turns_off(qtbot):
    view, conn, writers, _ = make(qtbot)
    view.frame = full_frame()
    touch, kb = turn_on(qtbot, view, writers)
    hold_everything(view, touch, kb)
    conn.connection_lost(DeviceError("link down"))
    qtbot.waitUntil(lambda: not view.control.active, timeout=2000)
    assert_released(touch, kb)
    assert view.control_chip.text() == rc.DISCONNECTED_MSG


def test_busy_job_turns_off_and_blocks(qtbot):
    view, conn, writers, _ = make(qtbot)
    view.frame = full_frame()
    touch, kb = turn_on(qtbot, view, writers)
    hold_everything(view, touch, kb)
    assert conn.try_begin_job("sending to the tablet")
    qtbot.waitUntil(lambda: not view.control.active, timeout=2000)
    assert_released(touch, kb)
    assert "sending to the tablet" in view.control_chip.text()
    # while the job runs, Control can't come back on
    view.set_pointer("control")
    QApplication.processEvents()
    assert not view.control.active
    assert len(writers) == 2 and all(w.closed for w in writers.values())
    conn.end_job()


def test_job_seen_before_its_signal_sends_nothing(qtbot):
    view, conn, writers, _ = make(qtbot)
    view.frame = full_frame()
    touch, kb = turn_on(qtbot, view, writers)
    conn.on_job.clear()  # the job slot is taken but the notice hasn't arrived yet
    conn.try_begin_job("uploading")
    press(view.canvas, view_point(view, 500, 500))
    key(view.canvas, Qt.Key.Key_A, text="a")
    assert touch.items == [] and kb.data() == b""
    assert not view.control.active
    conn.end_job()


def test_focus_out_with_key_down_releases(qtbot):
    view, conn, writers, _ = make(qtbot)
    view.frame = full_frame()
    touch, kb = turn_on(qtbot, view, writers)
    hold_everything(view, touch, kb)
    QApplication.sendEvent(view.canvas, QFocusEvent(QEvent.Type.FocusOut))
    assert touch.data() == UP
    assert kb.data() == ev(KEY, 44, 0) + SYNR + ev(KEY, 29, 0) + SYNR
    assert view.control.active  # still on; only let go


def test_touch_writer_death_turns_off_with_message(qtbot):
    view, conn, writers, _ = make(qtbot)
    touch, kb = turn_on(qtbot, view, writers)
    t = threading.Thread(target=lambda: touch.on_dead(OSError("Socket is closed")))
    t.start()
    t.join(5)
    qtbot.waitUntil(lambda: not view.control.active, timeout=2000)
    assert view.control_chip.text() == rc.LOST_MSG.format("Socket is closed")
    assert view.control_chip.isVisible() and touch.closed and kb.closed


def test_missing_touch_device_refuses(qtbot):
    view, conn, writers, made = make(qtbot, proc='N: Name="x"\nH: Handlers=event9\n')
    view.control_btn.click()
    qtbot.waitUntil(lambda: not view.control.active, timeout=5000)
    assert made == [] and view.control_chip.text() == rc.NO_TOUCH_MSG


def test_shutdown_releases(qtbot):
    view, conn, writers, _ = make(qtbot)
    view.frame = full_frame()
    touch, kb = turn_on(qtbot, view, writers)
    hold_everything(view, touch, kb)
    view.shutdown()
    assert_released(touch, kb)


def test_present_window_never_sends(qtbot):
    view, conn, writers, _ = make(qtbot)
    view.frame = full_frame()
    touch, kb = turn_on(qtbot, view, writers)
    view.show_present()
    pw = view.present
    qtbot.addWidget(pw)
    press(pw, QPointF(100, 100))
    release(pw, QPointF(100, 100))
    key(pw, Qt.Key.Key_A, text="a")
    assert touch.items == [] and kb.data() == b""
    pw.close()


def test_lookup_only_reads_proc(qtbot):
    view, conn, writers, _ = make(qtbot)
    dev = conn.device
    turn_on(qtbot, view, writers)
    runs = [a for m, a in dev.log if m == "run"]
    assert f"cat {ri.PROC_DEVICES}" in runs
    assert not any(m in ("write_bytes", "rename") for m, _ in dev.log)


# ---------------------------------------------------------------- default writer and the window

def test_default_writer_opens_cat_on_the_device_off_the_gui_thread():
    opened, seen = [], threading.Event()

    class Chan:
        data = bytearray()

        def write(self, b):
            self.data += b
            seen.set()

        def close(self):
            pass

    chan = Chan()

    class Dev:
        def open_writer(self, cmd):
            opened.append((cmd, threading.current_thread()))
            return chan
    w = rc._default_writer(Dev(), "/dev/input/event2", lambda e: None)
    w.write(b"x" * 16)
    assert seen.wait(5)
    w.close()
    assert w.join(5)
    assert opened[0][0] == "cat > /dev/input/event2"
    assert opened[0][1] is not threading.main_thread()


def test_window_shortcuts_and_esc_stay_with_rmdesk(qtbot, monkeypatch):
    from PySide6.QtTest import QTest
    from rmdesk.ui.app import MainWindow
    made: dict[str, FakeWriter] = {}

    def wf(dev, path, on_dead):
        made[path] = FakeWriter(path, on_dead)
        return made[path]
    monkeypatch.setattr(rc, "_default_writer", wf)
    monkeypatch.setattr("rmdesk.ui.screen_view.StreamClient", FakeClient)
    monkeypatch.setattr("rmdesk.ui.screen_view.gms.status", lambda dev: "running")
    cfg = Config()
    conn = FwConnection(cfg, factory=lambda h, p: FakeDevice({ri.PROC_DEVICES: PROC_ALL.encode()}),
                        secrets=NoSecrets(), sleep=lambda s: None, persist=lambda c: None)
    conn.connect()
    win = MainWindow(conn, cfg, save_config=lambda c: None)
    qtbot.addWidget(win)
    win.show()
    win.show_view("Screen")
    sv = win.screen_view
    # Control is on by default in the real window: it switches itself on
    qtbot.waitUntil(lambda: sv.control.session is not None, timeout=5000)
    kb = made["/dev/input/event3"]
    win.toggle_fullscreen()
    assert sv.fullscreen
    sv.canvas.setFocus()
    QTest.keyClick(sv.canvas, Qt.Key.Key_Escape)
    assert not sv.fullscreen and sv.control.active  # Esc went to rmdesk, not the tablet
    QTest.keyClick(sv.canvas, Qt.Key.Key_1, Qt.KeyboardModifier.ControlModifier)
    assert win.stack.currentWidget() is not sv  # Ctrl+1: rmdesk switched to Files
    assert not sv.control.active and kb.closed
    assert (KEY, 1, 1) not in kb.events() and (KEY, 2, 1) not in kb.events()
    # whatever Ctrl press reached the tablet was released when Control went off
    downs = [c for t, c, v in kb.events() if t == KEY and v == 1]
    ups = [c for t, c, v in kb.events() if t == KEY and v == 0]
    assert sorted(downs) == sorted(ups)
    win.close()



# ---------------------------------------------------------------- on by default

def test_control_and_keep_refreshing_are_on_by_default(qtbot):
    view, conn, writers, made = make(qtbot, defaults=True)
    assert view.keep_refreshing and view.keep_btn.isChecked()
    qtbot.waitUntil(lambda: view.control.session is not None, timeout=5000)
    assert view.control.active and view.control_btn.isChecked()


def test_switched_off_control_stays_off_after_a_job(qtbot):
    view, conn, writers, made = make(qtbot, defaults=True)
    qtbot.waitUntil(lambda: view.control.session is not None, timeout=5000)
    view.place_btn.click()  # the user turns it off (Place)
    assert not view.control.active
    assert conn.try_begin_job("sending")
    conn.end_job()
    QApplication.processEvents()
    assert not view.control.active and view.pointer == "place"


def test_control_comes_back_after_a_job_and_after_compose(qtbot):
    view, conn, writers, made = make(qtbot, defaults=True)
    qtbot.waitUntil(lambda: view.control.session is not None, timeout=5000)
    assert conn.try_begin_job("sending")
    qtbot.waitUntil(lambda: not view.control.active, timeout=2000)  # off during the job
    conn.end_job()
    qtbot.waitUntil(lambda: view.control.active, timeout=2000)  # back on after it
    view.set_pointer("place")
    assert not view.control.active
    view.set_pointer("control")
    qtbot.waitUntil(lambda: view.control.active, timeout=2000)


# ---------------------------------------------------------------- Ctrl+V types the clipboard

def test_ctrl_v_types_the_pc_clipboard(qtbot):
    from PySide6.QtGui import QGuiApplication
    view, conn, writers, _ = make(qtbot)
    touch, kb = turn_on(qtbot, view, writers)
    c = view.canvas
    QGuiApplication.clipboard().setText("Hi!\r\nok")
    C = Qt.KeyboardModifier.ControlModifier
    key(c, Qt.Key.Key_Control, mods=C)
    key(c, Qt.Key.Key_V, mods=C, text="\x16")
    key(c, Qt.Key.Key_V, down=False, mods=C)
    key(c, Qt.Key.Key_Control, down=False)
    qtbot.waitUntil(lambda: not view.control._paste_timer.isActive(), timeout=5000)
    keys = [(code, v) for t, code, v in kb.events() if t == KEY]
    # Ctrl alone is never sent and V isn't sent: only the typed characters arrive
    typed = keys
    H, I, ONE, ENTER, O, K = 35, 23, 2, 28, 24, 37
    assert typed == [(42, 1), (H, 1), (H, 0), (42, 0), (I, 1), (I, 0),
                     (42, 1), (ONE, 1), (ONE, 0), (42, 0), (ENTER, 1), (ENTER, 0),
                     (O, 1), (O, 0), (K, 1), (K, 0)]
    assert view.control.message == "Typed 6 characters from the clipboard."
    assert touch.items == []


def test_paste_skips_what_a_us_keyboard_cannot_type(qtbot):
    view, conn, writers, _ = make(qtbot)
    touch, kb = turn_on(qtbot, view, writers)
    view.control.paste("café ü")
    qtbot.waitUntil(lambda: not view.control._paste_timer.isActive(), timeout=5000)
    assert "Typed 4 characters" in view.control.message and "é ü" in view.control.message


def test_paste_stops_when_control_turns_off(qtbot):
    view, conn, writers, _ = make(qtbot)
    touch, kb = turn_on(qtbot, view, writers)
    view.control.paste("x" * 200)
    view.place_btn.click()  # off mid-paste
    n = len([1 for t, code, v in kb.events() if t == KEY])
    QApplication.processEvents()
    qtbot.wait(100)
    assert len([1 for t, code, v in kb.events() if t == KEY]) == n  # nothing after off
    assert not view.control._paste_timer.isActive()


def test_paste_without_folio_does_nothing(qtbot):
    view, conn, writers, _ = make(qtbot, proc=PROC_NO_FOLIO)
    turn_on(qtbot, view, writers, want=("/dev/input/event2",))
    view.control.paste("hello")
    assert not view.control._paste_timer.isActive()



# ---------------------------------------------------------------- zoom

def test_only_1x_is_offered_and_follows_control(qtbot):
    from PySide6.QtWidgets import QPushButton
    view, conn, writers, _ = make(qtbot)
    assert not hasattr(view, "zoom_in_btn") and not hasattr(view, "zoom_out_btn")
    texts = {b.text() for b in view.findChildren(QPushButton)}
    assert "+" not in texts and "\u2212" not in texts and "-" not in texts
    assert not view.zoom_reset_btn.isEnabled()  # Control off: nothing to zoom with
    touch, _ = turn_on(qtbot, view, writers)
    assert view.zoom_reset_btn.isVisible() and view.zoom_reset_btn.isEnabled()
    assert view.zoom_reset_btn.text() == "1x" and "Ctrl+0" in view.zoom_reset_btn.toolTip()
    seen = []
    view.control.zoom_reset = lambda: seen.append("reset") or True
    view.zoom_reset_btn.clicked.disconnect()
    view.zoom_reset_btn.clicked.connect(view.control.zoom_reset)
    view.zoom_reset_btn.click()
    assert seen == ["reset"]


def test_pinch_zoom_still_works_in_code(qtbot):
    view, conn, writers, _ = make(qtbot)
    touch, _ = turn_on(qtbot, view, writers)
    assert view.control.zoom(rc.ZOOM_STEP)  # unused by the UI, kept in code
    ev_ = touch.events()
    slots = [v for t, c, v in ev_ if t == ABS and c == 0x2F]
    assert 0 in slots and 1 in slots and slots[-1] == 0  # two fingers, back to slot 0
    ups = [1 for t, c, v in ev_ if t == ABS and c == TID and v == -1]
    assert len(ups) == 2  # both fingers lifted


def test_ctrl_wheel_does_not_zoom(qtbot):
    view, conn, writers, _ = make(qtbot)
    view.frame = full_frame()
    touch, _ = turn_on(qtbot, view, writers)
    seen = []
    view.control.zoom = lambda f: seen.append(f) or True
    p = view_point(view, 702, 936)
    e = QWheelEvent(QPointF(p), view.canvas.mapToGlobal(QPointF(p)), QPoint(0, 0),
                    QPoint(0, 120), Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier,
                    Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(view.canvas, e)
    assert seen == []


def test_ctrl_0_resets_and_ctrl_minus_plus_do_not_zoom(qtbot):
    view, conn, writers, _ = make(qtbot)
    touch, kb = turn_on(qtbot, view, writers)
    calls = []
    view.control.zoom = lambda f: calls.append(("zoom", f)) or True
    view.control.zoom_reset = lambda: calls.append(("reset",)) or True
    C = Qt.KeyboardModifier.ControlModifier
    key(view.canvas, Qt.Key.Key_0, mods=C)
    key(view.canvas, Qt.Key.Key_0, down=False, mods=C)
    key(view.canvas, Qt.Key.Key_Minus, mods=C)
    key(view.canvas, Qt.Key.Key_Minus, down=False, mods=C)
    key(view.canvas, Qt.Key.Key_Plus, mods=C)
    key(view.canvas, Qt.Key.Key_Plus, down=False, mods=C)
    key(view.canvas, Qt.Key.Key_Equal, mods=C)
    key(view.canvas, Qt.Key.Key_Equal, down=False, mods=C)
    assert calls == [("reset",)]  # Ctrl+minus/plus/= no longer pinch
    assert not [1 for t, c, v in kb.events() if t == KEY and c == 11]  # Ctrl+0 not sent


def test_zoom_reset_taps_the_label_for_the_layout(qtbot, monkeypatch):
    from rmdesk.remote import input as ri_
    from rmdesk.ui import live_compose
    view, conn, writers, _ = make(qtbot)
    touch, _ = turn_on(qtbot, view, writers)
    monkeypatch.setattr(live_compose, "tablet_layout", lambda dev: "keyboard")
    taps = []
    view.control.session.tap = lambda fx, fy: taps.append((fx, fy))
    assert view.control.zoom_reset()
    qtbot.waitUntil(lambda: bool(taps), timeout=3000)
    assert taps == [ri_.ZOOM_LABEL["keyboard"]]



# ---------------------------------------------------------------- modifiers can't stick

def test_modifiers_alone_are_never_sent(qtbot):
    view, conn, writers, _ = make(qtbot)
    touch, kb = turn_on(qtbot, view, writers)
    assert kb.sweeps() == 1  # every modifier released on the tablet when Control starts
    c = view.canvas
    A = Qt.KeyboardModifier.AltModifier
    key(c, Qt.Key.Key_Alt, mods=A)  # e.g. a desktop shortcut swallows the Alt release
    assert kb.events() == []
    key(c, Qt.Key.Key_Minus, text="-")
    key(c, Qt.Key.Key_Minus, down=False, text="-")
    keys = [(code, v) for t, code, v in kb.events() if t == KEY]
    assert keys == [(12, 1), (12, 0)]  # "-" arrives plain: no Alt was held on the tablet


def test_modifier_goes_with_its_key_and_is_lifted_after(qtbot):
    view, conn, writers, _ = make(qtbot)
    touch, kb = turn_on(qtbot, view, writers)
    c = view.canvas
    C = Qt.KeyboardModifier.ControlModifier
    key(c, Qt.Key.Key_Control, mods=C)
    key(c, Qt.Key.Key_A, mods=C, text="a")
    key(c, Qt.Key.Key_A, down=False, mods=C, text="a")
    keys = [(code, v) for t, code, v in kb.events() if t == KEY]
    assert keys == [(29, 1), (30, 1), (30, 0), (29, 0)]
    key(c, Qt.Key.Key_Control, down=False)
    assert [(code, v) for t, code, v in kb.events() if t == KEY] == keys  # nothing more


def test_turning_off_sweeps_the_modifiers(qtbot):
    view, conn, writers, _ = make(qtbot)
    touch, kb = turn_on(qtbot, view, writers)
    view.set_pointer("place")
    assert kb.sweeps() == 2  # on start and on release


# ---------------------------------------------------------------- touch lent to the scroll probe

def _borrow(qtbot, control):
    got = {}
    control.borrow_touch(lambda s: got.update(session=s), lambda m: got.update(fail=m))
    qtbot.waitUntil(lambda: bool(got), timeout=5000)
    return got


def test_borrowed_touch_without_control_is_a_touch_only_session(qtbot):
    view, conn, writers, made = make(qtbot)
    view.set_pointer("place")
    assert not view.control.active
    got = _borrow(qtbot, view.control)
    s = got["session"]
    assert list(writers) == ["/dev/input/event2"] and s.keys is None  # touch only
    s.swipe([(1000, 936), (400, 936)])
    w = writers["/dev/input/event2"]
    assert (ABS, X, 1000) in w.events() and w.events()[-3:] == events(UP)
    assert not view.control.active  # Control itself stays off
    view.control.return_touch()
    assert w.closed


def test_borrowed_touch_while_control_is_on_is_its_session(qtbot):
    view, conn, writers, made = make(qtbot)
    turn_on(qtbot, view, writers)
    n = len(made)
    got = _borrow(qtbot, view.control)
    assert got["session"] is view.control.session and len(made) == n
    view.control.return_touch()
    assert view.control.active and not made[0].closed  # still Control's


def test_a_job_takes_the_borrowed_touch_back(qtbot):
    view, conn, writers, made = make(qtbot)
    view.set_pointer("place")
    got = _borrow(qtbot, view.control)
    assert conn.try_begin_job("sending to the tablet")
    qtbot.waitUntil(lambda: "fail" in got, timeout=2000)
    assert "sending to the tablet" in got["fail"] and writers["/dev/input/event2"].closed
    conn.end_job()


def test_no_borrowing_during_a_job_or_without_a_touch_screen(qtbot):
    view, conn, writers, made = make(qtbot)
    view.set_pointer("place")
    assert conn.try_begin_job("sending to the tablet")
    assert "sending to the tablet" in _borrow(qtbot, view.control)["fail"]
    conn.end_job()
    view, conn, writers, made = make(qtbot, proc=PROC_ALL.replace('"pt_mt"', '"other"'))
    view.set_pointer("place")
    assert _borrow(qtbot, view.control)["fail"] == rc.NO_TOUCH_MSG and made == []
