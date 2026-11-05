"""Placed items follow the page from the tablet's touch events too (ui.touch_watch +
compose.scroll_fuse in LiveCompose): the reading channel's life, a drag on a template-only
page, Control's own swipe read back the same way, and portrait as before."""
import queue
import threading

from rmdesk.compose import frame as fr
from rmdesk.device import DeviceError
from rmdesk.remote import input as ri
from rmdesk.ui import touch_watch as tw
from rmdesk.ui.live_compose import LiveCompose
from tests.fakes import FakeDevice
from tests.scroll_sim import add_dots, blank_page, frame_at
from tests.ui.test_follow_page import show
from tests.ui.test_live_compose import fw, make_device, make_screen  # noqa: F401

DOTS = add_dots(blank_page())
PROC = (b'I: Bus=0000 Vendor=0000 Product=0000 Version=0000\nN: Name="pt_mt"\n'
        b"H: Handlers=event2\n\n")


class FakeReader:
    """The touch device's stdout: bytes pushed by the test (as evdev would hand them out)."""

    def __init__(self, cmd):
        self.cmd = cmd
        self.q: queue.Queue = queue.Queue()
        self.closed = threading.Event()

    def push(self, data: bytes):
        self.q.put(data)

    def die(self):
        self.q.put(DeviceError("Socket is closed"))

    def read(self, n=4096):
        if self.closed.is_set():
            raise DeviceError("closed")
        try:
            item = self.q.get(timeout=0.02)
        except queue.Empty:
            return None
        if isinstance(item, Exception):
            raise item
        return item

    def error(self):
        return ""

    def close(self):
        self.closed.set()


class ReadingDevice(FakeDevice):
    def __init__(self, base: FakeDevice):
        super().__init__(base.files)
        self.files[ri.PROC_DEVICES] = PROC
        self.readers: list[FakeReader] = []

    def open_reader(self, cmd):
        self._call("open_reader", cmd)
        r = FakeReader(cmd)
        self.readers.append(r)
        return r


class FastWatcher(tw.TouchWatcher):
    def __init__(self, parent=None):
        super().__init__(parent, backoff=(0.05, 0.1))


def screen(qtbot, tmp_path, monkeypatch, layout=fr.KEYBOARD):
    monkeypatch.setattr(LiveCompose, "touch_watcher_factory", FastWatcher)
    dev = ReadingDevice(make_device())
    view, panel, *_ = make_screen(qtbot, tmp_path, dev=dev)
    panel.set_layout(layout)
    return view, panel, dev


class Loopback:
    """What a writer to the touch device does, as the reader sees it: the same bytes."""

    def __init__(self, reader):
        self.reader = reader

    def write(self, data):
        self.reader.push(data)

    def pause(self, _s):
        pass

    def close(self):
        pass


def wait_for_drag(qtbot, panel):
    qtbot.waitUntil(lambda: panel._tracker.drag is None and panel._tracker.pred is not None,
                    timeout=3000)


def test_the_channel_opens_with_items_and_closes_without(qtbot, tmp_path, monkeypatch, fw):
    view, panel, dev = screen(qtbot, tmp_path, monkeypatch)
    assert not panel.touch_watch.running and dev.readers == []
    item = panel.add_ink_text("Hi")
    qtbot.waitUntil(lambda: len(dev.readers) == 1, timeout=3000)
    assert dev.readers[0].cmd == "cat /dev/input/event2"  # found by name
    assert ("run", f"cat {ri.PROC_DEVICES}") in dev.log
    assert panel.touch_watch.running
    panel.add_shape("rectangle")
    assert panel.touch_watch.opens == 1  # one channel however many items
    for it in panel.placed_items():
        it.setSelected(True)
    panel.remove_selected()
    qtbot.waitUntil(lambda: not panel.touch_watch.running, timeout=3000)
    assert dev.readers[0].closed.is_set()
    del item
    panel.add_ink_text("Again")
    qtbot.waitUntil(lambda: len(dev.readers) == 2 and panel.touch_watch.running, timeout=3000)
    panel.conn.disconnect()
    qtbot.waitUntil(lambda: not panel.touch_watch.running, timeout=3000)
    assert dev.readers[1].closed.is_set() and panel.touch_watch.device is None


def test_a_dead_channel_is_opened_again(qtbot, tmp_path, monkeypatch, fw):
    view, panel, dev = screen(qtbot, tmp_path, monkeypatch)
    panel.add_ink_text("Hi")
    qtbot.waitUntil(lambda: len(dev.readers) == 1, timeout=3000)
    dev.readers[0].die()
    qtbot.waitUntil(lambda: len(dev.readers) == 2 and panel.touch_watch.running, timeout=3000)
    assert panel.touch_watch.fails == 1
    dev.readers[1].die()
    qtbot.waitUntil(lambda: len(dev.readers) == 3, timeout=3000)
    dev.readers[2].push(ri.SYN)  # events again: the backoff starts over
    qtbot.waitUntil(lambda: panel.touch_watch.fails == 0, timeout=3000)


def test_shutdown_closes_the_channel(qtbot, tmp_path, monkeypatch, fw):
    view, panel, dev = screen(qtbot, tmp_path, monkeypatch)
    panel.add_ink_text("Hi")
    qtbot.waitUntil(lambda: panel.touch_watch.running, timeout=3000)
    panel.shutdown()
    assert not panel.touch_watch.running and dev.readers[0].closed.is_set()


def test_a_finger_drag_on_a_dot_page_moves_the_items(qtbot, tmp_path, monkeypatch, fw):
    """The tablet's own finger: raw input_event bytes, in awkward chunks."""
    view, panel, dev = screen(qtbot, tmp_path, monkeypatch)
    show(view, frame_at(DOTS, 300))
    it = panel.add_ink_text("Hi")
    it.setPos(600, 700)
    show(view, frame_at(DOTS, 300))
    qtbot.waitUntil(lambda: len(dev.readers) == 1, timeout=3000)
    data = b""
    x0, y = 500, 900
    tx, ty = ri.frame_to_touch(x0, y)
    data += (ri.SLOT0 + ri.pack(ri.EV_ABS, ri.ABS_MT_TRACKING_ID, 17)
             + ri.pack(ri.EV_ABS, ri.ABS_MT_POSITION_X, tx)
             + ri.pack(ri.EV_ABS, ri.ABS_MT_POSITION_Y, ty) + ri.SYN)
    for k in range(1, 11):  # 137 px toward higher frame x
        fx, _ = ri.frame_to_touch(x0 + round(13.7 * k), y)
        data += ri.pack(ri.EV_ABS, ri.ABS_MT_POSITION_X, fx) + ri.SYN
    data += ri.pack(ri.EV_ABS, ri.ABS_MT_TRACKING_ID, -1) + ri.SYN
    for i in range(0, len(data), 23):
        dev.readers[0].push(data[i:i + 23])
    wait_for_drag(qtbot, panel)
    show(view, frame_at(DOTS, 437))
    assert it.pos().x() == 737 and it.pos().y() == 700
    assert not panel.page_box.isVisibleTo(panel)


def test_controls_own_swipe_is_followed(qtbot, tmp_path, monkeypatch, fw):
    """Control's injected touches come back through the same reader (evdev gives writes to
    every reader): simulated by looping the session's bytes into it."""
    view, panel, dev = screen(qtbot, tmp_path, monkeypatch)
    show(view, frame_at(DOTS, 600))
    it = panel.add_shape("rectangle")
    it.setPos(800, 900)
    show(view, frame_at(DOTS, 600))
    qtbot.waitUntil(lambda: len(dev.readers) == 1, timeout=3000)
    session = ri.InputSession(Loopback(dev.readers[0]))
    session.swipe([(1000.0, 936.0), (900.0, 936.0), (800.0, 936.0), (790.0, 936.0)])
    wait_for_drag(qtbot, panel)
    show(view, frame_at(DOTS, 390))  # toward the page's top by 210
    assert it.pos().x() == 590


def test_a_pinch_moves_nothing(qtbot, tmp_path, monkeypatch, fw):
    view, panel, dev = screen(qtbot, tmp_path, monkeypatch)
    show(view, frame_at(DOTS, 300))
    it = panel.add_shape("rectangle")
    it.setPos(800, 900)
    show(view, frame_at(DOTS, 300))
    qtbot.waitUntil(lambda: len(dev.readers) == 1, timeout=3000)
    ri.InputSession(Loopback(dev.readers[0])).pinch(2.0)
    qtbot.waitUntil(lambda: panel._tracker.zoomed, timeout=3000)
    assert panel._tracker.pred is None
    show(view, frame_at(DOTS, 300))
    assert it.pos().x() == 800


def test_portrait_is_unchanged(qtbot, tmp_path, monkeypatch, fw):
    view, panel, dev = screen(qtbot, tmp_path, monkeypatch, layout=fr.PORTRAIT_UI)
    show(view, frame_at(DOTS, 300))
    it = panel.add_shape("rectangle")
    it.setPos(800, 900)
    show(view, frame_at(DOTS, 300))
    qtbot.wait(100)
    assert dev.readers == [] and not panel.touch_watch.running  # nothing read
    show(view, frame_at(DOTS, 300))
    assert it.pos().x() == 800
