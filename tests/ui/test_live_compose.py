"""Compose mode on the live Screen view (offscreen; FakeDevice; fake stream client)."""
import io
import json
import math
import threading
from pathlib import Path

import numpy as np
import pytest
import rmscene
from PySide6.QtCore import QPointF, QRectF, Qt, QThreadPool
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QApplication

from rmdesk import firmware, safety
from rmdesk.compose import build, shade, sharptext
from rmdesk.compose.items import Box, PenStyle
from rmdesk.config import Config
from rmdesk.connection import Connection
from rmdesk.device import DeviceError
from rmdesk.library.model import XOCHITL
from rmdesk.notebook import page as pagemod
from rmdesk.notebook.opened import ZOOM_MSG
from rmdesk.notebook.page import VerifyError
from rmdesk.stream.client import Frame
from rmdesk.ui.compose import messages as msg
from rmdesk.ui.compose import shade_job
from rmdesk.ui.compose.confirm import ConfirmBridge
from rmdesk.ui.compose.scene import BLANK, ImageSceneItem
from rmdesk.ui.compose.shade_job import ShadeSettings
from rmdesk.ui.screen_view import ScreenView
from tests.fakes import FakeDevice

RM = Path(__file__).parent.parent / "fixtures" / "rm"
NB = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"
PG1 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PG2 = "ffffffff-0000-1111-2222-333333333333"
PAGE = f"{XOCHITL}/{NB}/{PG2}.rm"  # the open page (lastOpenedPage = 1)
ORIG = (RM / "Normal_AB.rm").read_bytes()
WRITES = ("write_bytes", "rename")


@pytest.fixture(autouse=True)
def drain_workers(qtbot):
    yield
    QThreadPool.globalInstance().waitForDone(10000)
    QApplication.processEvents()


@pytest.fixture(autouse=True)
def pruned(monkeypatch):
    calls = []
    monkeypatch.setattr(safety, "prune",
                        lambda root=None, now=None, protect=(): calls.append((root, list(protect))) or [])
    return calls


@pytest.fixture
def fw(monkeypatch):
    """Firmware reported by the fake tablet; tests may change `fw.info`."""
    class Fw:
        info = firmware.FirmwareInfo("3.29.1.0", "SN123", 6)
    monkeypatch.setattr(firmware, "detect", lambda dev: Fw.info)
    return Fw


class NoSecrets:
    def get(self, k):
        return None

    def set(self, k, v):
        pass


class Creds(NoSecrets):
    def get(self, serial):
        return "admin:pw"


class FakeClient:
    instances: list = []

    def __init__(self, host, password, on_frame, *, username, on_error=None):
        self.on_frame = on_frame
        self.alive = False
        self.calls: list = []
        FakeClient.instances.append(self)

    def start(self):
        self.calls.append("start")
        self.alive = True

    def stop(self):
        self.calls.append("stop")
        self.alive = False

    def resync(self):
        self.calls.append("resync")


@pytest.fixture(autouse=True)
def reset_clients():
    FakeClient.instances = []


def _nb(files, uid, name, opened_at, pages, page_index, page_bytes, orientation="portrait",
        **content_extra):
    meta = {"visibleName": name, "type": "DocumentType", "parent": "",
            "lastOpened": opened_at, "lastOpenedPage": page_index, "lastModified": "1"}
    content = {"fileType": "notebook", "orientation": orientation, "pageCount": len(pages),
               "cPages": {"pages": [{"id": p} for p in pages]}, **content_extra}
    files[f"{XOCHITL}/{uid}.metadata"] = json.dumps(meta).encode()
    files[f"{XOCHITL}/{uid}.content"] = json.dumps(content).encode()
    for p in pages:
        if p in page_bytes:
            files[f"{XOCHITL}/{uid}/{p}.rm"] = page_bytes[p]


def make_device(name="Physics notes", page=ORIG, orientation="portrait", synced=None,
                **content_extra):
    files = {}
    _nb(files, NB, name, "2000", [PG1, PG2], 1, {PG1: ORIG, PG2: page} if page else {PG1: ORIG},
        orientation, **content_extra)
    if synced is not None:
        key = f"{XOCHITL}/{NB}.metadata"
        meta = json.loads(files[key])
        meta["synced"] = synced
        files[key] = json.dumps(meta).encode()
    _nb(files, OTHER, "Older", "1000", ["o1"], 0, {"o1": ORIG})
    return FakeDevice(files)


def make_screen(qtbot, tmp_path, dev=None, cfg=None, compose=True, confirm=True):
    dev = dev or make_device()
    cfg = cfg or Config()
    cfg.confirm_sends = confirm  # the app's default is off; these tests drive the confirm
    conn = Connection(cfg, factory=lambda h, p: dev, secrets=NoSecrets(),
                      sleep=lambda s: None, persist=lambda c: None)
    conn.connect()
    saved = []
    view = ScreenView(conn, keep_default=False, control_default=False, client_factory=FakeClient, pen_factory=FakeClient,
                      status_fn=lambda d: "running", secrets=Creds(),
                      pictures_dir=tmp_path / "Pictures", cfg=cfg,
                      save_config=lambda c: saved.append(json.dumps(c.last_send)),
                      backup_root=tmp_path / "backups")
    qtbot.addWidget(view)
    view.resize(600, 800)
    view.show()
    panel = view.compose
    panel.ask = lambda text: True
    answer_confirm(panel, True)
    if compose:
        view.place_btn.click()
    return view, panel, dev, cfg, saved


def answer_confirm(panel, answer, seen=None):
    """Replace the confirm dialog (shown on the GUI thread) with a recorded answer."""
    def show(text, reply):
        assert threading.current_thread() is threading.main_thread()
        if seen is not None:
            seen.append(text)
        if answer is not None:
            reply(answer)
    panel.bridge.show = show


def idle(panel):
    return lambda: not panel.busy


def layer_labels(data: bytes) -> list[str]:
    tree = rmscene.read_tree(io.BytesIO(data))
    return [g.label.value for g in tree.root.children.values() if hasattr(g, "label")]


def nothing_written(dev):
    return not any(m in WRITES for m, _ in dev.log) and dev.xochitl_active


def frame(value=255):
    return Frame(1404, 1872, np.full((1872, 1404), value, np.uint8))


def red_pixels(img: QImage) -> int:
    img = img.convertToFormat(QImage.Format.Format_RGB32)
    n = 0
    for y in range(0, img.height(), 2):
        for x in range(0, img.width(), 2):
            c = img.pixelColor(x, y)
            if c.red() > 150 and c.green() < 90 and c.blue() < 90:
                n += 1
    return n


def _draw(path: Path, w=200, h=100):
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    p = QPainter(img)
    p.setPen(QPen(QColor("black"), 4))
    p.drawLine(20, 20, w - 20, h - 20)
    p.drawEllipse(30, 10, 60, 60)
    p.end()
    img.save(str(path))
    return path


def record_send(monkeypatch, tmp_path, opened=None):
    """Replace safety.send_to_open_page; it confirms and builds against a real OpenedPage."""
    from rmdesk.notebook.opened import OpenedPage
    calls = []
    opened = opened or OpenedPage(NB, "Physics notes", PG2, 1, 2, "portrait", ORIG)

    def fake(device, build, confirm, **kw):
        calls.append(dict(kw=kw))
        if not confirm(opened):
            raise safety.Cancelled("not sent")
        new_page, n = build(opened)
        calls[-1].update(new_page=new_page, n=n)
        return safety.SendResult(backup_dir=tmp_path / "b" / "1"), opened

    monkeypatch.setattr(safety, "send_to_open_page", fake)
    return calls


# -- the brief's tests ----------------------------------------------------------------

def test_compose_toggle_shows_toolbar_and_hides_present_items(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path, compose=False)
    assert not view.compose_mode and not panel.toolbar.isVisible()
    assert view.canvas.isVisible()
    view.place_btn.click()
    assert view.compose_mode and view.place_btn.isChecked()
    assert panel.toolbar.isVisible() and panel.view.isVisible()
    for b in (panel.add_image_btn, panel.add_ink_btn, panel.remove_btn, panel.tool_combo,
              panel.color_combo, panel.thick_spin, panel.send_btn, panel.undo_btn):
        assert b.isVisible()
    assert not panel.shade_controls.isVisible()  # only while a picture is selected
    assert view.canvas is panel.view  # one picture: the same widget in every pointer
    # the stream keeps running in Place
    qtbot.waitUntil(lambda: any(c.alive for c in FakeClient.instances), timeout=5000)
    client = next(c for c in FakeClient.instances if c.alive)
    client.on_frame(frame(255))
    qtbot.waitUntil(lambda: view.frame is not None, timeout=5000)
    item = panel.add_ink_text("Hello there", size=120)
    item.setPos(200, 300)
    QApplication.processEvents()
    assert red_pixels(panel.view.grab().toImage()) > 0  # items are drawn over the live view
    view.show_present()
    present = view.present
    qtbot.addWidget(present)
    present.resize(540, 720)
    QApplication.processEvents()
    assert red_pixels(present.grab().toImage()) == 0  # Present never shows compose items
    present.hide()
    assert client.alive and "stop" not in client.calls
    view.control_btn.click()  # the pointer is a segmented Control | Place control
    assert not view.compose_mode and not panel.toolbar.isVisible() and view.canvas.isVisible()
    assert panel.placed_items() == [item]  # leaving Place keeps the items
    assert red_pixels(panel.view.grab().toImage()) > 0  # still drawn on the picture


def test_items_stored_in_frame_coordinates(qtbot, tmp_path, fw, monkeypatch):
    calls = record_send(monkeypatch, tmp_path)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    assert panel.scene.sceneRect() == QRectF(0, 0, 1404, 1872)
    assert panel.scene.parent() is panel  # Qt-owned: never deleted by GC on a worker thread
    item = panel.add_ink_text("Hi")
    item.setPos(60, 60)
    assert item.box() == Box(60, 60, item.w, item.h)
    panel.send()
    qtbot.waitUntil(lambda: bool(calls) and not panel.busy, timeout=5000)
    pts = [pt for ln in _new_lines(calls[0]["new_page"]) for pt in ln.points]
    xs, ys = [pt.x for pt in pts], [pt.y for pt in pts]
    ink_x = [x for pl in item.local_polylines() for x, _ in pl]
    ink_y = [y for pl in item.local_polylines() for _, y in pl]
    assert min(xs) == pytest.approx(60 + min(ink_x) - 702)
    assert min(ys) == pytest.approx(60 + min(ink_y))


def _new_lines(data: bytes):
    tree = rmscene.read_tree(io.BytesIO(data))
    for g in tree.root.children.values():
        if getattr(g, "label", None) and g.label.value == "rmdesk":
            return [v for v in g.children.values() if hasattr(v, "points")]
    return []


@pytest.mark.parametrize("rot", [90, 180, 270])
def test_rotation_does_not_move_items(qtbot, tmp_path, fw, rot):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_ink_text("Hi")
    item.setPos(100, 200)
    before_view = panel.view.mapFromScene(QPointF(100, 200))
    while view.rotation != rot:
        view.rotate()
    QApplication.processEvents()
    assert item.pos() == QPointF(100, 200) and item.box().x == 100
    assert panel.view.mapFromScene(QPointF(100, 200)) != before_view  # only the drawing moved
    # the view shows the whole frame, rotated as the Screen view is
    t = panel.view.transform()
    s = math.hypot(t.m11(), t.m12())
    assert (round(t.m11() / s), round(t.m12() / s)) == {90: (0, 1), 180: (-1, 0), 270: (0, -1)}[rot]
    view._on_frame_thread(frame(200))  # a frame arriving never moves items
    QApplication.processEvents()
    assert view.frame is not None and item.pos() == QPointF(100, 200)


def test_items_outside_page_are_clipped(qtbot, tmp_path, fw, monkeypatch):
    calls = record_send(monkeypatch, tmp_path)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_ink_text("Hello there", size=120)
    item.setPos(1404 - item.w / 2, 1872 - item.h / 2)  # hangs off the bottom-right corner
    panel.send()
    qtbot.waitUntil(lambda: bool(calls) and not panel.busy, timeout=5000)
    pts = [pt for ln in _new_lines(calls[0]["new_page"]) for pt in ln.points]
    assert pts and calls[0]["n"] > 0
    for pt in pts:
        assert -702 <= pt.x <= 702 and 0 <= pt.y <= 1872


def test_send_flow(qtbot, tmp_path, fw):
    view, panel, dev, cfg, saved = make_screen(qtbot, tmp_path)
    seen_jobs, seen = [], []

    def show(text, reply):
        seen.append(text)
        seen_jobs.append(view.conn.current_job)
        assert not dev.xochitl_active  # asked while xochitl is stopped
        reply(True)
    panel.bridge.show = show
    panel.add_ink_text("Hello").setPos(200, 300)
    panel.add_ink_text("World").setPos(200, 600)
    panel.send()
    qtbot.waitUntil(lambda: cfg.last_send is not None and not panel.busy, timeout=10000)
    assert seen == ["Add 2 items to “Physics notes”, page 2 of 2?"]
    assert seen_jobs == ["sending to the tablet"]
    assert view.conn.current_job is None
    assert dev.xochitl_active
    assert cfg.last_send["notebook_id"] == NB and cfg.last_send["name"] == "Physics notes"
    bdir = Path(cfg.last_send["backup_dir"])
    assert (bdir / "manifest.json").is_file() and bdir.is_relative_to(tmp_path / "backups")
    assert saved and json.loads(saved[-1]) == cfg.last_send
    assert dev.files[PAGE] != ORIG and "rmdesk" in layer_labels(dev.files[PAGE])
    assert dev.files[f"{XOCHITL}/{NB}/{PG1}.rm"] == ORIG
    assert panel.placed_items() == []
    assert panel.message.text().startswith("Sent to \u201cPhysics notes\u201d")
    assert panel.undo_btn.isEnabled()


def test_confirm_singular_and_hidden_layer_warning(qtbot, tmp_path, fw, monkeypatch):
    monkeypatch.setattr(pagemod.Page, "rmdesk_layer_hidden", lambda self: True)
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path)
    seen = []
    answer_confirm(panel, False, seen)
    panel.add_ink_text("Hello")
    panel.send()
    qtbot.waitUntil(lambda: bool(seen) and not panel.busy, timeout=10000)
    assert seen == ["Add 1 item to “Physics notes”, page 2 of 2?\n\n"
                    "The rmdesk layer is hidden on this page — show it in Layers on the "
                    "tablet to see the new ink."]


def test_confirm_names_notebook_and_page(qtbot, tmp_path, fw):
    # the tablet shows the library (no notebook open): the most recent notebook is named so
    # the user can say No
    dev = make_device(name="Diary")
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path, dev=dev)
    seen = []
    answer_confirm(panel, False, seen)
    item = panel.add_ink_text("Hello")
    dev.log.clear()
    panel.send()
    qtbot.waitUntil(lambda: bool(seen) and not panel.busy, timeout=10000)
    assert seen == ["Add 1 item to “Diary”, page 2 of 2?"]
    assert nothing_written(dev) and dev.files[PAGE] == ORIG
    assert panel.placed_items() == [item]
    assert panel.status.text() == "Nothing was sent."
    assert panel.message.text() == ""
    assert view.conn.device is not None and view.conn.current_job is None
    assert cfg.last_send is None


def test_confirm_timeout_is_no(qtbot, tmp_path, fw):
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path)
    panel.bridge.timeout = 0.2
    seen = []
    answer_confirm(panel, None, seen)  # the user walked away: no answer at all
    item = panel.add_ink_text("Hello")
    panel.send()
    qtbot.waitUntil(lambda: bool(seen) and not panel.busy, timeout=10000)
    assert nothing_written(dev) and dev.files[PAGE] == ORIG
    assert panel.placed_items() == [item]
    assert panel.status.text() == "Nothing was sent."
    assert view.conn.current_job is None and view.conn.device is not None


def test_confirm_bridge_defaults_and_late_answer(qtbot):
    b = ConfirmBridge()
    assert b.timeout == 60.0
    replies = []
    b.show = lambda text, reply: replies.append(reply)
    b.timeout = 0.1
    out = []
    t = threading.Thread(target=lambda: out.append(b.confirm("q?")))
    t.start()
    qtbot.waitUntil(lambda: not t.is_alive(), timeout=5000)
    t.join(1)
    QApplication.processEvents()
    assert out == [False]
    for r in replies:
        r(True)  # a late Yes after the timeout changes nothing
    assert out == [False]
    b.show = lambda text, reply: reply(True)
    b.timeout = 5
    t = threading.Thread(target=lambda: out.append(b.confirm("q?")))
    t.start()
    qtbot.waitUntil(lambda: not t.is_alive(), timeout=5000)
    assert out == [False, True]


@pytest.mark.parametrize("kind", ["zoom", "blank", "pdf"])
def test_not_editable_shows_reason(qtbot, tmp_path, fw, kind):
    from rmdesk.notebook import opened as op
    if kind == "zoom":
        dev, want = make_device(zoomMode="custom", customZoomScale=2), ZOOM_MSG
    elif kind == "blank":
        dev, want = make_device(page=None), op.BLANK_PAGE_MSG
    else:
        dev = make_device()
        content = json.loads(dev.files[f"{XOCHITL}/{NB}.content"])
        content["fileType"] = "pdf"
        dev.files[f"{XOCHITL}/{NB}.content"] = json.dumps(content).encode()
        want = op.PDF_MSG
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path, dev=dev)
    seen = []
    answer_confirm(panel, True, seen)
    item = panel.add_ink_text("Hello")
    dev.log.clear()
    panel.send()
    qtbot.waitUntil(lambda: bool(panel.message.text()) and not panel.busy, timeout=10000)
    assert panel.message.text() == want
    assert seen == [] and nothing_written(dev)
    assert panel.placed_items() == [item]
    assert view.conn.device is not None and view.conn.current_job is None


def test_landscape_page_is_refused_before_any_write(qtbot, tmp_path, fw):
    dev = make_device(orientation="landscape")
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path, dev=dev)
    seen = []
    answer_confirm(panel, True, seen)
    item = panel.add_ink_text("Hello")
    dev.log.clear()
    panel.send()
    qtbot.waitUntil(lambda: bool(panel.message.text()) and not panel.busy, timeout=10000)
    assert panel.message.text() == (
        "Sending to landscape pages is turned off while rmdesk re-checks landscape "
        "positions. Use a portrait page for now.")
    assert seen == [] and nothing_written(dev)  # no question asked; xochitl started again
    assert cfg.last_send is None
    assert panel.placed_items() == [item]
    assert view.conn.device is not None and view.conn.current_job is None


def test_landscape_refused_before_build(qtbot, tmp_path, fw, monkeypatch):
    from rmdesk.notebook.opened import OpenedPage
    op = OpenedPage(NB, "Physics notes", PG2, 1, 2, "landscape", ORIG)
    calls = record_send(monkeypatch, tmp_path, opened=op)
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path)
    monkeypatch.setattr(build, "frame_to_lines",
                        lambda *a: pytest.fail("built for a landscape page"))
    panel.add_ink_text("Hello")
    panel.send()
    qtbot.waitUntil(lambda: bool(panel.message.text()) and not panel.busy, timeout=10000)
    assert panel.message.text() == msg.LANDSCAPE_OFF
    assert len(calls) == 1 and "new_page" not in calls[0]


def test_undo(qtbot, tmp_path, fw):
    view, panel, dev, cfg, saved = make_screen(qtbot, tmp_path)
    panel.add_ink_text("Hello")
    panel.send()
    qtbot.waitUntil(lambda: cfg.last_send is not None and not panel.busy, timeout=10000)
    asked, jobs = [], []
    panel.ask = lambda text: asked.append(text) or True
    real = safety.undo_last

    def spy(*a, **k):
        jobs.append(view.conn.current_job)
        return real(*a, **k)
    safety.undo_last, old = spy, safety.undo_last
    try:
        assert "“Physics notes”" in panel.undo_btn.toolTip()
        panel.undo()
        qtbot.waitUntil(lambda: cfg.last_send is None and not panel.busy, timeout=10000)
    finally:
        safety.undo_last = old
    assert asked == [msg.UNDO_CONFIRM.format(name="“Physics notes”")]
    assert jobs == ["undoing the last send"] and view.conn.current_job is None
    assert dev.files[PAGE] == ORIG
    assert panel.message.text().startswith("The last send to “Physics notes” was undone.")
    assert json.loads(saved[-1]) is None
    assert not panel.undo_btn.isEnabled()


def test_no_compose_page_in_the_window(qtbot, tmp_path):
    from rmdesk.ui.app import MainWindow

    def down(h, p):
        raise DeviceError("down")
    cfg = Config()
    conn = Connection(cfg, factory=down, secrets=NoSecrets(), sleep=lambda s: None,
                      persist=lambda c: None)
    win = MainWindow(conn, cfg, save_config=lambda c: None)
    qtbot.addWidget(win)
    from rmdesk.ui.app import VIEWS
    assert VIEWS == ["Files", "Screen", "Device"]  # no Compose page: Place is in Screen
    assert not hasattr(win, "compose_view")


def test_no_typed_text_action(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    from PySide6.QtWidgets import QPushButton
    texts = [b.text() for b in view.findChildren(QPushButton)]
    assert not any("typed" in t.lower() for t in texts)
    assert not hasattr(panel, "add_typed_text") and not hasattr(msg, "ASCII_ONLY")
    import importlib.util
    for gone in ("rmdesk.ui.compose_view", "rmdesk.ui.compose.editor", "rmdesk.ui.compose.work"):
        assert importlib.util.find_spec(gone) is None


def test_testing_mode_guard_uses_identified_notebook(qtbot, tmp_path, fw):
    fw.info = firmware.FirmwareInfo("3.30.0.0", "SN123", 6)  # untested firmware: test-only
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path)  # the open notebook isn't "rmdesk test"
    assert panel.mode == "test-only" and panel.banner.isVisible()
    seen = []
    answer_confirm(panel, True, seen)
    item = panel.add_ink_text("Hello")
    assert panel.send_btn.isEnabled()  # the notebook is only known after the stop
    dev.log.clear()
    panel.send()
    qtbot.waitUntil(lambda: bool(panel.message.text()) and not panel.busy, timeout=10000)
    assert panel.message.text() == panel.banner.text()
    assert seen == []  # refused before asking
    assert nothing_written(dev) and dev.files[PAGE] == ORIG
    assert panel.placed_items() == [item] and cfg.last_send is None


def test_testing_mode_guard_passed_and_rmdesk_test_is_written(qtbot, tmp_path, fw):
    fw.info = firmware.FirmwareInfo("3.30.0.0", "SN123", 6)
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path, dev=make_device(name="rmdesk test"))
    panel.add_ink_text("Hello")
    panel.send()
    qtbot.waitUntil(lambda: cfg.last_send is not None and not panel.busy, timeout=10000)
    assert "rmdesk" in layer_labels(dev.files[PAGE])


def test_testing_mode_guard_rejects_on_the_tablet(qtbot, tmp_path, fw, monkeypatch):
    # the name check before the confirm passes, but the backup's metadata (read by the guard)
    # says otherwise: nothing is written
    fw.info = firmware.FirmwareInfo("3.30.0.0", "SN123", 6)
    dev = make_device(name="rmdesk test")
    real_backup = safety.backup_page  # Compose sends take a page-scoped backup

    def backup(device, notebook_id, page_id, **kw):
        key = f"{XOCHITL}/{notebook_id}.metadata"
        meta = json.loads(device.files[key])
        meta["visibleName"] = "Diary"
        device.files[key] = json.dumps(meta).encode()
        return real_backup(device, notebook_id, page_id, **kw)
    monkeypatch.setattr(safety, "backup_page", backup)
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path, dev=dev)
    panel.add_ink_text("Hello")
    dev.log.clear()
    panel.send()
    qtbot.waitUntil(lambda: bool(panel.message.text()) and not panel.busy, timeout=10000)
    assert panel.message.text() == panel.banner.text()
    assert nothing_written(dev) and dev.files[PAGE] == ORIG and cfg.last_send is None


def test_guard_not_passed_in_full_mode(qtbot, tmp_path, fw, monkeypatch):
    calls = record_send(monkeypatch, tmp_path)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    panel.add_ink_text("Hi")
    panel.send()
    qtbot.waitUntil(lambda: bool(calls) and not panel.busy, timeout=5000)
    assert calls[0]["kw"].get("guard") is None
    assert calls[0]["kw"]["root"] == tmp_path / "backups"


def test_testing_mode_undo_only_for_the_acceptance_notebook(qtbot, tmp_path, fw, monkeypatch):
    fw.info = firmware.FirmwareInfo("3.30.0.0", "SN123", 6)
    undone = []
    monkeypatch.setattr(safety, "undo_last",
                        lambda dev, cfg, root=None, guard=None: undone.append(guard)
                        or safety.RestoreResult(safety_backup=tmp_path / "sb"))
    cfg = Config(last_send={"notebook_id": NB, "name": "Diary",
                            "backup_dir": str(tmp_path / "b" / "1")})
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path, cfg=cfg)
    assert not panel.undo_btn.isEnabled()
    dev.log.clear()
    panel.undo()
    assert undone == [] and dev.log == []
    assert panel.message.text() == panel.banner.text()
    cfg.last_send = {"notebook_id": NB, "name": "rmdesk test",
                     "backup_dir": str(tmp_path / "b" / "1")}
    panel._update_actions()
    assert panel.undo_btn.isEnabled()
    panel.undo()
    qtbot.waitUntil(lambda: len(undone) == 1 and not panel.busy, timeout=5000)
    assert callable(undone[0])


# -- ported from the old Compose view -----------------------------------------------------

def test_compose_disabled_for_other_rm_format(qtbot, tmp_path, fw):
    fw.info = firmware.FirmwareInfo("3.30.0.0", "SN123", 5)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    assert panel.mode == "off" and not panel.allowed
    assert panel.disabled_label.isVisible()
    assert panel.disabled_label.text() == firmware.COMPOSE_DISABLED_MSG
    for b in (panel.send_btn, panel.undo_btn, panel.add_image_btn, panel.add_ink_btn):
        assert not b.isEnabled()


def test_item_geometry_to_box(qtbot):
    item = ImageSceneItem(Path("x.png"), 100, 100, PenStyle())
    item.setPos(200, 300)
    item.set_size(400, 200)
    assert item.box() == Box(200, 300, 400, 200)


def test_notebook_changed_keeps_items(qtbot, tmp_path, fw, monkeypatch):
    def fail(*a, **k):
        raise safety.NotebookChanged("page bytes differ")
    monkeypatch.setattr(safety, "send_to_open_page", fail)
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path)
    item = panel.add_ink_text("Hello")
    panel.send()
    qtbot.waitUntil(lambda: panel.message.text() == msg.CHANGED and not panel.busy, timeout=10000)
    assert panel.placed_items() == [item] and cfg.last_send is None
    assert panel.details.text()


def test_send_failed_not_restored_names_backup(qtbot, tmp_path, fw, monkeypatch):
    bdir = tmp_path / "backups" / NB / "20261103-120000"

    def fail(*a, **k):
        raise safety.SendFailed(DeviceError("write broke"), restored=False, backup_dir=bdir,
                                restore_error=DeviceError("restore broke"),
                                restart_error=safety.RestartTimeout("not active"))
    monkeypatch.setattr(safety, "send_to_open_page", fail)
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path)
    item = panel.add_ink_text("Hi")
    panel.send()
    qtbot.waitUntil(lambda: bool(panel.message.text()) and not panel.busy, timeout=5000)
    text = panel.message.text()
    assert "putting it back also failed" in text and str(bdir) in text
    assert msg.SCREEN_RESTART in text and "write broke" in panel.details.text()
    assert panel.placed_items() == [item] and cfg.last_send is None


def test_send_failed_restored_message(qtbot, tmp_path, fw, monkeypatch):
    def fail(*a, **k):
        raise safety.SendFailed(DeviceError("x"), restored=True, backup_dir=tmp_path / "bk")
    monkeypatch.setattr(safety, "send_to_open_page", fail)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    panel.add_ink_text("Hi")
    panel.send()
    qtbot.waitUntil(lambda: bool(panel.message.text()) and not panel.busy, timeout=5000)
    assert panel.message.text().startswith(msg.SEND_RESTORED)
    assert str(tmp_path / "bk") in panel.message.text()


@pytest.mark.parametrize("exc, want", [
    (safety.StopTimeout("slow"), msg.NOT_RESPONDING),
    (VerifyError("bad"), msg.CANT_EDIT),
    (safety.BackupError("disk full"), msg.BACKUP_FAILED),
])
def test_send_errors_before_writing(qtbot, tmp_path, fw, monkeypatch, exc, want):
    def fail(*a, **k):
        raise exc
    monkeypatch.setattr(safety, "send_to_open_page", fail)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    panel.add_ink_text("Hi")
    panel.send()
    qtbot.waitUntil(lambda: bool(panel.message.text()) and not panel.busy, timeout=5000)
    assert panel.message.text() == want
    assert view.conn.device is not None  # a slow xochitl is not a lost connection


def test_busy_state_during_send(qtbot, tmp_path, fw, monkeypatch):
    gate = threading.Event()

    def slow(*a, **k):
        gate.wait(5)
        raise safety.Cancelled("not sent")
    monkeypatch.setattr(safety, "send_to_open_page", slow)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_ink_text("Hi")
    panel.send()
    try:
        assert panel.busy and panel.status.text() == msg.SENDING
        for b in (panel.send_btn, panel.undo_btn, panel.add_image_btn, panel.add_ink_btn):
            assert not b.isEnabled()
        assert not (item.flags() & item.GraphicsItemFlag.ItemIsMovable)
    finally:
        gate.set()
    qtbot.waitUntil(idle(panel), timeout=5000)
    assert item.flags() & item.GraphicsItemFlag.ItemIsMovable


def test_nothing_on_page_is_refused_before_any_device_call(qtbot, tmp_path, fw, monkeypatch):
    calls = record_send(monkeypatch, tmp_path)
    view, panel, dev, *_ = make_screen(qtbot, tmp_path)
    panel.add_ink_text("Hi").setPos(3000, 3000)
    dev.log.clear()
    panel.send()
    assert panel.message.text() == msg.NOTHING_TO_SEND
    assert calls == [] and dev.log == []


def test_ink_skipped_chars_warn(qtbot, tmp_path, fw, monkeypatch):
    calls = record_send(monkeypatch, tmp_path)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    asked = []
    panel.ask = lambda text: asked.append(text) or False
    panel.add_ink_text("Hi ü")
    panel.send()
    assert asked and msg.INK_SKIPPED.format(chars="ü") in asked[0]
    assert calls == []


def test_remove_and_pen(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_ink_text("Hi")
    assert item.isSelected() and panel.remove_btn.isEnabled()
    panel.color_combo.setCurrentText("grey")
    assert item.pen.color == "grey"
    panel.remove_btn.click()
    assert panel.placed_items() == [] and not panel.send_btn.isEnabled()


def test_undo_failure_names_safety_backup(qtbot, tmp_path, fw, monkeypatch):
    def fail(device, cfg, **kw):
        raise safety.UndoFailed(DeviceError("x"), tmp_path / "safety", files_written=False)
    monkeypatch.setattr(safety, "undo_last", fail)
    cfg = Config(last_send={"notebook_id": NB, "name": "Physics notes",
                            "backup_dir": str(tmp_path / "b")})
    view, panel, *_ = make_screen(qtbot, tmp_path, cfg=cfg)
    panel.undo()
    qtbot.waitUntil(lambda: bool(panel.message.text()) and not panel.busy, timeout=5000)
    assert str(tmp_path / "safety") in panel.message.text()
    assert msg.NOTHING_CHANGED in panel.message.text()
    assert cfg.last_send is not None


def test_undo_done_but_screen_off(qtbot, tmp_path, fw, monkeypatch):
    def fail(device, cfg, **kw):
        e = safety.RestartTimeout("not active")
        raise safety.UndoFailed(e, tmp_path / "safety", files_written=True, restart_error=e)
    monkeypatch.setattr(safety, "undo_last", fail)
    cfg = Config(last_send={"notebook_id": NB, "backup_dir": str(tmp_path / "b")})
    view, panel, _, _, saved = make_screen(qtbot, tmp_path, cfg=cfg)
    panel.undo()
    qtbot.waitUntil(lambda: cfg.last_send is None and not panel.busy, timeout=5000)
    assert panel.message.text() == msg.UNDO_NO_SCREEN
    assert json.loads(saved[-1]) is None


def test_undo_of_an_old_send_without_a_name(qtbot, tmp_path, fw):
    cfg = Config(last_send={"notebook_id": NB, "backup_dir": str(tmp_path / "b")})
    view, panel, *_ = make_screen(qtbot, tmp_path, cfg=cfg)
    asked = []
    panel.ask = lambda text: asked.append(text) or False
    assert panel.undo_btn.isEnabled()
    panel.undo()
    assert asked == [msg.UNDO_CONFIRM.format(name=msg.UNKNOWN_NOTEBOOK)]
    assert cfg.last_send is not None


def test_prune_at_startup_protects_last_send(qtbot, tmp_path, fw, pruned):
    bdir = tmp_path / "backups" / NB / "20261103-120000"
    cfg = Config(last_send={"notebook_id": NB, "backup_dir": str(bdir)})
    make_screen(qtbot, tmp_path, cfg=cfg)
    QThreadPool.globalInstance().waitForDone(5000)
    assert pruned == [(tmp_path / "backups", [bdir])]


def test_send_refused_while_device_job_runs(qtbot, tmp_path, fw, monkeypatch):
    calls = record_send(monkeypatch, tmp_path)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    panel.add_ink_text("Hi")
    assert view.conn.try_begin_job("uploading files")
    panel.send()
    QThreadPool.globalInstance().waitForDone(5000)
    QApplication.processEvents()
    assert calls == [] and panel.busy is None
    assert panel.message.text() == ("The tablet is busy with: uploading files. "
                                    "Try again when it's done.")
    assert view.conn.current_job == "uploading files"


def test_send_releases_job_on_error(qtbot, tmp_path, fw, monkeypatch):
    seen = []
    view, panel, *_ = make_screen(qtbot, tmp_path)

    def fail(*a, **k):
        seen.append(view.conn.current_job)
        raise safety.BackupError("disk full")
    monkeypatch.setattr(safety, "send_to_open_page", fail)
    panel.add_ink_text("Hi")
    panel.send()
    qtbot.waitUntil(lambda: bool(seen) and not panel.busy, timeout=5000)
    assert seen == ["sending to the tablet"] and view.conn.current_job is None


def test_undo_refused_while_device_job_runs(qtbot, tmp_path, fw, monkeypatch):
    calls = []
    monkeypatch.setattr(safety, "undo_last", lambda *a, **k: calls.append(1))
    cfg = Config(last_send={"notebook_id": NB, "backup_dir": str(tmp_path / "b")})
    view, panel, *_ = make_screen(qtbot, tmp_path, cfg=cfg)
    view.conn.try_begin_job("changing fast USB uploads")
    panel.undo()
    QThreadPool.globalInstance().waitForDone(5000)
    QApplication.processEvents()
    assert calls == [] and panel.busy is None
    assert "busy with: changing fast USB uploads" in panel.message.text()


def test_send_and_undo_clear_pending_refresh(qtbot, tmp_path, fw):
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path)
    view.conn.add_pending(2)
    panel.add_ink_text("Hello")
    panel.send()
    qtbot.waitUntil(lambda: cfg.last_send is not None and not panel.busy, timeout=10000)
    assert view.conn.pending_refresh == 0
    view.conn.add_pending(1)
    panel.undo()
    qtbot.waitUntil(lambda: cfg.last_send is None and not panel.busy, timeout=10000)
    assert view.conn.pending_refresh == 0


def test_send_outcome_stays_visible_when_offline(qtbot, tmp_path, fw, monkeypatch):
    bdir = tmp_path / "backups" / NB / "20261103-120000"

    def fail(*a, **k):
        raise safety.SendFailed(DeviceError("x"), restored=False, backup_dir=bdir)
    monkeypatch.setattr(safety, "send_to_open_page", fail)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    panel.add_ink_text("Hi")
    panel.send()
    qtbot.waitUntil(lambda: bool(panel.message.text()) and not panel.busy, timeout=5000)
    view.conn.disconnect()
    qtbot.waitUntil(lambda: not panel.allowed, timeout=5000)
    assert panel.message.isVisible() and str(bdir) in panel.message.text()


def _shown_parent(qtbot):
    from PySide6.QtWidgets import QWidget
    parent = QWidget()
    qtbot.addWidget(parent)
    parent.resize(700, 500)
    parent.show()
    return parent


def _sheet_of(parent):
    from rmdesk.ui.widgets.sheet import ConfirmSheet
    return next((w for w in parent.findChildren(ConfirmSheet) if w.isVisible()), None)


def test_confirm_bridge_default_sheet_yes_and_timeout(qtbot):
    parent = _shown_parent(qtbot)
    b = ConfirmBridge(parent, timeout=0.2)
    replies = []
    b._sheet("Add 1 item?", replies.append)  # non-blocking, on the GUI thread
    sheet = _sheet_of(parent)
    assert sheet.body.text() == "Add 1 item?"
    assert sheet.no_btn.isDefault() and parent.focusWidget() is sheet.no_btn
    assert sheet.note.text() == "The tablet is paused until you answer."
    sheet.yes_btn.click()
    qtbot.waitUntil(lambda: replies == [True], timeout=5000)
    assert _sheet_of(parent) is None
    b._sheet("Again?", replies.append)
    qtbot.waitUntil(lambda: replies == [True, False], timeout=5000)  # closed by the timeout
    qtbot.waitUntil(lambda: _sheet_of(parent) is None, timeout=2000)


# -- fix round L7/L8 ------------------------------------------------------------------

SYNC_LINE = ("This notebook syncs with the reMarkable cloud. If it was edited elsewhere since its "
             "last sync, the cloud copy may conflict with this change.")


def test_confirm_warns_on_every_send_to_a_synced_notebook(qtbot, tmp_path, fw):
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path, dev=make_device(synced=True))
    seen = []
    answer_confirm(panel, True, seen)
    for _ in range(2):
        panel.add_ink_text("Hello")
        panel.send()
        qtbot.waitUntil(lambda: cfg.last_send is not None and not panel.busy, timeout=10000)
        cfg.last_send = None
    want = "Add 1 item to \u201cPhysics notes\u201d, page 2 of 2?\n\n" + SYNC_LINE
    assert seen == [want, want]


def test_confirm_has_no_sync_line_for_unsynced_notebook(qtbot, tmp_path, fw):
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path, dev=make_device(synced=False))
    seen = []
    answer_confirm(panel, False, seen)
    panel.add_ink_text("Hello")
    panel.send()
    qtbot.waitUntil(lambda: bool(seen) and not panel.busy, timeout=10000)
    assert seen == ["Add 1 item to \u201cPhysics notes\u201d, page 2 of 2?"]


def test_confirm_text_order_hidden_layer_then_sync():
    from rmdesk.notebook.opened import OpenedPage
    op = OpenedPage(NB, "N", PG2, 0, 1, "portrait", b"", synced=True)
    assert msg.confirm_text(3, op, hidden_layer=True) == (
        "Add 3 items to \u201cN\u201d, page 1 of 1?\n\n" + msg.HIDDEN_LAYER + "\n\n" + SYNC_LINE)


def test_link_failure_behind_not_editable_marks_connection_lost(qtbot, tmp_path, fw):
    from rmdesk.device import DeviceError
    from tests.fakes import FailRule
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path)
    lost = []
    real = view.conn.connection_lost
    view.conn.connection_lost = lambda e: (lost.append(e), real(e))
    panel.add_ink_text("Hello")
    dev.fail_on["listdir"] = FailRule(DeviceError("ssh link dropped"), times=1)
    panel.send()
    qtbot.waitUntil(lambda: bool(panel.message.text()) and not panel.busy, timeout=10000)
    from rmdesk.notebook.opened import BAD_FILES_MSG
    assert panel.message.text() == BAD_FILES_MSG  # the reason is still shown
    assert len(lost) == 1 and str(lost[0]) == "ssh link dropped"
    assert view.conn.current_job is None


def test_not_editable_without_link_failure_keeps_connection(qtbot, tmp_path, fw):
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path, dev=make_device(page=None))
    lost = []
    view.conn.connection_lost = lambda e: lost.append(e)
    panel.add_ink_text("Hello")
    panel.send()
    qtbot.waitUntil(lambda: bool(panel.message.text()) and not panel.busy, timeout=10000)
    assert lost == []


def test_shutdown_during_pending_confirm_answers_no_at_once(qtbot, tmp_path, fw):
    import time
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path)
    panel.bridge.timeout = 30  # long: only cancel() can end the wait quickly
    pending = []
    panel.bridge.show = lambda text, reply: pending.append(reply)  # dialog left open
    panel.add_ink_text("Hello")
    dev.log.clear()
    panel.send()
    qtbot.waitUntil(lambda: bool(pending), timeout=10000)
    assert view.conn.current_job == "sending to the tablet"
    t0 = time.monotonic()
    view.shutdown()
    qtbot.waitUntil(lambda: not panel.busy, timeout=5000)
    assert time.monotonic() - t0 < 1.0
    assert nothing_written(dev) and dev.files[PAGE] == ORIG
    assert view.conn.current_job is None
    assert panel.status.text() == "Nothing was sent."


def test_bridge_cancel_closes_the_default_sheet(qtbot):
    parent = _shown_parent(qtbot)
    b = ConfirmBridge(parent, timeout=30)
    out = []
    t = threading.Thread(target=lambda: out.append(b.confirm("Add 1 item?")))
    t.start()
    qtbot.waitUntil(lambda: _sheet_of(parent) is not None, timeout=5000)
    b.cancel()
    qtbot.waitUntil(lambda: not t.is_alive(), timeout=2000)
    assert out == [False]
    qtbot.waitUntil(lambda: _sheet_of(parent) is None, timeout=2000)
    b.cancel()  # nothing pending: harmless


def test_sheet_yes_no_and_worker_waits(qtbot):
    parent = _shown_parent(qtbot)
    b = ConfirmBridge(parent, timeout=30)
    for button, want in (("yes_btn", True), ("no_btn", False)):
        out = []
        t = threading.Thread(target=lambda: out.append(b.confirm("Add 2 items?", ["Picture: a.png"])))
        t.start()
        qtbot.waitUntil(lambda: _sheet_of(parent) is not None, timeout=5000)
        sheet = _sheet_of(parent)
        assert sheet.items_label.text() == "\u2022 Picture: a.png"
        qtbot.wait(100)
        assert t.is_alive() and out == []  # the worker keeps waiting (modal in effect)
        getattr(sheet, button).click()
        qtbot.waitUntil(lambda: not t.is_alive(), timeout=2000)
        assert out == [want]
        qtbot.waitUntil(lambda: _sheet_of(parent) is None, timeout=2000)


def test_sheet_escape_is_no_and_timeout_is_no(qtbot):
    parent = _shown_parent(qtbot)
    b = ConfirmBridge(parent, timeout=0.3)
    out = []
    t = threading.Thread(target=lambda: out.append(b.confirm("Add 1 item?")))
    t.start()
    qtbot.waitUntil(lambda: not t.is_alive(), timeout=5000)  # nobody answers
    assert out == [False]
    qtbot.waitUntil(lambda: _sheet_of(parent) is None, timeout=2000)
    b.timeout = 30
    out.clear()
    t = threading.Thread(target=lambda: out.append(b.confirm("Add 1 item?")))
    t.start()
    qtbot.waitUntil(lambda: _sheet_of(parent) is not None, timeout=5000)
    qtbot.keyClick(_sheet_of(parent).no_btn, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: not t.is_alive(), timeout=2000)
    assert out == [False]


@pytest.mark.parametrize("how", ["close", "hide"])
def test_sheet_cancel_on_close(qtbot, how):
    """Closing the window, or hiding the view, while the sheet is up answers No."""
    parent = _shown_parent(qtbot)
    b = ConfirmBridge(parent, timeout=30)
    out = []
    t = threading.Thread(target=lambda: out.append(b.confirm("Add 1 item?")))
    t.start()
    qtbot.waitUntil(lambda: _sheet_of(parent) is not None, timeout=5000)
    parent.close() if how == "close" else parent.hide()
    qtbot.waitUntil(lambda: not t.is_alive(), timeout=2000)
    assert out == [False]


def test_sheet_not_shown_for_a_hidden_view_answers_no(qtbot):
    from PySide6.QtWidgets import QWidget
    parent = QWidget()
    qtbot.addWidget(parent)  # never shown
    b = ConfirmBridge(parent, timeout=30)
    out = []
    t = threading.Thread(target=lambda: out.append(b.confirm("Add 1 item?")))
    t.start()
    qtbot.waitUntil(lambda: not t.is_alive(), timeout=5000)
    assert out == [False]


def test_sheet_tab_stays_inside(qtbot):
    parent = _shown_parent(qtbot)
    b = ConfirmBridge(parent, timeout=30)
    b._sheet("Q?", lambda a: None)
    sheet = _sheet_of(parent)
    assert parent.focusWidget() is sheet.no_btn
    qtbot.keyClick(sheet.no_btn, Qt.Key.Key_Tab)
    assert parent.focusWidget() is sheet.yes_btn
    qtbot.keyClick(sheet.yes_btn, Qt.Key.Key_Tab)
    assert parent.focusWidget() is sheet.no_btn
    sheet.answer(False)


# -- rotation (R-ROT) ---------------------------------------------------------
class _Ev:
    """Minimal stand-in for a QGraphicsSceneMouseEvent."""
    def __init__(self, pos, scene_pos=None, shift=False):
        from PySide6.QtCore import Qt
        self._p, self._s, self._shift, self._Qt = pos, scene_pos or pos, shift, Qt

    def pos(self): return self._p
    def scenePos(self): return self._s
    def button(self): return self._Qt.MouseButton.LeftButton
    def accept(self): pass

    def modifiers(self):
        return (self._Qt.KeyboardModifier.ShiftModifier if self._shift
                else self._Qt.KeyboardModifier.NoModifier)


def _rotate_drag(item, scene_x, scene_y, shift=False):
    """Press the rotate handle (item selected), drag to a scene point, release."""
    hc = item._rot_point(item.w / 2, -24, item.rotation_deg)
    item.mousePressEvent(_Ev(hc))
    item.mouseMoveEvent(_Ev(QPointF(0, 0), QPointF(scene_x, scene_y), shift))
    item.mouseReleaseEvent(_Ev(QPointF(0, 0)))


def test_rotate_polylines_direction_and_identity():
    from rmdesk.compose.place import rotate_polylines
    out = rotate_polylines([[(110.0, 50.0), (100.0, 40.0)]], 100.0, 50.0, 90)
    # y-down frame: +90 is clockwise on screen, so (cx+10, cy) -> (cx, cy+10)
    assert out[0][0] == pytest.approx((100.0, 60.0))
    assert out[0][1] == pytest.approx((110.0, 50.0))  # (cx, cy-10) -> (cx+10, cy)
    assert rotate_polylines([[(1.0, 2.0)]], 0, 0, 0) == [[(1.0, 2.0)]]
    assert rotate_polylines([[(110.0, 50.0)]], 100, 50, 180)[0][0] == pytest.approx((90.0, 50.0))


def test_snap_angle():
    from rmdesk.compose.place import snap_angle
    assert snap_angle(7.0) == 0 and snap_angle(8.0) == 15 and snap_angle(-10.0) == 345
    assert snap_angle(359.0) == 0 and snap_angle(100.0) == 105
    assert snap_angle(-7.0, free=True) == pytest.approx(353.0)
    assert 0 <= snap_angle(-1e-20, free=True) < 360


def test_rotate_handle_drag_snaps_and_shift_is_free(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_ink_text("Hi")
    item.setPos(300, 300)
    item.setSelected(True)
    c = item.mapToScene(item.centre())
    _rotate_drag(item, c.x() + 100, c.y() + 5)  # about 87.1 degrees -> 90
    assert item.rotation_deg == 90
    _rotate_drag(item, c.x() + 100, c.y() - 100)  # 45 degrees
    assert item.rotation_deg == 45
    _rotate_drag(item, c.x() + 100, c.y() - 90, shift=True)  # free: atan2(100, 90) ~ 48.0
    assert item.rotation_deg == pytest.approx(math.degrees(math.atan2(100, 90)))
    _rotate_drag(item, c.x() - 100, c.y(), shift=False)  # left of centre -> 270
    assert item.rotation_deg == 270
    assert item.pos() == QPointF(300, 300)  # rotating never moves the box


def test_rotated_item_still_resizes_in_its_local_frame(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = ImageSceneItem(Path("x.png"), 200, 100, PenStyle())
    panel.scene.addItem(item)
    item.setPos(500, 500)
    item.setSelected(True)
    item.set_rotation(90)
    tl = item.mapToScene(item._rot_point(0, 0, 90))
    corner = item._rot_point(item.w - 5, item.h - 5, 90)  # on the (rotated) corner handle
    item.mousePressEvent(_Ev(corner))
    assert item._resizing
    item.mouseMoveEvent(_Ev(item._rot_point(300, 150, 90)))
    item.mouseReleaseEvent(_Ev(QPointF(0, 0)))
    assert (item.w, item.h) == (300, 150)
    assert item.mapToScene(item._rot_point(0, 0, 90)) == QPointF(tl.x(), tl.y()) or \
        (item.mapToScene(item._rot_point(0, 0, 90)) - tl).manhattanLength() < 1e-6
    assert item.box().w == 300 and item.rotation_deg == 90


def test_rotated_send_equals_preview(qtbot, tmp_path, fw, monkeypatch):
    calls = record_send(monkeypatch, tmp_path)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_ink_text("Hi", size=80)
    item.setPos(400, 600)
    item.set_rotation(30)
    assert item.send_data()[0].rotation == 30
    preview = [(x + 400 - 702, y + 600) for pl in item.rotated_polylines() for x, y in pl]
    assert preview != [(x + 400 - 702, y + 600) for pl in item.local_polylines() for x, y in pl]
    panel.send()
    qtbot.waitUntil(lambda: bool(calls) and not panel.busy, timeout=5000)
    sent = [(pt.x, pt.y) for ln in _new_lines(calls[0]["new_page"]) for pt in ln.points]
    assert len(sent) == len(preview)
    for a, b in zip(sent, preview):
        assert a == pytest.approx(b, abs=0.01)


def test_rotated_item_near_edge_is_clipped_to_page(qtbot, tmp_path, fw, monkeypatch):
    calls = record_send(monkeypatch, tmp_path)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_ink_text("Hello there", size=120)
    item.setPos(1404 - item.w / 2, 100)  # hangs off the right edge
    item.set_rotation(45)
    xs = [x + item.pos().x() for pl in item.rotated_polylines() for x, _ in pl]
    assert max(xs) > 1404  # really crosses the edge before clipping
    panel.send()
    qtbot.waitUntil(lambda: bool(calls) and not panel.busy, timeout=5000)
    pts = [pt for ln in _new_lines(calls[0]["new_page"]) for pt in ln.points]
    assert pts
    for pt in pts:
        assert -702 <= pt.x <= 702 and 0 <= pt.y <= 1872


def test_rotation_persists_through_failed_send(qtbot, tmp_path, fw, monkeypatch):
    def fail(*a, **k):
        raise safety.SendFailed(DeviceError("write broke"), restored=True,
                                backup_dir=tmp_path / "b", restore_error=None,
                                restart_error=None)
    monkeypatch.setattr(safety, "send_to_open_page", fail)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_ink_text("Hi")
    item.setPos(250, 250)
    item.set_rotation(135)
    panel.send()
    qtbot.waitUntil(lambda: bool(panel.message.text()) and not panel.busy, timeout=5000)
    assert panel.placed_items() == [item]
    assert item.rotation_deg == 135 and item.pos() == QPointF(250, 250)


def test_bracket_keys_rotate_selected_item(qtbot, tmp_path, fw):
    from PySide6.QtCore import Qt
    view, panel, *_ = make_screen(qtbot, tmp_path)
    a = panel.add_ink_text("Hi")
    b = panel.add_ink_text("Yo")
    b.setSelected(False)  # adding an item selects it
    a.setSelected(True)
    panel.view.setFocus()
    qtbot.keyClick(panel.view, Qt.Key.Key_BracketRight)
    qtbot.keyClick(panel.view, Qt.Key.Key_BracketRight)
    assert a.rotation_deg == 30 and b.rotation_deg == 0
    for _ in range(3):
        qtbot.keyClick(panel.view, Qt.Key.Key_BracketLeft)
    assert a.rotation_deg == 345  # normalised into [0, 360)
    a.setSelected(False)
    qtbot.keyClick(panel.view, Qt.Key.Key_BracketRight)
    assert a.rotation_deg == 345 and b.rotation_deg == 0



# -- shaded pictures ---------------------------------------------------------------------

def _shaded(panel, item):
    """Wait until the item's preview is shaded for its current geometry and settings."""
    return lambda: item.current() and item.pending_key is None


def _sent_local(call, item_pos):
    """Sent points back in item-local frame pixels: (x, y, width)."""
    x0, y0 = item_pos
    return [[(pt.x + 702 - x0, pt.y - y0, pt.width) for pt in ln.points]
            for ln in _new_lines(call["new_page"])]


def _approx_strokes(a, b):
    assert len(a) == len(b) > 0
    for sa, sb in zip(a, b):
        assert len(sa) == len(sb)
        for pa, pb in zip(sa, sb):
            assert pa == pytest.approx(pb, abs=0.01)


def test_image_is_shaded_and_sent(qtbot, tmp_path, fw, monkeypatch):
    calls = record_send(monkeypatch, tmp_path)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_image(_draw(tmp_path / "pic.png"))
    assert abs(item.w / item.h - 2.0) < 0.01  # the picture's aspect from its header, at once
    assert not panel.send_btn.isEnabled() and item.placeholder() == "Shading…"
    assert item.pen.tool == shade.DEFAULT_SHADE_PEN and panel.tool_combo.currentText() == "pencil"
    assert panel.shade_controls.isVisible()
    assert (panel.spacing_spin.minimum(), panel.spacing_spin.maximum(),
            panel.spacing_spin.value()) == (3, 12, 5)
    assert (panel.contrast_spin.minimum(), panel.contrast_spin.maximum(),
            panel.contrast_spin.value()) == (0.5, 2.0, 1.0)
    assert not panel.cross_check.isChecked()
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    assert item.ready() and item.preview_strokes() and panel.send_btn.isEnabled()
    panel.send()
    qtbot.waitUntil(lambda: bool(calls) and not panel.busy, timeout=10000)
    lines = _new_lines(calls[0]["new_page"])
    assert calls[0]["n"] == len(lines) > 0
    widths = {pt.width for ln in lines for pt in ln.points}
    assert len(widths) > 1  # per-point widths survive into the page
    assert all(ln.tool == build._TOOL["pencil"] for ln in lines)


def test_preview_is_shade_image_for_the_current_box(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    src = _draw(tmp_path / "pic.png")
    item = panel.add_image(src)
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    item.setPos(310, 420)
    item.set_rotation(30)
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    b = item.box()
    res = shade.shade_image(src, round(b.w), round(b.h), 5, 1.0, False, box_x=b.x, box_y=b.y,
                            rotation=30)
    assert item.shaded.result.strokes == res.strokes
    want = [[(p.x + 702 - b.x, p.y - b.y, p.width) for p in ln.points]
            for ln in build.shade_to_lines(item.model(), res, PenStyle("pencil"))]
    _approx_strokes(item.preview_strokes(), want)
    # the cached paths hold every preview segment (built once per result, not per paint)
    n_seg = sum(len(st) - 1 for st in item.preview_strokes())
    assert sum(path.elementCount() for _w, path in item.shaded.paths) == 2 * n_seg
    panel.scene.clearSelection()
    QApplication.processEvents()
    assert red_pixels(panel.view.grab().toImage()) > 0


def test_svg_is_rasterised_before_shading(qtbot, tmp_path, fw, monkeypatch):
    seen = []
    real = shade_job.shade_item
    monkeypatch.setattr(shade_job, "shade_item",
                        lambda m, *a: seen.append(Path(m.path)) or real(m, *a))
    svg = tmp_path / "pic.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
                   '<line x1="2" y1="2" x2="38" y2="18" stroke="black" stroke-width="2"/></svg>')
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_image(svg)
    assert abs(item.w / item.h - 2.0) < 0.01
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    assert seen[0].suffix == ".png" and QImage(str(seen[0])).width() == 1024
    assert item.raster == seen[0] and item.source == svg
    assert item.send_data().model.path == seen[0]


def test_settings_change_reshades_debounced(qtbot, tmp_path, fw, monkeypatch):
    seen = []
    real = shade_job.shade_item
    monkeypatch.setattr(shade_job, "shade_item",
                        lambda m, *a: seen.append(tuple(a)) or real(m, *a))
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_image(_draw(tmp_path / "pic.png"))
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    for v in (6, 7, 8):
        panel.spacing_spin.setValue(v)
    panel.contrast_spin.setValue(1.5)
    panel.cross_check.setChecked(True)
    assert item.settings == ShadeSettings(8, 1.5, True) and not item.current()
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    assert seen == [(5, 1.0, False, False), (8, 1.5, True, False)]  # one re-shade, after the last change
    assert item.shaded_key[-1] == ShadeSettings(8, 1.5, True)


def test_move_resize_rotate_reshade(qtbot, tmp_path, fw, monkeypatch):
    seen = []
    real = shade_job.shade_item
    monkeypatch.setattr(shade_job, "shade_item",
                        lambda m, *a: seen.append((m.box, m.rotation)) or real(m, *a))
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_image(_draw(tmp_path / "pic.png"))
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    for step in (lambda: item.setPos(100, 120), lambda: item.set_size(300, 150),
                 lambda: item.set_rotation(45)):
        n = len(seen)
        step()
        assert not item.current()
        qtbot.waitUntil(_shaded(panel, item), timeout=10000)
        assert len(seen) == n + 1 and seen[-1] == (item.box(), item.rotation_deg)
    # many drag steps re-shade once, after the last
    n = len(seen)
    for x in range(200, 260, 10):
        item.setPos(x, 300)
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    assert len(seen) == n + 1 and seen[-1][0].x == 250


def test_stale_shade_result_is_dropped(qtbot, tmp_path, fw, monkeypatch):
    gate = threading.Event()
    real = shade_job.shade_item

    def slow_first(m, spacing, *a):
        if spacing == 5:
            gate.wait(10)  # the first shading finishes last
        return real(m, spacing, *a)
    monkeypatch.setattr(shade_job, "shade_item", slow_first)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_image(_draw(tmp_path / "pic.png"))
    try:
        panel.spacing_spin.setValue(9)
        qtbot.waitUntil(lambda: item.shaded is not None, timeout=10000)
        assert item.shaded_key[-1].spacing == 9
        result = item.shaded
    finally:
        gate.set()
    QThreadPool.globalInstance().waitForDone(10000)
    QApplication.processEvents()
    assert item.shaded is result and item.shaded_key[-1].spacing == 9 and item.current()


def test_sent_strokes_equal_current_preview(qtbot, tmp_path, fw, monkeypatch):
    calls = record_send(monkeypatch, tmp_path)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_image(_draw(tmp_path / "pic.png"))
    item.setPos(300, 500)
    item.set_rotation(60)
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    preview = item.preview_strokes()
    panel.send()
    qtbot.waitUntil(lambda: bool(calls) and not panel.busy, timeout=10000)
    _approx_strokes(_sent_local(calls[0], (300, 500)), preview)


def test_send_uses_snapshot_strokes(qtbot, tmp_path, fw, monkeypatch):
    """Review focus 5: a re-shade (or any change) while the send runs doesn't change what is
    sent: the worker shades the GUI-thread snapshot taken when Send was pressed."""
    calls = record_send(monkeypatch, tmp_path)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_image(_draw(tmp_path / "pic.png"))
    item.setPos(200, 400)
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    snapshot = item.preview_strokes()

    reshaded = []

    def show(text, reply):  # GUI thread, xochitl stopped, before the build
        item.set_rotation(90)
        panel.spacing_spin.setValue(10)
        item.set_size(item.w / 2, item.h / 2)
        qtbot.waitUntil(_shaded(panel, item), timeout=10000)  # re-shaded while sending
        reshaded.append(item.preview_strokes())
        reply(True)
    panel.bridge.show = show
    panel.send()
    qtbot.waitUntil(lambda: bool(calls) and not panel.busy, timeout=10000)
    assert reshaded and reshaded[0] != snapshot
    _approx_strokes(_sent_local(calls[0], (200, 400)), snapshot)


def test_send_reshades_a_stale_preview_at_the_snapshot_geometry(qtbot, tmp_path, fw,
                                                                 monkeypatch):
    calls = record_send(monkeypatch, tmp_path)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_image(_draw(tmp_path / "pic.png"))
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    item.setPos(150, 250)  # moved: the preview is out of date until the debounced re-shade
    assert not item.current()
    spec = item.send_data()
    panel.send()
    qtbot.waitUntil(lambda: bool(calls) and not panel.busy, timeout=10000)
    want = shade_job.page_lines(spec, "portrait")
    got = _new_lines(calls[0]["new_page"])
    _approx_strokes([[(p.x, p.y, p.width) for p in ln.points] for ln in got],
                    [[(p.x, p.y, p.width) for p in ln.points] for ln in want])


@pytest.mark.parametrize("kind", ["white", "transparent"])
def test_blank_picture_not_sendable(qtbot, tmp_path, fw, monkeypatch, kind):
    calls = record_send(monkeypatch, tmp_path)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    img = QImage(300, 200, QImage.Format.Format_ARGB32)
    img.fill(QColor("white") if kind == "white" else QColor(0, 0, 0, 0))
    path = tmp_path / f"{kind}.png"
    img.save(str(path))
    item = panel.add_image(path)
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    assert panel.message.text() == ("Nothing to draw — the picture is blank. "
                                    "Remove it to send the rest.")
    assert item.shaded.blank and not item.ready() and item.placeholder() == BLANK
    assert not panel.send_btn.isEnabled()
    assert panel.send_btn.toolTip() == "Remove the blank picture to send."
    panel.send()
    assert calls == [] and not panel.busy
    panel.add_ink_text("Hi")  # the blank picture still blocks the rest
    assert not panel.send_btn.isEnabled()
    panel.scene.clearSelection()
    item.setSelected(True)
    panel.remove_selected()
    assert panel.send_btn.isEnabled() and panel.send_btn.toolTip() == ""


def test_picture_off_the_page_is_not_blank(qtbot, tmp_path, fw, monkeypatch):
    calls = record_send(monkeypatch, tmp_path)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_image(_draw(tmp_path / "pic.png"))
    item.setPos(3000, 3000)
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    assert not item.shaded.blank and item.ready() and not item.preview_strokes()
    panel.send()
    assert panel.message.text() == msg.NOTHING_TO_SEND and calls == []


@pytest.mark.parametrize("capped, used, want", [
    (False, 7, "This picture is very detailed; rmdesk used wider line spacing (7 px)."),
    (True, 12, "This picture is very detailed; only part of it fits."),
])
def test_capped_status_texts(qtbot, tmp_path, fw, monkeypatch, capped, used, want):
    calls = record_send(monkeypatch, tmp_path)
    real = shade_job.shade_item

    def fake(m, *a):
        r = real(m, *a)
        return shade.ShadeResult(r.strokes, r.size, capped, used)
    monkeypatch.setattr(shade_job, "shade_item", fake)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_image(_draw(tmp_path / "pic.png"))
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    assert panel.message.text() == want
    asked = []
    panel.ask = lambda text: asked.append(text) or False
    panel.send()
    if capped:  # only part of the picture would be drawn: asked first
        assert asked and msg.SHADE_PARTIAL in asked[0] and calls == []
    else:
        assert asked == []
        qtbot.waitUntil(lambda: bool(calls) and not panel.busy, timeout=10000)


def test_shade_controls_follow_the_selection(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    a = panel.add_image(_draw(tmp_path / "a.png"))
    qtbot.waitUntil(_shaded(panel, a), timeout=10000)
    panel.spacing_spin.setValue(8)
    ink = panel.add_ink_text("Hi")
    assert not panel.shade_controls.isVisible()
    b = panel.add_image(_draw(tmp_path / "b.png"))
    assert panel.spacing_spin.value() == 5 and b.settings.spacing == 5
    panel.scene.clearSelection()
    a.setSelected(True)
    assert panel.shade_controls.isVisible() and panel.spacing_spin.value() == 8
    assert ink.isVisible()


def test_no_tracing_left_in_the_package():
    import re as _re
    import rmdesk
    root = Path(rmdesk.__file__).parent
    hits = []
    for f in root.rglob("*.py"):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if _re.search(r"trac(e|ing)\b|traced|detail_slider|\"Detail\"", line, _re.I):
                hits.append(f"{f.relative_to(root)}:{n}: {line.strip()}")
    assert hits == []
    assert not (root / "compose" / "trace.py").exists()
    assert not (root / "ui" / "compose" / "trace_job.py").exists()


def test_unreadable_picture_is_removed_with_a_message(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not a picture")
    item = panel.add_image(bad)
    assert (item.w, item.h) == (600, 600)  # no header to read the aspect from
    qtbot.waitUntil(lambda: panel.message.text() == msg.PICTURE_FAILED, timeout=10000)
    assert panel.placed_items() == [] and "cannot read image" in panel.details.text()


def test_build_page_does_no_picture_work(qtbot, tmp_path, fw, monkeypatch):
    """S5: the pictures are shaded before xochitl stops; while it is stopped (confirm and
    build) nothing is shaded and no picture file is read."""
    from rmdesk.compose import shade as shade_mod
    from rmdesk.notebook.opened import OpenedPage
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_image(_draw(tmp_path / "pic.png"))
    item.setPos(250, 350)
    item.set_rotation(30)
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    preview = item.preview_strokes()
    log, stopped = [], threading.Event()
    real_shade, real_read = shade_job.shade_item, shade_mod.cv2.imread
    monkeypatch.setattr(shade_job, "shade_item",
                        lambda *a: log.append(("shade", stopped.is_set())) or real_shade(*a))
    monkeypatch.setattr(shade_mod.cv2, "imread",
                        lambda *a: log.append(("read", stopped.is_set())) or real_read(*a))
    opened = OpenedPage(NB, "Physics notes", PG2, 1, 2, "portrait", ORIG)
    calls = []

    def fake(device, build_page, confirm, **kw):
        stopped.set()  # xochitl would be stopped from here on
        assert confirm(opened)
        new_page, n = build_page(opened)
        calls.append(dict(new_page=new_page, n=n))
        return safety.SendResult(backup_dir=tmp_path / "b" / "1"), opened
    monkeypatch.setattr(safety, "send_to_open_page", fake)
    panel.send()
    qtbot.waitUntil(lambda: bool(calls) and not panel.busy, timeout=10000)
    assert ("shade", False) in log and ("read", False) in log  # done before the stop
    assert not any(during for _what, during in log)
    _approx_strokes(_sent_local(calls[0], (250, 350)), preview)


def test_missing_picture_fails_before_the_tablet_is_touched(qtbot, tmp_path, fw, monkeypatch):
    calls = record_send(monkeypatch, tmp_path)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    src = _draw(tmp_path / "pic.png")
    item = panel.add_image(src)
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    src.unlink()
    panel.send()
    qtbot.waitUntil(lambda: bool(panel.message.text()) and not panel.busy, timeout=10000)
    assert panel.message.text() == f"{msg.PICTURE_FAILED} {msg.NOTHING_CHANGED}"
    assert calls == [] and panel.placed_items() == [item]


def test_preview_widths_follow_the_pen_thickness_like_the_send(qtbot, tmp_path, fw,
                                                                monkeypatch):
    calls = record_send(monkeypatch, tmp_path)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_image(_draw(tmp_path / "pic.png"))
    item.setPos(200, 300)
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    thin = item.preview_strokes()
    panel.thick_spin.setValue(2.5)
    assert item.pen.thickness == 2.5 and not item.current()
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    thick = item.preview_strokes()
    assert max(w for st in thick for *_xy, w in st) > max(w for st in thin for *_xy, w in st)
    panel.send()
    qtbot.waitUntil(lambda: bool(calls) and not panel.busy, timeout=10000)
    _approx_strokes(_sent_local(calls[0], (200, 300)), thick)  # widths too, at 2.5


def test_preview_paths_clamp_widths_like_the_send():
    huge = [[(0.0, 0.0, shade.WIDTH_MAX + 500), (5.0, 0.0, shade.WIDTH_MAX + 500)],
            [(0.0, 5.0, 0), (5.0, 5.0, 0)]]
    assert [u for u, _p in shade_job.preview_paths(huge)] == [shade.WIDTH_MIN, shade.WIDTH_MAX]


# -- redesign (Task 4) ---------------------------------------------------------------------

def test_pointer_segmented_drives_place(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path, compose=False)
    assert view.pointer_seg.index() == 0 and view.pointer == "control"
    assert view.canvas.isVisible()
    assert view.full_btn.property("variant") == "primary" and view.full_btn.text() == "Expand"
    assert not panel.send_btn.isVisible()  # no items, not Place: nothing to send
    view.place_btn.click()
    assert view.compose_mode and view.pointer_seg.index() == 1 and panel.toolbar.isVisible()
    assert not view.full_btn.isVisible()  # one primary per screen: Send in Place
    assert panel.send_btn.property("variant") == "primary" and panel.send_btn.isVisible()
    view.control_btn.click()
    assert not view.compose_mode and view.canvas.isVisible()
    assert view.full_btn.isVisible() and view.full_btn.property("variant") == "primary"
    view.set_pointer("place")  # programmatic switch keeps the control in step
    assert view.place_btn.isChecked()


def test_send_count_badge(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    assert panel.badge.isHidden()
    panel.add_ink_text("One")
    assert panel.badge.text() == "1" and not panel.badge.isHidden()
    panel.add_ink_text("Two")
    assert panel.badge.text() == "2"
    b = panel.badge.geometry()
    assert panel.send_btn.rect().contains(b)  # inside the button, not clipped
    panel.remove_selected()
    assert panel.badge.text() == "1"
    for it in panel.placed_items():
        panel.scene.removeItem(it)
    panel._update_actions()
    assert panel.badge.isHidden()


def test_ctrl_brackets_do_not_rotate(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    a = panel.add_ink_text("Hi")
    a.setSelected(True)
    panel.view.setFocus()
    for key in (Qt.Key.Key_BracketLeft, Qt.Key.Key_BracketRight):
        for mod in (Qt.KeyboardModifier.ControlModifier, Qt.KeyboardModifier.AltModifier,
                    Qt.KeyboardModifier.MetaModifier):
            qtbot.keyClick(panel.view, key, mod)
    assert a.rotation_deg == 0
    qtbot.keyClick(panel.view, Qt.Key.Key_BracketRight)  # the plain key still rotates
    assert a.rotation_deg == 15


def test_rotate_handle_hit_area_and_stays_in_scene(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    panel.view.fit()
    item = panel.add_ink_text("Hi")
    item.setPos(600, 600)
    item.setSelected(True)
    scale = panel.view.transform().m11()
    assert item.hit_radius() * scale >= 12 - 1e-6  # at least 12 screen px
    hc = item.rotate_handle_centre()
    off = QPointF(item.hit_radius() * 0.9, 0)  # near the edge of the hit area
    assert item._on_rotate_handle(QPointF(hc.x(), hc.y()) + off)
    assert not item._on_rotate_handle(QPointF(hc.x(), hc.y()) + QPointF(item.hit_radius() * 1.5, 0))
    # an item at the very top: the handle moves below it instead of leaving the scene
    item.setPos(600, 0)
    hc = item.rotate_handle_centre()
    c = item.mapToScene(item._rot_point(hc.x(), hc.y(), item.rotation_deg))
    assert panel.scene.sceneRect().contains(c) and hc.y() > item.h
    item.setPos(600, 1872 - item.h)  # at the bottom: back above
    hc = item.rotate_handle_centre()
    assert hc.y() < 0
    # a huge item that fills the page: clamped into the scene
    item.set_size(1404, 1872)
    item.setPos(0, 0)
    c = item.mapToScene(item._rot_point(*(lambda p: (p.x(), p.y()))(item.rotate_handle_centre()),
                                        item.rotation_deg))
    assert panel.scene.sceneRect().contains(c)


def test_narrow_window_no_clipping(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    view.resize(700 - 208, 600)
    view.show()
    panel.add_ink_text("Hi")
    for it in panel.placed_items():
        it.setSelected(True)
    QApplication.processEvents()
    assert view.minimumSizeHint().width() <= view.width()
    header = [view.pointer_seg, view.refresh_btn, view.keep_btn, view.rotate_btn,
              view.shot_btn, view.full_btn, panel.undo_btn, panel.send_btn]
    visible = [w for w in (*header, panel.add_image_btn, panel.add_ink_btn,
                           *panel.shape_btns.values(), panel.remove_btn, panel.tool_combo,
                           panel.color_combo, panel.thick_spin)
               if w.isVisible()]  # an ink text box has its pen controls on the format bar
    assert len(visible) == 14 and panel.format_bar.isVisible()  # Expand hides in Place
    for w in visible:
        r = w.mapTo(view, w.rect().topLeft())
        assert r.x() >= 0 and r.x() + w.width() <= view.width(), w
        assert w.width() >= w.minimumSizeHint().width(), w
        assert w.height() >= w.minimumSizeHint().height(), w
    # the header wrapped onto more than one row rather than squeezing
    ys = {w.mapTo(view, w.rect().topLeft()).y() for w in header}
    assert len(ys) > 1
    fb = panel.format_bar.geometry()
    assert fb.left() >= 0 and fb.right() < panel.view.viewport().width()
    assert panel.view.height() >= 200


def test_tab_order(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    panel.add_ink_text("Hi")
    chain, w = [], view.control_btn
    while w not in chain:
        chain.append(w)
        w = w.nextInFocusChain()
    want = [view.control_btn, view.place_btn, view.refresh_btn, view.keep_btn,
            view.rotate_btn, view.shot_btn, view.full_btn, panel.undo_btn, panel.send_btn]
    pos = [chain.index(x) for x in want]
    assert pos == sorted(pos)
    bar = [panel.add_image_btn, panel.add_ink_btn, panel.add_rect_btn, panel.add_line_btn,
           panel.add_arrow_btn, panel.add_note_btn, panel.remove_btn, panel.tool_combo,
           panel.color_combo, panel.thick_spin]
    bpos = [chain.index(x) for x in bar]
    assert bpos == sorted(bpos) and bpos[0] > pos[-1]  # the Place row after the header
    for x in want + [b for b in bar if b.isEnabled()]:
        assert x.focusPolicy() & Qt.FocusPolicy.TabFocus, x
        assert x in chain
    assert chain.index(panel.view) > bpos[-1]  # then the picture (Place: it takes keys)
    assert view.keep_btn.accessibleName() == "Keep refreshing"


def test_confirm_sheet_lists_what_is_written(qtbot, tmp_path, fw, monkeypatch):
    """The real sheet (not the test stub) shows the confirm text, the items and the pause note."""
    from rmdesk.ui.widgets.sheet import ConfirmSheet
    calls = record_send(monkeypatch, tmp_path)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    panel.bridge.show = panel.bridge._sheet  # undo the stub of make_screen
    panel.add_ink_text("Hello there")
    panel.send()
    qtbot.waitUntil(lambda: bool(view.findChildren(ConfirmSheet)) and
                    view.window().findChildren(ConfirmSheet)[0].isVisible(), timeout=10000)
    sheet = view.window().findChildren(ConfirmSheet)[0]
    assert sheet.body.text().startswith("Add 1 item to")
    assert "Ink text: \u201cHello there\u201d" in sheet.items_label.text()
    assert "paused" in sheet.note.text()
    assert panel.busy  # modal in effect: the send waits for the answer
    sheet.no_btn.click()
    qtbot.waitUntil(idle(panel), timeout=10000)
    assert panel.status.text() == "Nothing was sent."


def test_confirm_bridge_never_shows_an_expired_question(qtbot):
    from rmdesk.ui.compose.confirm import _Request
    b = ConfirmBridge()
    shown = []
    b.show = lambda text, reply: shown.append(text)
    req = _Request("is the tablet paused?")
    req.expire()  # the worker already gave up (timeout / window closing)
    b._on_ask(req)
    assert shown == [] and b.items is None
    live = _Request("q?", ["x"])
    b._on_ask(live)
    assert shown == ["q?"] and b.items == ["x"]


def test_send_failure_with_stopped_screen_says_restart(qtbot, tmp_path, fw, monkeypatch):
    def fail(*a, **k):
        e = safety.BackupError("cannot read notebook: SSH session not active")
        e.xochitl_start_error = DeviceError("SSH session not active")
        raise e
    monkeypatch.setattr(safety, "send_to_open_page", fail)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    panel.add_ink_text("Hi")
    panel.send()
    qtbot.waitUntil(lambda: bool(panel.message.text()) and not panel.busy, timeout=5000)
    assert msg.SCREEN_RESTART in panel.message.text()


@pytest.mark.parametrize("exc", [safety.Cancelled("no"), None])
def test_refusal_with_stopped_screen_says_restart(qtbot, tmp_path, fw, monkeypatch, exc):
    from rmdesk.notebook.opened import NotEditable
    e = exc if exc is not None else NotEditable("zoomed in")

    def fail(*a, **k):
        e.xochitl_start_error = DeviceError("down")
        raise e
    monkeypatch.setattr(safety, "send_to_open_page", fail)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    panel.add_ink_text("Hi")
    panel.send()
    qtbot.waitUntil(lambda: bool(panel.message.text()) and not panel.busy, timeout=5000)
    assert msg.SCREEN_RESTART in panel.message.text()


def test_send_records_page_and_sha_for_undo(qtbot, tmp_path, fw):
    import hashlib
    view, panel, dev, cfg, saved = make_screen(qtbot, tmp_path)
    panel.add_ink_text("Hello")
    panel.send()
    qtbot.waitUntil(lambda: cfg.last_send is not None and not panel.busy, timeout=10000)
    assert cfg.last_send["page_id"] == PG2
    assert cfg.last_send["page_sha256"] == hashlib.sha256(dev.files[PAGE]).hexdigest()


def test_undo_refused_when_page_changed_on_tablet(qtbot, tmp_path, fw):
    view, panel, dev, cfg, saved = make_screen(qtbot, tmp_path)
    panel.add_ink_text("Hello")
    panel.send()
    qtbot.waitUntil(lambda: cfg.last_send is not None and not panel.busy, timeout=10000)
    dev.files[PAGE] = b"written on the tablet since"
    panel.undo()
    qtbot.waitUntil(lambda: bool(panel.message.text()) and not panel.busy
                    and not panel.message.text().startswith("Sent to"), timeout=10000)
    assert panel.message.text().startswith(
        "The page changed on the tablet after the last send, so rmdesk can't undo it safely.")
    assert dev.files[PAGE] == b"written on the tablet since" and dev.xochitl_active


# -- ink text edited on the page, with the format bar ---------------------------------------

def _start_ink(qtbot, tmp_path, **kw):
    view, panel, *rest = make_screen(qtbot, tmp_path, **kw)
    panel.view.fit()
    panel.add_ink_btn.click()  # no pop-up: a box on the picture, being edited
    item = next(i for i in panel.placed_items())
    return view, panel, item, rest


def _rendered(item):
    return lambda: item.ready() and not item._dirty


def test_add_ink_text_puts_an_edited_box_in_the_visible_view(qtbot, tmp_path, fw):
    from rmdesk.ui.compose.scene import RENDER_MS, InkSceneItem
    view, panel, item, _ = _start_ink(qtbot, tmp_path)
    assert isinstance(item, InkSceneItem) and item.editing and item.isSelected()
    assert item.text == "" and not item.ready()
    centre = panel.view.mapToScene(panel.view.viewport().rect().center())
    c = item.mapToScene(item.centre())
    assert abs(c.x() - centre.x()) < 2 and abs(c.y() - centre.y()) < 2
    assert RENDER_MS == 150
    qtbot.keyClicks(panel.view, "Hi")
    qtbot.keyClick(panel.view, Qt.Key.Key_Return)  # Enter makes a new line
    qtbot.keyClicks(panel.view, "yo")
    assert item.text == "Hi\nyo" and item.editing
    assert item._dirty  # the strokes follow after the debounce
    qtbot.waitUntil(_rendered(item), timeout=3000)
    res = __import__("rmdesk.compose.inktext", fromlist=["x"]).text_to_polylines("Hi\nyo", item.size)
    assert item.local_polylines() == res.lines
    qtbot.keyClick(panel.view, Qt.Key.Key_Escape)  # Esc ends editing, the box stays
    assert not item.editing and panel.placed_items() == [item]


def test_typing_updates_the_preview(qtbot, tmp_path, fw):
    view, panel, item, _ = _start_ink(qtbot, tmp_path)
    client = None
    qtbot.waitUntil(lambda: any(c.alive for c in FakeClient.instances), timeout=5000)
    client = next(c for c in FakeClient.instances if c.alive)
    client.on_frame(frame(255))
    qtbot.waitUntil(lambda: view.frame is not None, timeout=5000)
    qtbot.keyClicks(panel.view, "Hello")
    QApplication.processEvents()
    typed = panel.view.grab().toImage()  # the strokes are not there yet (debounced)
    qtbot.waitUntil(_rendered(item), timeout=3000)
    QApplication.processEvents()
    shown = panel.view.grab().toImage()
    assert shown != typed  # the red strokes appeared
    n = len(item.local_polylines())
    qtbot.keyClicks(panel.view, " there")
    qtbot.waitUntil(_rendered(item), timeout=3000)
    assert len(item.local_polylines()) > n
    QApplication.processEvents()
    assert panel.view.grab().toImage() != shown
    qtbot.keyClick(panel.view, Qt.Key.Key_Escape)
    QApplication.processEvents()
    assert red_pixels(panel.view.grab().toImage()) > 0


def test_emptied_box_is_removed_when_editing_ends(qtbot, tmp_path, fw):
    view, panel, item, _ = _start_ink(qtbot, tmp_path)
    qtbot.keyClicks(panel.view, "a")
    qtbot.keyClick(panel.view, Qt.Key.Key_Backspace)
    qtbot.keyClick(panel.view, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: panel.placed_items() == [], timeout=3000)


def test_skipped_characters_reported_while_typing(qtbot, tmp_path, fw):
    view, panel, item, _ = _start_ink(qtbot, tmp_path)
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent
    qtbot.keyClicks(panel.view, "Hi ")
    QApplication.sendEvent(panel.view, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Udiaeresis,
                                                 Qt.KeyboardModifier.NoModifier, "ü"))
    qtbot.waitUntil(_rendered(item), timeout=3000)
    assert item.skipped == ["ü"]
    assert panel.message.text() == msg.INK_SKIPPED.format(chars=msg.char_list(["ü"]))


def test_keys_while_editing_do_not_rotate_or_delete(qtbot, tmp_path, fw):
    view, panel, item, _ = _start_ink(qtbot, tmp_path)
    panel.view.setFocus()
    qtbot.keyClicks(panel.view, "a[b]")
    assert item.rotation_deg == 0 and item.text == "a[b]"
    qtbot.keyClick(panel.view, Qt.Key.Key_Left)
    qtbot.keyClick(panel.view, Qt.Key.Key_Backspace)  # removes the "b"
    qtbot.keyClick(panel.view, Qt.Key.Key_Delete)  # removes the "]"
    assert item.text == "a[" and panel.placed_items() == [item]
    for k in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace, Qt.Key.Key_Backspace,
              Qt.Key.Key_Backspace):
        qtbot.keyClick(panel.view, k)
    assert panel.placed_items() == [item] and item.text == "" and item.editing
    qtbot.keyClicks(panel.view, "ok")
    qtbot.keyClick(panel.view, Qt.Key.Key_Escape)
    qtbot.keyClick(panel.view, Qt.Key.Key_BracketRight)  # not editing: rotates again
    assert item.rotation_deg == 15 and item.text == "ok"


def test_double_click_edits_an_existing_ink_text(qtbot, tmp_path, fw):
    from PySide6.QtTest import QTest
    view, panel, *_ = make_screen(qtbot, tmp_path)
    panel.view.fit()
    item = panel.add_ink_text("Hi", size=120)
    assert not item.editing
    p = panel.view.mapFromScene(item.mapToScene(item.centre()))
    QTest.mouseDClick(panel.view.viewport(), Qt.MouseButton.LeftButton, pos=p)
    assert item.editing and item.isSelected()
    qtbot.keyClick(panel.view, Qt.Key.Key_End)
    qtbot.keyClicks(panel.view, "!")
    assert item.text == "Hi!"
    panel.scene.clearSelection()  # clicking outside ends editing
    assert not item.editing


def test_clicking_outside_ends_editing(qtbot, tmp_path, fw):
    from PySide6.QtTest import QTest
    view, panel, item, _ = _start_ink(qtbot, tmp_path)
    qtbot.keyClicks(panel.view, "Hi")
    corner = panel.view.mapFromScene(QPointF(5, 5))
    QTest.mouseClick(panel.view.viewport(), Qt.MouseButton.LeftButton, pos=corner)
    assert not item.editing and item.text == "Hi"
    qtbot.waitUntil(_rendered(item), timeout=3000)
    assert panel.placed_items() == [item]


def test_preview_equals_sent_polylines_with_styles(qtbot, tmp_path, fw, monkeypatch):
    from rmdesk.compose.inktext import text_to_polylines
    from rmdesk.ui.compose.scene import InkStyle
    calls = record_send(monkeypatch, tmp_path)
    view, panel, item, _ = _start_ink(qtbot, tmp_path)
    qtbot.keyClicks(panel.view, "Ab")
    qtbot.keyClick(panel.view, Qt.Key.Key_Return)
    qtbot.keyClicks(panel.view, "wide line")
    style = InkStyle("serif", True, True, "right")
    panel.format_bar.font_combo.setCurrentIndex(1)
    panel.format_bar.font_combo.activated.emit(1)
    panel.format_bar.bold_btn.click()
    panel.format_bar.italic_btn.click()
    panel.format_bar.align_btns["right"].click()
    assert item.style == style
    panel.format_bar.size_spin.setValue(60)
    assert item.size == 60
    item.setPos(300, 500)
    item.set_rotation(30)
    qtbot.keyClicks(panel.view, "!")  # still pending when the send starts
    assert item._dirty
    panel.send()
    qtbot.waitUntil(lambda: bool(calls) and not panel.busy, timeout=5000)
    assert not item._dirty
    p = item.pos()  # the render after "!" grew the box leftwards (right aligned)
    assert p.y() != 500 or p.x() != 300
    preview = [(x + p.x() - 702, y + p.y()) for pl in item.rotated_polylines() for x, y in pl]
    sent = [(pt.x, pt.y) for ln in _new_lines(calls[0]["new_page"]) for pt in ln.points]
    assert len(sent) == len(preview) and preview
    for a, b in zip(sent, preview):
        assert a == pytest.approx(b, abs=0.01)
    direct = text_to_polylines("Ab\nwide line!", 60, "serif", True, True, "right")
    assert item.local_polylines() == direct.lines
    spec = item.send_data()[0]
    assert (spec.font, spec.bold, spec.italic, spec.align) == ("serif", True, True, "right")


def test_resized_box_still_sends_its_preview(qtbot, tmp_path, fw, monkeypatch):
    calls = record_send(monkeypatch, tmp_path)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_ink_text("Hi there", style=__import__(
        "rmdesk.ui.compose.scene", fromlist=["x"]).InkStyle("gothic", True, True, "center"))
    item.setPos(100, 100)
    item.resize_to(item.w * 2, item.h * 2)  # the corner handle
    assert item.size == pytest.approx(80)
    assert panel.format_bar.size_spin.value() == 80  # the bar follows the corner drag
    preview = [(x + 100 - 702, y + 100) for pl in item.rotated_polylines() for x, y in pl]
    panel.send()
    qtbot.waitUntil(lambda: bool(calls) and not panel.busy, timeout=5000)
    sent = [(pt.x, pt.y) for ln in _new_lines(calls[0]["new_page"]) for pt in ln.points]
    assert len(sent) == len(preview)
    for a, b in zip(sent, preview):
        assert a == pytest.approx(b, abs=0.01)


def test_format_bar_shows_only_for_ink_text(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    panel.view.fit()
    bar = panel.format_bar
    assert bar.isHidden()
    pic = ImageSceneItem(Path("x.png"), 200, 100, PenStyle())
    panel.scene.addItem(pic)
    pic.setPos(100, 100)
    panel.scene.clearSelection()
    pic.setSelected(True)
    assert bar.isHidden() and panel.shade_controls.isVisible()
    for w in (panel.tool_combo, panel.color_combo, panel.thick_spin):
        assert w.isVisible()  # pictures keep the toolbar pen controls
    ink = panel.add_ink_text("Hello", size=80)
    ink.setPos(500, 900)
    assert ink.isSelected() and not pic.isSelected()
    assert bar.isVisible() and bar.item is ink and not panel.shade_controls.isVisible()
    for w in (panel.tool_combo, panel.color_combo, panel.thick_spin):
        assert not w.isVisible()  # replaced by the bar
    vp = panel.view.viewport().rect()
    assert vp.contains(bar.geometry())
    box_top = panel.view.mapFromScene(ink.mapToScene(QPointF(0, 0))).y()
    assert bar.geometry().bottom() < box_top  # above the box
    for w in (bar.size_spin, bar.font_combo, bar.bold_btn, bar.italic_btn,
              *bar.align_btns.values(), bar.line_spacing_spin,
              bar.letter_spacing_spin, bar.tool_combo, bar.color_combo, bar.thick_spin):
        assert w.isVisible()
    assert [bar.font_combo.itemText(i) for i in range(bar.font_combo.count())][:4] == \
        ["Sans", "Serif", "Script", "Gothic"]
    # the bar's pen applies to the box (and to the next ink text)
    bar.tool_combo.setCurrentText("marker")
    bar.tool_combo.activated.emit(bar.tool_combo.currentIndex())
    assert ink.pen.tool == "marker" and panel._pen.tool == "marker"
    panel.scene.clearSelection()
    assert bar.isHidden() and panel.tool_combo.isVisible()
    a = panel.add_ink_text("One")
    b = panel.add_ink_text("Two")
    a.setSelected(True)  # two selected: no bar
    assert bar.isHidden()


def test_format_bar_kept_inside_the_view(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    panel.view.fit()
    ink = panel.add_ink_text("Top", size=80)
    ink.setPos(0, 0)  # no room above: the bar goes below the box
    QApplication.processEvents()
    bar = panel.format_bar
    vp = panel.view.viewport().rect()
    assert vp.contains(bar.geometry())
    bottom = panel.view.mapFromScene(ink.mapToScene(QPointF(0, ink.h))).y()
    assert bar.geometry().top() > bottom
    ink.setPos(1404 - ink.w, 1872 - ink.h)
    QApplication.processEvents()
    assert vp.contains(bar.geometry())


@pytest.mark.parametrize("width", [1280, 700])
def test_narrow_window_format_bar_wraps(qtbot, tmp_path, fw, width):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    view.resize(width - 208, 800)
    view.show()
    QApplication.processEvents()
    panel.add_ink_btn.click()
    qtbot.keyClicks(panel.view, "Hello")
    QApplication.processEvents()
    bar = panel.format_bar
    vp = panel.view.viewport().rect()
    assert bar.isVisible() and vp.contains(bar.geometry())
    controls = [bar.size_spin, bar.font_combo, bar.bold_btn, bar.italic_btn,
                *bar.align_btns.values(), bar.line_spacing_spin, bar.letter_spacing_spin,
                bar.tool_combo, bar.color_combo, bar.thick_spin]
    for w in controls:
        r = QRectF(w.mapTo(bar, w.rect().topLeft()), w.size().toSizeF())
        assert QRectF(bar.rect()).contains(r), w
        assert w.width() >= w.minimumSizeHint().width(), w
    rows = {w.mapTo(bar, w.rect().topLeft()).y() // 20 for w in controls}
    if width == 700:
        assert len(rows) > 1  # wrapped rather than squeezed
    for w in (panel.add_image_btn, panel.add_ink_btn, panel.remove_btn, panel.undo_btn,
              panel.send_btn):
        r = w.mapTo(view, w.rect().topLeft())
        assert r.x() >= 0 and r.x() + w.width() <= view.width(), w


def test_busy_send_stops_editing(qtbot, tmp_path, fw):
    view, panel, item, _ = _start_ink(qtbot, tmp_path)
    qtbot.keyClicks(panel.view, "Hi")
    panel._begin("sending")
    assert not item.editing and not item.editable and not panel.format_bar.isEnabled()
    item.start_editing()
    assert not item.editing
    panel._end()
    assert item.editable


def test_ink_model_defaults_unchanged():
    from rmdesk.compose.items import Box, InkTextItem
    it = InkTextItem("x", Box(0, 0, 1, 1))
    assert (it.font, it.bold, it.italic, it.align) == ("sans", False, False, "left")


@pytest.mark.parametrize("turn", [90, 180, 270])
def test_new_items_start_upright_in_a_rotated_view(qtbot, tmp_path, fw, turn):
    """With Screen rotated (e.g. to match a landscape page), new ink text and pictures start
    upright as the user sees them: their rotation undoes the view's."""
    from rmdesk.compose.place import normalise_deg
    cfg = Config(screen_rotation=turn)
    view, panel, *_ = make_screen(qtbot, tmp_path, cfg=cfg)
    assert view.rotation == turn
    ink = panel.add_ink_text("Hi")
    assert ink.rotation_deg == normalise_deg(-turn)
    # on screen, the item's own x axis points right: compare the view's mapping of its edge
    a = panel.view.mapFromScene(ink.mapToScene(ink._rot_point(0, 0, ink.rotation_deg)))
    b = panel.view.mapFromScene(ink.mapToScene(ink._rot_point(ink.w, 0, ink.rotation_deg)))
    assert b.x() - a.x() > abs(b.y() - a.y())


def test_new_items_unrotated_in_an_unrotated_view(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    assert panel.add_ink_text("Hi").rotation_deg == 0


def test_remove_disables_itself(qtbot, tmp_path, fw):
    from rmdesk.ui import theme
    view, panel, *_ = make_screen(qtbot, tmp_path)
    panel.add_ink_text("Hi")
    panel.remove_btn.click()
    assert not panel.remove_btn.isEnabled()
    # the disabled danger button is greyed, not red
    assert ('QPushButton[variant="danger"]:disabled' in theme.qss()
            and theme.qss().index('QPushButton[variant="danger"]:disabled')
            > theme.qss().index('QPushButton[variant="danger"] {'))


# -- ink text: selection, clipboard, undo, spacing, more fonts ---------------------------------

CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier


def _same_strokes(a, b):
    assert len(a) == len(b)
    for pa, pb in zip(a, b):
        assert len(pa) == len(pb)
        flat_a = [v for pt in pa for v in pt]
        flat_b = [v for pt in pb for v in pt]
        assert flat_a == pytest.approx(flat_b, abs=1e-6)


def _typed(qtbot, tmp_path, text):
    view, panel, item, rest = _start_ink(qtbot, tmp_path)
    panel.view.setFocus()
    for i, line in enumerate(text.split("\n")):
        if i:
            qtbot.keyClick(panel.view, Qt.Key.Key_Return)
        qtbot.keyClicks(panel.view, line)
    return view, panel, item


def test_shift_arrows_home_end_and_ctrl_a_select(qtbot, tmp_path, fw):
    view, panel, item = _typed(qtbot, tmp_path, "hello world")
    assert item.selection() == (11, 11) and item.selected_text() == ""
    qtbot.keyClick(panel.view, Qt.Key.Key_Left, SHIFT)
    qtbot.keyClick(panel.view, Qt.Key.Key_Left, SHIFT)
    assert item.selected_text() == "ld" and item.cursor == 9
    qtbot.keyClick(panel.view, Qt.Key.Key_Home, SHIFT)
    assert item.selected_text() == "hello world"
    qtbot.keyClick(panel.view, Qt.Key.Key_Right)  # collapses to the selection's end
    assert item.selected_text() == "" and item.cursor == 11
    qtbot.keyClick(panel.view, Qt.Key.Key_Home)
    qtbot.keyClick(panel.view, Qt.Key.Key_End, SHIFT)
    assert item.selected_text() == "hello world"
    qtbot.keyClick(panel.view, Qt.Key.Key_Left)
    assert item.cursor == 0 and item.selected_text() == ""
    qtbot.keyClick(panel.view, Qt.Key.Key_A, CTRL)
    assert item.selected_text() == "hello world" and item.text == "hello world"
    qtbot.keyClicks(panel.view, "X")  # typing replaces the selection
    assert item.text == "X" and item.cursor == 1


def test_shift_up_down_select_across_lines(qtbot, tmp_path, fw):
    view, panel, item = _typed(qtbot, tmp_path, "abc\ndef")
    qtbot.keyClick(panel.view, Qt.Key.Key_Up, SHIFT)
    assert item.selected_text() == "\ndef"
    qtbot.keyClick(panel.view, Qt.Key.Key_Backspace)
    assert item.text == "abc" and item.cursor == 3


def test_copy_cut_paste(qtbot, tmp_path, fw):
    from PySide6.QtGui import QGuiApplication
    cb = QGuiApplication.clipboard()
    cb.setText("untouched")
    view, panel, item = _typed(qtbot, tmp_path, "one two")
    qtbot.keyClick(panel.view, Qt.Key.Key_C, CTRL)  # nothing selected: nothing copied
    assert cb.text() == "untouched"
    for _ in range(3):
        qtbot.keyClick(panel.view, Qt.Key.Key_Left, SHIFT)
    qtbot.keyClick(panel.view, Qt.Key.Key_C, CTRL)
    assert cb.text() == "two" and item.text == "one two"
    qtbot.keyClick(panel.view, Qt.Key.Key_X, CTRL)
    assert cb.text() == "two" and item.text == "one " and item.selected_text() == ""
    qtbot.keyClick(panel.view, Qt.Key.Key_X, CTRL)  # nothing selected: clipboard kept
    assert cb.text() == "two" and item.text == "one "
    qtbot.keyClick(panel.view, Qt.Key.Key_Home)
    qtbot.keyClick(panel.view, Qt.Key.Key_V, CTRL)
    assert item.text == "twoone " and item.cursor == 3
    qtbot.keyClick(panel.view, Qt.Key.Key_End, SHIFT)
    qtbot.keyClick(panel.view, Qt.Key.Key_V, CTRL)  # paste replaces the selection
    assert item.text == "twotwo" and item.cursor == 6
    qtbot.keyClick(panel.view, Qt.Key.Key_Left, SHIFT)
    qtbot.keyClick(panel.view, Qt.Key.Key_Delete)  # Delete removes the selection
    assert item.text == "twotw"


def test_paste_normalises_crlf_and_takes_plain_text(qtbot, tmp_path, fw):
    from PySide6.QtCore import QMimeData
    from PySide6.QtGui import QGuiApplication
    cb = QGuiApplication.clipboard()
    view, panel, item = _typed(qtbot, tmp_path, "")
    cb.setText("a\r\nb\rc\nd")
    qtbot.keyClick(panel.view, Qt.Key.Key_V, CTRL)
    assert item.text == "a\nb\nc\nd" and item.cursor == len(item.text)
    md = QMimeData()
    md.setHtml("<b>bold</b>")  # no plain text: nothing pasted
    cb.setMimeData(md)
    qtbot.keyClick(panel.view, Qt.Key.Key_V, CTRL)
    assert item.text == "a\nb\nc\nd"


def test_paste_reports_skipped_characters(qtbot, tmp_path, fw):
    from PySide6.QtGui import QGuiApplication
    QGuiApplication.clipboard().setText("Hi ü")
    view, panel, item = _typed(qtbot, tmp_path, "")
    qtbot.keyClick(panel.view, Qt.Key.Key_V, CTRL)
    qtbot.waitUntil(_rendered(item), timeout=3000)
    assert item.skipped == ["ü"]
    assert panel.message.text() == msg.INK_SKIPPED.format(chars=msg.char_list(["ü"]))


def test_undo_redo_inside_the_box(qtbot, tmp_path, fw):
    view, panel, item = _typed(qtbot, tmp_path, "hello")
    qtbot.keyClicks(panel.view, " world")
    qtbot.keyClick(panel.view, Qt.Key.Key_Left, SHIFT)
    qtbot.keyClick(panel.view, Qt.Key.Key_Backspace)
    assert item.text == "hello worl"
    qtbot.keyClick(panel.view, Qt.Key.Key_Z, CTRL)
    assert item.text == "hello world" and item.selected_text() == "d"  # selection restored
    qtbot.keyClick(panel.view, Qt.Key.Key_Z, CTRL)
    assert item.text == "hello"  # a typed word is one step
    qtbot.keyClick(panel.view, Qt.Key.Key_Z, CTRL)
    assert item.text == ""
    qtbot.keyClick(panel.view, Qt.Key.Key_Z, CTRL)  # nothing more to undo
    assert item.text == ""
    qtbot.keyClick(panel.view, Qt.Key.Key_Z, CTRL | SHIFT)
    assert item.text == "hello" and item.cursor == 5
    qtbot.keyClick(panel.view, Qt.Key.Key_Y, CTRL)
    assert item.text == "hello world"
    qtbot.keyClick(panel.view, Qt.Key.Key_Z, CTRL)
    qtbot.keyClicks(panel.view, "!")  # a new edit drops the redo steps
    qtbot.keyClick(panel.view, Qt.Key.Key_Y, CTRL)
    assert item.text == "hello!"
    qtbot.waitUntil(_rendered(item), timeout=3000)
    from rmdesk.compose.inktext import text_to_polylines
    assert item.local_polylines() == text_to_polylines("hello!", item.size).lines


def test_undo_stack_is_bounded(qtbot, tmp_path, fw):
    from rmdesk.ui.compose.scene import UNDO_STEPS
    assert UNDO_STEPS == 100
    view, panel, item = _typed(qtbot, tmp_path, "")
    for _ in range(UNDO_STEPS + 20):
        qtbot.keyClicks(panel.view, "a ")
    assert len(item._undo) <= UNDO_STEPS


def test_ctrl_z_while_editing_is_not_undo_last_send(qtbot, tmp_path, fw, monkeypatch):
    from PySide6.QtGui import QKeySequence, QShortcut
    view, panel, item = _typed(qtbot, tmp_path, "ab")
    called = []
    monkeypatch.setattr(panel, "undo", lambda: called.append("undo"))
    window = view.window()
    window.activateWindow()  # window shortcuts only fire in the active window
    qtbot.waitUntil(lambda: QApplication.activeWindow() is window, timeout=3000)
    panel.view.setFocus()
    fired = []
    shortcuts = []
    for seq in ("Ctrl+Z", "Ctrl+Shift+Z", "Ctrl+Y", "Ctrl+C", "Ctrl+X", "Ctrl+V", "Ctrl+A"):
        sc = QShortcut(QKeySequence(seq), window)  # e.g. a future app-wide binding
        sc.setContext(Qt.ShortcutContext.WindowShortcut)
        sc.activated.connect(lambda s=seq: fired.append(s))
        shortcuts.append(sc)
    qtbot.keyClick(panel.view, Qt.Key.Key_A, CTRL)
    qtbot.keyClick(panel.view, Qt.Key.Key_C, CTRL)
    qtbot.keyClick(panel.view, Qt.Key.Key_X, CTRL)
    qtbot.keyClick(panel.view, Qt.Key.Key_V, CTRL)
    qtbot.keyClick(panel.view, Qt.Key.Key_Z, CTRL)
    qtbot.keyClick(panel.view, Qt.Key.Key_Z, CTRL | SHIFT)
    qtbot.keyClick(panel.view, Qt.Key.Key_Y, CTRL)
    assert fired == [] and called == []
    assert item.editing and item.text == "ab"
    qtbot.keyClick(panel.view, Qt.Key.Key_Escape)
    qtbot.keyClick(panel.view, Qt.Key.Key_Z, CTRL)  # not editing: the window's shortcut
    assert fired == ["Ctrl+Z"] and called == []


def test_app_shortcuts_while_editing(qtbot, tmp_path, fw):
    """The app's own bindings (Ctrl+1.., Ctrl+B, F11) are not editing keys: they still work."""
    from rmdesk.ui.compose.scene import InkSceneItem
    view, panel, item = _typed(qtbot, tmp_path, "ab")
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtCore import QEvent
    for key, mods in ((Qt.Key.Key_1, CTRL), (Qt.Key.Key_F11,
                                              Qt.KeyboardModifier.NoModifier)):
        assert not item.wants_key(QKeyEvent(QEvent.Type.KeyPress, key, mods))
    for key, mods in ((Qt.Key.Key_Z, CTRL), (Qt.Key.Key_A, CTRL), (Qt.Key.Key_Left, SHIFT),
                      (Qt.Key.Key_Home, Qt.KeyboardModifier.NoModifier)):
        assert item.wants_key(QKeyEvent(QEvent.Type.KeyPress, key, mods))
    assert isinstance(item, InkSceneItem)


def _box_point(panel, item, x, y):
    return panel.view.mapFromScene(item.mapToScene(item._rot_point(x, y, item.rotation_deg)))


def test_mouse_drag_selects_without_moving_the_box(qtbot, tmp_path, fw):
    from PySide6.QtTest import QTest
    view, panel, *_ = make_screen(qtbot, tmp_path)
    panel.view.fit()
    item = panel.add_ink_text("hello world", size=120)
    item.start_editing()
    pos = item.pos()
    stops = item.stops[0]
    k = item.size / item.base_size
    vp = panel.view.viewport()
    a = _box_point(panel, item, stops[0] * k + 2, item.size * 0.5)
    b = _box_point(panel, item, stops[5] * k, item.size * 0.5)
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, pos=a)
    QTest.mouseMove(vp, b)
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, pos=b)
    assert item.pos() == pos
    assert item.selected_text() == "hello" and item.editing


def test_double_click_selects_a_word_while_editing(qtbot, tmp_path, fw):
    from PySide6.QtTest import QTest
    view, panel, *_ = make_screen(qtbot, tmp_path)
    panel.view.fit()
    item = panel.add_ink_text("hello world", size=120)
    k = item.size / item.base_size
    x = (item.stops[0][7] + item.stops[0][8]) / 2 * k
    p = _box_point(panel, item, x, item.size * 0.5)
    QTest.mouseDClick(panel.view.viewport(), Qt.MouseButton.LeftButton, pos=p)
    assert item.editing and item.selected_text() == ""  # not editing yet: starts editing
    QTest.mouseDClick(panel.view.viewport(), Qt.MouseButton.LeftButton, pos=p)
    assert item.selected_text() == "world"


def _highlight_pixels(img: QImage) -> int:
    """Pixels of the translucent red selection over the white page (pinkish)."""
    img = img.convertToFormat(QImage.Format.Format_RGB32)
    n = 0
    for y in range(0, img.height(), 2):
        for x in range(0, img.width(), 2):
            c = img.pixelColor(x, y)
            if c.red() > 200 and 120 < c.green() < 235 and 120 < c.blue() < 235 \
                    and c.red() - c.green() > 15:
                n += 1
    return n


def test_selection_is_drawn(qtbot, tmp_path, fw):
    view, panel, item = _typed(qtbot, tmp_path, "hello world")
    qtbot.waitUntil(lambda: any(c.alive for c in FakeClient.instances), timeout=5000)
    client = next(c for c in FakeClient.instances if c.alive)
    client.on_frame(frame(255))
    qtbot.waitUntil(lambda: view.frame is not None, timeout=5000)
    qtbot.waitUntil(_rendered(item), timeout=3000)
    QApplication.processEvents()
    before = _highlight_pixels(panel.view.grab().toImage())
    for _ in range(5):
        qtbot.keyClick(panel.view, Qt.Key.Key_Left, SHIFT)
    QApplication.processEvents()
    img = panel.view.grab().toImage()
    after = _highlight_pixels(img)
    assert after > before + 20
    # the highlight sits over "world" (the right part of the box), not over "hello"
    r = item.selection_rects()
    assert len(r) == 1
    k = item.size / item.base_size
    assert r[0].left() == pytest.approx(item.stops[0][6] * k)
    assert r[0].right() == pytest.approx(item.stops[0][11] * k)


def test_selection_rects_follow_line_and_letter_spacing(qtbot, tmp_path, fw):
    from rmdesk.compose import inktext
    from rmdesk.ui.compose.scene import InkStyle
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_ink_text("ab\ncd", size=40,
                              style=InkStyle(line_spacing=2.0, letter_spacing=0.5))
    item.start_editing(0)
    item.set_selection(1, 4)
    rects = item.selection_rects()
    pitch = inktext.line_pitch(40, 2.0)
    assert len(rects) == 2
    assert rects[1].top() == pytest.approx(pitch - 0.05 * 40)
    assert rects[1].right() == pytest.approx(item.stops[1][1])
    a, b = item.caret_line()
    assert a.y() == pytest.approx(pitch - 0.05 * 40)  # caret on row 2 at the doubled pitch
    assert item.cursor_at(QPointF(item.stops[1][2] - 1, pitch + 10)) == 5
    assert item.cursor_at(QPointF(item.stops[0][1] + 1, 10)) == 1


def test_format_bar_spacing_controls(qtbot, tmp_path, fw):
    from rmdesk.compose import inktext
    from rmdesk.compose.inktext import text_to_polylines
    view, panel, item = _typed(qtbot, tmp_path, "Ab\ncd")
    bar = panel.format_bar
    ls, lt = bar.line_spacing_spin, bar.letter_spacing_spin
    assert ls.accessibleName() == "Line spacing" and lt.accessibleName() == "Letter spacing"
    assert (ls.minimum(), ls.maximum(), ls.singleStep(), ls.value()) == (0.8, 3.0, 0.1, 1.0)
    assert (lt.minimum(), lt.maximum(), lt.singleStep(), lt.value()) == (-0.1, 0.5, 0.05, 0.0)
    ls.setValue(1.5)
    lt.setValue(0.25)
    assert item.style.line_spacing == 1.5 and item.style.letter_spacing == 0.25
    qtbot.waitUntil(_rendered(item), timeout=3000)
    assert item.local_polylines() == text_to_polylines(
        "Ab\ncd", item.size, line_spacing=1.5, letter_spacing=0.25).lines
    assert item.h > inktext.line_pitch(item.size, 1.4)
    # new boxes start with the last style
    qtbot.keyClick(panel.view, Qt.Key.Key_Escape)
    nxt = panel.add_ink_text("x")
    assert (nxt.style.line_spacing, nxt.style.letter_spacing) == (1.5, 0.25)
    assert ls.value() == 1.5 and lt.value() == 0.25  # the bar shows the selected box
    # the corner drag keeps the spacing proportional
    nxt.set_style(item.style)
    w0 = nxt.w
    nxt.resize_to(nxt.w * 2, nxt.h * 2)
    direct = text_to_polylines("x", nxt.size, line_spacing=1.5, letter_spacing=0.25)
    _same_strokes(nxt.local_polylines(), direct.lines)
    assert nxt.w == pytest.approx(2 * w0)


def test_preview_equals_sent_with_spacing_and_new_font(qtbot, tmp_path, fw, monkeypatch):
    from rmdesk.compose.inktext import text_to_polylines
    calls = record_send(monkeypatch, tmp_path)
    view, panel, item = _typed(qtbot, tmp_path, "Ab\nwide line")
    bar = panel.format_bar
    i = bar.font_combo.findData("cursive")
    bar.font_combo.setCurrentIndex(i)
    bar.font_combo.activated.emit(i)
    bar.bold_btn.click()
    bar.align_btns["center"].click()
    bar.line_spacing_spin.setValue(2.2)
    bar.letter_spacing_spin.setValue(-0.05)
    panel.format_bar.size_spin.setValue(60)
    item.setPos(300, 500)
    item.resize_to(item.w * 1.5, item.h * 1.5)
    item.set_rotation(15)
    panel.send()
    qtbot.waitUntil(lambda: bool(calls) and not panel.busy, timeout=5000)
    p = item.pos()
    preview = [(x + p.x() - 702, y + p.y()) for pl in item.rotated_polylines() for x, y in pl]
    sent = [(pt.x, pt.y) for ln in _new_lines(calls[0]["new_page"]) for pt in ln.points]
    assert len(sent) == len(preview) and preview
    for a, b in zip(sent, preview):
        assert a == pytest.approx(b, abs=0.01)
    spec = item.send_data()[0]
    assert (spec.font, spec.bold, spec.align) == ("cursive", True, "center")
    assert (spec.line_spacing, spec.letter_spacing) == (2.2, -0.05)
    direct = text_to_polylines("Ab\nwide line", item.size, "cursive", True, False, "center",
                               line_spacing=2.2, letter_spacing=-0.05)
    _same_strokes(item.local_polylines(), direct.lines)


def test_font_list_has_the_new_fonts_with_tooltips(qtbot, tmp_path, fw):
    view, panel, item = _typed(qtbot, tmp_path, "abc")
    combo = panel.format_bar.font_combo
    assert [combo.itemText(i) for i in range(combo.count())] == \
        ["Sans", "Serif", "Script", "Gothic", "Cursive", "Italian Gothic", "Greek (symbols)",
         "Cyrillic (symbols)"]
    for f in ("greek", "cyrillic"):
        tip = combo.itemData(combo.findData(f), Qt.ItemDataRole.ToolTipRole)
        assert "Latin" in tip
    i = combo.findData("greek")
    combo.setCurrentIndex(i)
    combo.activated.emit(i)
    assert item.style.font == "greek" and "Latin" in combo.toolTip()


def test_ink_model_spacing_defaults():
    from rmdesk.compose.items import Box, InkTextItem
    it = InkTextItem("x", Box(0, 0, 1, 1))
    assert (it.line_spacing, it.letter_spacing) == (1.0, 0.0)


FOLIO_DEVICES = ('I: Bus=0000\nN: Name="Wacom I2C Digitizer"\nH: Handlers=event1\n\n'
                 'I: Bus=0000\nN: Name="rM_Keyboard"\nH: Handlers=kbd event3\n')


def folio_handler(mode="Landscape", pid="4242"):
    """Fake tablet answers for tablet_layout(): xochitl's MainPID and its last orientation log."""
    from rmdesk.device import CommandResult

    def handler(cmd):
        if cmd.startswith("systemctl show -p MainPID"):
            return CommandResult(0, f"{pid}\n", "")
        if cmd.startswith("journalctl"):
            assert f"_PID={pid}" in cmd
            line = (f"07:09:05.924 rm.orientationmodule     Setting new orientation {mode} "
                    "(setOrientation ...)\n") if mode else ""
            return CommandResult(0 if mode else 1, line, "")
        return None
    return handler


def test_send_with_type_folio_uses_the_keyboard_layout(qtbot, tmp_path, fw, monkeypatch):
    """Folio attached: the frame maps to the page turned, 1:1 and scrolled (calibrated)."""
    from rmdesk.compose import frame as fr
    from rmdesk.notebook.opened import OpenedPage
    op = OpenedPage(NB, "Physics notes", PG2, 1, 2, "portrait", ORIG, vertical_scroll=678.0)
    calls = record_send(monkeypatch, tmp_path, opened=op)
    seen = {}
    real = build.frame_to_lines

    def spy(*a, **k):
        seen.update(k)
        return real(*a, **k)
    monkeypatch.setattr(build, "frame_to_lines", spy)
    from rmdesk.compose import scroll_fit
    fits = []

    def fake_fit(picture, points):
        fits.append((picture, len(points)))
        return {"scroll": 678.0}
    monkeypatch.setattr(scroll_fit, "fit_details", fake_fit)
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path)
    view.frame = frame(255)  # the live picture the item was placed on
    dev.run_handler = folio_handler("Landscape")
    panel.add_ink_text("Hello")
    panel.send()
    qtbot.waitUntil(lambda: not panel.busy and cfg.last_send is not None, timeout=10000)
    assert seen == {"layout": fr.KEYBOARD, "scroll": 678.0}
    # the scroll came from the live picture (a copy), matched against the page's strokes
    assert len(fits) == 1 and fits[0][0].shape == view.frame.gray.shape
    assert calls and calls[0]["n"] > 0


def test_send_after_detaching_uses_portrait(qtbot, tmp_path, fw, monkeypatch):
    from rmdesk.compose import frame as fr
    calls = record_send(monkeypatch, tmp_path)
    seen = {}
    real = build.frame_to_lines
    monkeypatch.setattr(build, "frame_to_lines", lambda *a, **k: seen.update(k) or real(*a, **k))
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path)
    dev.run_handler = folio_handler("Portrait")
    panel.add_ink_text("Hello")
    panel.send()
    qtbot.waitUntil(lambda: not panel.busy and cfg.last_send is not None, timeout=10000)
    assert seen["layout"] == fr.PORTRAIT_UI


def test_keyboard_layout_with_no_scroll_match_refuses(qtbot, tmp_path, fw, monkeypatch):
    from rmdesk.compose import scroll_fit
    record_send(monkeypatch, tmp_path)
    from rmdesk.ui import live_compose as lc
    monkeypatch.setattr(lc, "DEBUG_DIR", tmp_path / "debug")
    monkeypatch.setattr(scroll_fit, "fit_details",
                        lambda picture, points: {"scroll": None, "points": 100, "reason": "test"})
    monkeypatch.setattr(build, "frame_to_lines", lambda *a, **k: pytest.fail("built"))
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path)
    view.frame = frame(255)
    dev.run_handler = folio_handler("Landscape")
    panel.add_ink_text("Hello")
    panel.send()
    qtbot.waitUntil(lambda: bool(panel.message.text()) and not panel.busy, timeout=10000)
    assert panel.message.text() == msg.SCROLL_UNKNOWN and cfg.last_send is None
    # what rmdesk saw is kept locally for diagnosis
    kept = sorted(f.suffix for f in (tmp_path / "debug").iterdir())
    assert kept == [".json", ".png", ".rm"]


def test_keyboard_layout_with_no_live_picture_refuses(qtbot, tmp_path, fw, monkeypatch):
    record_send(monkeypatch, tmp_path)
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path)
    view.frame = None
    dev.run_handler = folio_handler("Landscape")
    panel.add_ink_text("Hello")
    panel.send()
    qtbot.waitUntil(lambda: bool(panel.message.text()) and not panel.busy, timeout=10000)
    assert panel.message.text() == msg.SCROLL_UNKNOWN and cfg.last_send is None


def test_tablet_layout_detection():
    from rmdesk.compose import frame as fr
    from rmdesk.device import CommandResult
    from rmdesk.notebook.opened import NotEditable
    from rmdesk.ui.live_compose import INPUT_DEVICES, tablet_layout

    class D:
        def __init__(self, handler, files=None):
            self.handler, self.files = handler, files or {}

        def run(self, cmd, timeout=30):
            r = self.handler(cmd)
            if r is not None:
                return r
            if cmd == f"cat {INPUT_DEVICES}":
                if INPUT_DEVICES in self.files:
                    return CommandResult(0, self.files[INPUT_DEVICES], "")
                return CommandResult(1, "", "cat: x: No such file or directory")
            raise AssertionError(cmd)
    assert tablet_layout(D(folio_handler("Landscape"))) == fr.KEYBOARD
    # the real log line, as xochitl writes it on fw 3.29.1.0 (with colour codes)
    real = ("07:09:05.924 \x1b[01;32mrm.orientationmodule     Setting new orientation "
            "Landscape\x1b[0;37m (setOrientation /home/runner/work/xochitl/xochitl/src/xofm/"
            "libs/orientation/src/orientationwrapper.cpp:82)\x1b[0m\n")

    def coloured(cmd):
        if cmd.startswith("systemctl show"):
            return CommandResult(0, "6243\n", "")
        if cmd.startswith("journalctl"):
            return CommandResult(0, real, "")
        return None
    assert tablet_layout(D(coloured)) == fr.KEYBOARD
    assert tablet_layout(D(lambda c: coloured(c) if c.startswith("systemctl") else
                           CommandResult(0, real.replace("Landscape", "Portrait"), "")
                           if c.startswith("journalctl") else None)) == fr.PORTRAIT_UI
    assert tablet_layout(D(folio_handler("Portrait"))) == fr.PORTRAIT_UI
    # no orientation line yet from this xochitl: the folio's input device decides
    assert tablet_layout(D(folio_handler(None), {INPUT_DEVICES: FOLIO_DEVICES})) == fr.KEYBOARD
    assert tablet_layout(D(folio_handler(None))) == fr.PORTRAIT_UI
    with pytest.raises(NotEditable):
        tablet_layout(D(folio_handler("Sideways")))
    # no running xochitl pid to read the log of: the device list decides
    nopid = lambda c: CommandResult(1, "", "no") if c.startswith("systemctl") else None
    assert tablet_layout(D(nopid, {INPUT_DEVICES: FOLIO_DEVICES})) == fr.KEYBOARD
    assert tablet_layout(D(nopid)) == fr.PORTRAIT_UI
    assert tablet_layout(D(folio_handler("Landscape", pid="0"))) == fr.PORTRAIT_UI
    with pytest.raises(NotEditable):  # and that list unreadable: refuse
        tablet_layout(D(lambda c: CommandResult(2, "", "denied")))


def test_keyboard_attached_detection():
    from rmdesk.device import CommandResult
    from rmdesk.ui.live_compose import keyboard_attached
    from rmdesk.notebook.opened import NotEditable

    class D:
        def __init__(self, res): self.res = res
        def run(self, cmd, timeout=30): return self.res
    assert keyboard_attached(D(CommandResult(0, FOLIO_DEVICES, ""))) is True
    assert keyboard_attached(D(CommandResult(0, 'N: Name="Wacom I2C Digitizer"\n', ""))) is False
    assert keyboard_attached(D(CommandResult(1, "", "cat: x: No such file or directory"))) is False
    with pytest.raises(NotEditable):
        keyboard_attached(D(CommandResult(2, "", "permission denied")))



def test_invert_toggle_reshades_and_is_sent(qtbot, tmp_path, fw, monkeypatch):
    seen = []
    real = shade_job.shade_item
    monkeypatch.setattr(shade_job, "shade_item",
                        lambda m, *a: seen.append(tuple(a)) or real(m, *a))
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_image(_draw(tmp_path / "pic.png"))
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    assert panel.invert_check.isVisible() and not panel.invert_check.isChecked()
    panel.invert_check.setChecked(True)
    assert item.settings.invert and not item.current()
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    assert seen[-1][-1] is True  # shaded with invert on
    assert item.send_data().settings.invert  # and the send uses it


# -- ink text editing shortcuts --
NOMOD = Qt.KeyboardModifier.NoModifier
K = Qt.Key


def test_ctrl_arrows_move_by_word(qtbot, tmp_path, fw):
    view, panel, item = _typed(qtbot, tmp_path, "one  two three")
    keys = lambda k, m=CTRL: qtbot.keyClick(panel.view, k, m)  # noqa: E731
    keys(K.Key_Left)
    assert item.cursor == 9
    keys(K.Key_Left)
    assert item.cursor == 5
    keys(K.Key_Left)
    keys(K.Key_Left)
    assert item.cursor == 0
    keys(K.Key_Right)
    assert item.cursor == 5
    keys(K.Key_Right)
    assert item.cursor == 9
    keys(K.Key_Right)
    assert item.cursor == 14
    keys(K.Key_Left, CTRL | SHIFT)
    assert item.selection() == (9, 14)
    keys(K.Key_Left, CTRL | SHIFT)
    assert item.selection() == (5, 14)
    keys(K.Key_Right, CTRL | SHIFT)
    assert item.selection() == (9, 14)


def test_shift_up_down_extend_by_line(qtbot, tmp_path, fw):
    view, panel, item = _typed(qtbot, tmp_path, "abc\ndefg\nhi")
    k = lambda key, m=NOMOD: qtbot.keyClick(panel.view, key, m)  # noqa: E731
    k(K.Key_Up)
    assert item.cursor == 6  # "hi" col 2 -> "defg" col 2
    k(K.Key_Down, SHIFT)
    assert item.selection() == (6, 11)
    k(K.Key_Up, SHIFT)
    k(K.Key_Up, SHIFT)
    assert item.selection() == (2, 6)
    k(K.Key_Up, SHIFT)  # on the first line: to the start
    assert item.selection() == (0, 6)
    k(K.Key_Down)
    k(K.Key_Down)
    k(K.Key_Down)
    assert item.cursor == 11 and not item.has_selection()


def test_home_end_and_ctrl_home_end(qtbot, tmp_path, fw):
    view, panel, item = _typed(qtbot, tmp_path, "abc\ndefg\nhi")
    k = lambda key, m=NOMOD: qtbot.keyClick(panel.view, key, m)  # noqa: E731
    k(K.Key_Up)
    k(K.Key_Home)
    assert item.cursor == 4
    k(K.Key_End)
    assert item.cursor == 8
    k(K.Key_Home, SHIFT)
    assert item.selection() == (4, 8)
    k(K.Key_Home, CTRL)
    assert item.cursor == 0 and not item.has_selection()
    k(K.Key_End, CTRL)
    assert item.cursor == 11
    k(K.Key_Home, CTRL | SHIFT)
    assert item.selection() == (0, 11)
    k(K.Key_Home, CTRL)
    k(K.Key_End, CTRL | SHIFT)
    assert item.selection() == (0, 11)
    k(K.Key_End, SHIFT)
    assert item.selection() == (0, 11)


def test_ctrl_backspace_delete_remove_words_and_undo(qtbot, tmp_path, fw):
    view, panel, item = _typed(qtbot, tmp_path, "one two three")
    k = lambda key, m=CTRL: qtbot.keyClick(panel.view, key, m)  # noqa: E731
    k(K.Key_Backspace)
    assert item.text == "one two " and item.cursor == 8
    k(K.Key_Backspace)
    assert item.text == "one " and item.cursor == 4
    k(K.Key_Z)
    assert item.text == "one two "
    k(K.Key_Home)
    k(K.Key_Delete)
    assert item.text == "two " and item.cursor == 0
    k(K.Key_Z)
    assert item.text == "one two "
    item.set_selection(1, 6)
    k(K.Key_Backspace)
    assert item.text == "oo " and item.cursor == 1
    item.set_selection(1, 3)
    k(K.Key_Delete)
    assert item.text == "o" and not item.has_selection()


def test_format_shortcuts_update_the_style_and_format_bar(qtbot, tmp_path, fw):
    view, panel, item = _typed(qtbot, tmp_path, "hello")
    bar = panel.format_bar
    k = lambda key, m=CTRL: qtbot.keyClick(panel.view, key, m)  # noqa: E731
    k(K.Key_B)
    assert item.style.bold and bar.bold_btn.isChecked()
    k(K.Key_I)
    assert item.style.italic and bar.italic_btn.isChecked()
    k(K.Key_B)
    assert not item.style.bold and not bar.bold_btn.isChecked()
    k(K.Key_E)
    assert item.style.align == "center" and bar.align_btns["center"].isChecked()
    k(K.Key_R)
    assert item.style.align == "right" and bar.align_btns["right"].isChecked()
    k(K.Key_L)
    assert item.style.align == "left" and bar.align_btns["left"].isChecked()
    assert panel._ink_style == item.style  # the next new box starts with it
    assert item.text == "hello" and item.editing
    size, step = item.size, bar.size_spin.singleStep()
    k(K.Key_Plus, CTRL | SHIFT)
    assert item.size == size + step and bar.size_spin.value() == round(size + step)
    k(K.Key_Equal)
    assert item.size == size + 2 * step
    k(K.Key_Minus)
    k(K.Key_Minus)
    assert item.size == size
    for _ in range(200):
        k(K.Key_Minus)
    assert item.size == bar.size_spin.minimum()


def test_ctrl_b_while_editing_bolds_and_does_not_collapse_the_sidebar(qtbot, tmp_path, fw):
    from PySide6.QtGui import QKeySequence, QShortcut
    view, panel, item = _typed(qtbot, tmp_path, "hi")
    window = view.window()
    window.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is window, timeout=3000)
    panel.view.setFocus()
    fired = []
    shortcuts = []
    for seq in ("Ctrl+B", "Ctrl+I", "Ctrl+L", "Ctrl+E", "Ctrl+R", "Ctrl++", "Ctrl+=", "Ctrl+-"):
        sc = QShortcut(QKeySequence(seq), window)
        sc.setContext(Qt.ShortcutContext.WindowShortcut)
        sc.activated.connect(lambda s=seq: fired.append(s))
        shortcuts.append(sc)
    qtbot.keyClick(panel.view, K.Key_B, CTRL)
    qtbot.keyClick(panel.view, K.Key_I, CTRL)
    qtbot.keyClick(panel.view, K.Key_E, CTRL)
    qtbot.keyClick(panel.view, K.Key_Equal, CTRL)
    qtbot.keyClick(panel.view, K.Key_Minus, CTRL)
    assert fired == []
    assert item.style.bold and item.style.italic and item.style.align == "center"
    qtbot.keyClick(panel.view, K.Key_Escape)
    qtbot.keyClick(panel.view, K.Key_B, CTRL)  # not editing: the window's own shortcut
    assert fired == ["Ctrl+B"] and item.style.bold


def test_format_bar_tooltips_show_the_shortcuts(qtbot, tmp_path, fw):
    view, panel, item = _typed(qtbot, tmp_path, "x")
    bar = panel.format_bar
    assert "Ctrl+B" in bar.bold_btn.toolTip() and "Ctrl+I" in bar.italic_btn.toolTip()
    for a, key in (("left", "L"), ("center", "E"), ("right", "R")):
        assert f"Ctrl+{key}" in bar.align_btns[a].toolTip()
    assert "Ctrl++" in bar.size_spin.toolTip()



def test_keyboard_layout_on_a_page_without_handwriting_says_so(qtbot, tmp_path, fw, monkeypatch):
    from rmdesk.compose import scroll_fit
    from rmdesk.ui import live_compose as lc
    record_send(monkeypatch, tmp_path)
    monkeypatch.setattr(lc, "DEBUG_DIR", tmp_path / "debug")
    monkeypatch.setattr(scroll_fit, "fit_details",
                        lambda picture, points: {"scroll": None, "points": 0})
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path)
    view.frame = frame(255)
    dev.run_handler = folio_handler("Landscape")
    panel.add_ink_text("Hello")
    panel.send()
    qtbot.waitUntil(lambda: bool(panel.message.text()) and not panel.busy, timeout=10000)
    assert panel.message.text() == msg.SCROLL_NO_STROKES and cfg.last_send is None
    assert not (tmp_path / "debug").exists()  # an empty page is not a failure to keep


# -- keyboard layout on pages with text-anchored handwriting ----------------------------

def _anchored_page_and_picture(const=820.0):
    """A synthetic page whose only strokes are in groups anchored to text character A1, and
    the live picture showing them at frame-x constant `const`."""
    from tests.test_anchor_fit import A1, PageBuilder, render, scribble
    g = scribble(2, n=14)
    data = PageBuilder().anchored(A1, g, origin_x=-464.0).data()
    return data, render([(const, g)]), A1


def _send_keyboard(qtbot, tmp_path, monkeypatch, data, gray, pos=(500, 700)):
    from rmdesk.notebook.opened import OpenedPage
    op = OpenedPage(NB, "Physics notes", PG2, 1, 2, "portrait", data)
    calls = record_send(monkeypatch, tmp_path, opened=op)
    from rmdesk.ui import live_compose as lc
    monkeypatch.setattr(lc, "DEBUG_DIR", tmp_path / "debug")
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path)
    view.frame = Frame(1404, 1872, gray)
    dev.run_handler = folio_handler("Landscape")
    item = panel.add_ink_text("Hi")
    item.setPos(*pos)
    spec = item.send_data()
    panel.send()
    qtbot.waitUntil(lambda: not panel.busy and (cfg.last_send is not None
                                                or bool(panel.message.text())), timeout=10000)
    return calls, cfg, panel, spec


def test_keyboard_send_on_an_anchored_page_writes_one_anchored_group(qtbot, tmp_path, fw,
                                                                    monkeypatch):
    data, gray, a1 = _anchored_page_and_picture(const=820.0)
    calls, cfg, panel, spec = _send_keyboard(qtbot, tmp_path, monkeypatch, data, gray)
    assert cfg.last_send is not None, panel.message.text()
    new = calls[0]["new_page"]
    pagemod.verify(data, new, calls[0]["n"], None)  # what safety checks before writing
    old_ids = set(rmscene.read_tree(io.BytesIO(data))._node_ids)
    tree = rmscene.read_tree(io.BytesIO(new))
    (g,) = [tree[i] for i in tree._node_ids if i not in old_ids and tree[i].anchor_id]
    assert g.anchor_id.value == a1 and g.anchor_origin_x.value == -464.0
    # the sent strokes, local to the group: frame (fx, fy) -> (fy - 936 + 464, C - fx)
    item, polylines, size = spec
    frame_pts = [pt for pl in build.place(item, polylines, size) for piece in build._clip(pl)
                 for pt in piece]
    got = [(p.x, p.y) for ln in g.children.values() for p in ln.points]
    want = [(fy - 936 + 464, 820.0 - fx) for fx, fy in frame_pts]
    assert len(got) == len(want)
    assert max(abs(a - c) + abs(b - d) for (a, b), (c, d) in zip(got, want)) <= 2.5
    # and they show where they were placed: back to frame space through the same anchor
    back = [(820.0 - y, x - 464 + 936) for x, y in got]
    assert max(abs(a - c) + abs(b - d) for (a, b), (c, d) in zip(back, frame_pts)) <= 2.5


def test_keyboard_send_on_an_unanchored_page_still_writes_absolute_lines(qtbot, tmp_path, fw,
                                                                        monkeypatch):
    from tests.test_anchor_fit import PageBuilder, render, scribble
    lines = scribble(5, n=25, y0=20, y1=2400)
    data = PageBuilder().absolute(lines).data()
    gray = render([(1404 + 678, lines)])
    seen = {}
    real = build.frame_to_lines
    monkeypatch.setattr(build, "frame_to_lines", lambda *a, **k: seen.update(k) or real(*a, **k))
    calls, cfg, panel, _ = _send_keyboard(qtbot, tmp_path, monkeypatch, data, gray)
    assert cfg.last_send is not None, panel.message.text()
    from rmdesk.compose import frame as fr
    assert seen["layout"] == fr.KEYBOARD and abs(seen["scroll"] - 678) <= 2
    assert "anchored" not in seen
    new = calls[0]["new_page"]
    pagemod.verify(data, new, calls[0]["n"], None)
    tree = rmscene.read_tree(io.BytesIO(new))
    layer = list(tree.root.children.values())[-1]
    assert layer.label.value == "rmdesk"
    assert all(isinstance(v, rmscene.scene_items.Line) for v in layer.children.values())


def test_keyboard_send_with_no_confident_reference_refuses(qtbot, tmp_path, fw, monkeypatch):
    data, _gray, _ = _anchored_page_and_picture()
    blank = np.full((1872, 1404), 255, np.uint8)  # the strokes are nowhere on the picture
    monkeypatch.setattr(build, "frame_to_lines", lambda *a, **k: pytest.fail("built"))
    calls, cfg, panel, _ = _send_keyboard(qtbot, tmp_path, monkeypatch, data, blank)
    assert panel.message.text() == msg.SCROLL_UNKNOWN and cfg.last_send is None
    assert all("new_page" not in c for c in calls)
    kept = json.loads(next((tmp_path / "debug").glob("*.json")).read_text())
    assert "absolute" in kept and any(k.startswith("anchor ") for k in kept)


def test_keyboard_send_picks_the_anchor_nearest_the_items(qtbot, tmp_path, fw, monkeypatch):
    """Two confident anchors: the new group copies the one nearest the items in frame x."""
    from tests.test_anchor_fit import A1, A2, PageBuilder, render, scribble
    g1, g2 = scribble(2, n=14), scribble(3, n=14, y0=-80, y1=80)
    data = PageBuilder().anchored(A1, g1).anchored(A2, g2, origin_x=-300.0).data()
    gray = render([(1150, g1), (300, g2)])
    calls, cfg, panel, _ = _send_keyboard(qtbot, tmp_path, monkeypatch, data, gray,
                                          pos=(250, 700))
    assert cfg.last_send is not None, panel.message.text()
    new = calls[0]["new_page"]
    pagemod.verify(data, new, calls[0]["n"], None)
    old_ids = set(rmscene.read_tree(io.BytesIO(data))._node_ids)
    tree = rmscene.read_tree(io.BytesIO(new))
    (g,) = [tree[i] for i in tree._node_ids if i not in old_ids and tree[i].anchor_id]
    assert g.anchor_id.value == A2 and g.anchor_origin_x.value == -300.0


# -- the floating Place toolbar ----------------------------------------------------

def _tools(panel):
    return [w for w in (panel.add_image_btn, panel.add_ink_btn, *panel.shape_btns.values(),
                        panel.remove_btn, panel.tool_combo, panel.color_combo,
                        panel.thick_spin, panel.fill_check, panel.double_check)
            if w.isVisible()]


def test_place_toolbar_floats_top_centre_over_the_picture(qtbot, tmp_path, fw):
    from rmdesk.ui import live_compose as lc
    view, panel, *_ = make_screen(qtbot, tmp_path)
    view.resize(1000, 800)
    QApplication.processEvents()
    tb, vp = panel.toolbar, panel.view.viewport()
    assert tb.parentWidget() is vp and tb.isVisible()  # over the picture, not above it
    assert lc.TOOLBAR_INSET == 12
    g = tb.geometry()
    assert g.top() == 12
    assert abs(g.center().x() - vp.rect().center().x()) <= 1
    assert g.left() >= 12 and g.right() <= vp.width() - 12
    assert tb.objectName() == "placeBar" and tb.graphicsEffect() is not None  # the shadow
    for w in _tools(panel):  # the clusters are inside it
        r = w.mapTo(tb, w.rect().topLeft())
        assert r.x() >= 0 and r.y() >= 0 and r.x() + w.width() <= tb.width(), w
        assert r.y() + w.height() <= tb.height(), w
    view.set_pointer("control")
    assert not tb.isVisible()


def test_picture_keeps_its_size_in_control_and_place(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path, compose=False)
    view.resize(900, 700)
    QApplication.processEvents()
    control = (panel.view.geometry(), panel.view.viewport().size())
    view.set_pointer("place")
    QApplication.processEvents()
    assert panel.toolbar.isVisible()
    assert (panel.view.geometry(), panel.view.viewport().size()) == control
    view.set_pointer("control")
    QApplication.processEvents()
    assert (panel.view.geometry(), panel.view.viewport().size()) == control


def test_place_toolbar_wraps_and_stays_inside_a_narrow_picture(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    view.resize(1200, 800)
    panel.add_shape("rectangle")  # the shape extras join the row
    QApplication.processEvents()
    wide = panel.toolbar.height()
    view.resize(700 - 208, 700)
    QApplication.processEvents()
    tb, vp = panel.toolbar, panel.view.viewport()
    assert tb.height() > wide  # more rows
    assert tb.geometry().left() >= 0 and tb.geometry().right() < vp.width()
    for w in _tools(panel):
        r = w.mapTo(vp, w.rect().topLeft())
        assert r.x() >= 0 and r.x() + w.width() <= vp.width(), w


def test_items_under_the_toolbar_stay_reachable(qtbot, tmp_path, fw):
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest
    view, panel, *_ = make_screen(qtbot, tmp_path)
    view.resize(1000, 800)
    QApplication.processEvents()
    tb, vp = panel.toolbar, panel.view.viewport()
    # a point on the panel itself (its padding), not on any of its widgets
    local = QPoint(3, tb.height() // 2)
    assert tb.childAt(local) is None
    at = tb.mapTo(vp, local)
    item = panel.add_shape("rectangle")
    panel.scene.clearSelection()
    scene_pt = panel.view.mapToScene(at)
    item.setPos(scene_pt.x() - 20, scene_pt.y() - 20)
    QApplication.processEvents()
    assert not item.isSelected()
    QTest.mouseClick(tb, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, local)
    QApplication.processEvents()
    assert item.isSelected()  # the click went through the panel to the item


def test_ink_format_bar_stays_below_the_toolbar(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    view.resize(1000, 800)
    QApplication.processEvents()
    item = panel.add_ink_text("Hi")
    item.setPos(item.pos().x(), 0)  # at the very top of the page, under the toolbar
    panel.scene.clearSelection()
    item.setSelected(True)
    QApplication.processEvents()
    fb, tb = panel.format_bar, panel.toolbar
    assert fb.isVisible()
    assert fb.geometry().top() > tb.geometry().bottom()
    assert not fb.geometry().intersects(tb.geometry())


# -- keyboard layout, typed-only pages: the scroll measured by swiping to the top ---------

class FakeProbe:
    """Stands in for ScrollProbe: answers `scroll` (or fails with `fail`) on the next tick."""
    made: list = []
    scroll, fail, events = 321, None, None

    def __init__(self, f0, touch, frames, busy, parent=None):
        from PySide6.QtCore import QObject, Signal

        class Sig(QObject):
            progress = Signal(str)
            finished = Signal(int)
            failed = Signal(str, str)
        self._sig = Sig(parent)
        self.progress, self.finished, self.failed = (self._sig.progress, self._sig.finished,
                                                    self._sig.failed)
        self.f0, self.touch, self.frames, self.busy = f0, touch, frames, busy
        FakeProbe.made.append(self)

    def start(self):
        from PySide6.QtCore import QTimer
        self.job_at_start = self.busy()
        if FakeProbe.events is not None:
            FakeProbe.events.append("probe")
        self.progress.emit(msg.MEASURING)
        if FakeProbe.fail:
            QTimer.singleShot(0, lambda: self.failed.emit(FakeProbe.fail, "why"))
        else:
            QTimer.singleShot(0, lambda: self.finished.emit(FakeProbe.scroll))

    def cancel(self):
        pass

    def deleteLater(self):
        self._sig.deleteLater()


@pytest.fixture
def probes(monkeypatch):
    from rmdesk.ui.live_compose import LiveCompose
    FakeProbe.made, FakeProbe.scroll, FakeProbe.fail, FakeProbe.events = [], 321, None, []
    monkeypatch.setattr(LiveCompose, "probe_factory", FakeProbe)
    return FakeProbe


def _no_handwriting(monkeypatch):
    """Every fit of the page finds no strokes (as on a typed-only page)."""
    from rmdesk.compose import scroll_fit
    monkeypatch.setattr(scroll_fit, "fit_details",
                        lambda picture, points: {"scroll": None, "points": 0})


def _typed_only_send(qtbot, tmp_path, monkeypatch, mode="Landscape", control=True,
                     handwriting=False):
    calls = record_send(monkeypatch, tmp_path)
    real = build.frame_to_lines
    seen = {}
    monkeypatch.setattr(build, "frame_to_lines", lambda *a, **k: seen.update(k) or real(*a, **k))
    if not handwriting:
        _no_handwriting(monkeypatch)
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path)
    view._auto_on = control  # Control on whenever it can be (the app's default)
    view.frame = frame(255)
    dev.run_handler = folio_handler(mode)
    texts = []
    answer_confirm(panel, True, texts)
    panel.add_ink_text("Hello")
    return calls, seen, view, panel, dev, cfg, texts


def _wait_sent_or_refused(qtbot, panel, cfg):
    qtbot.waitUntil(lambda: not panel.busy and (cfg.last_send is not None
                                                or bool(panel.message.text())), timeout=10000)


def test_a_typed_only_page_is_measured_then_sent_with_that_scroll(qtbot, tmp_path, fw,
                                                                 monkeypatch, probes):
    from rmdesk.compose import frame as fr
    calls, seen, view, panel, dev, cfg, texts = _typed_only_send(qtbot, tmp_path, monkeypatch)
    orig_fake = safety.send_to_open_page
    monkeypatch.setattr(safety, "send_to_open_page",
                        lambda *a, **k: probes.events.append("send") or orig_fake(*a, **k))
    statuses = []
    panel.status.setText = lambda t, f=panel.status.setText: statuses.append(t) or f(t)
    panel.send()
    _wait_sent_or_refused(qtbot, panel, cfg)
    assert cfg.last_send is not None and panel.message.text().startswith("Sent to \u201cPhysics notes\u201d")
    assert seen == {"layout": fr.KEYBOARD, "scroll": 321.0}
    # measured first (no job held, xochitl running), then one send
    assert probes.events == ["probe", "send"] and len(calls) == 1
    p = probes.made[0]
    assert p.job_at_start is None and p.touch is view.control and p.frames is view
    assert p.f0.shape == view.frame.gray.shape and p.f0 is not view.frame.gray  # a copy
    assert msg.MEASURING in statuses
    assert texts and texts[0].endswith(msg.MEASURED_NOTE)


def test_a_refused_measurement_sends_nothing(qtbot, tmp_path, fw, monkeypatch, probes):
    probes.fail = msg.MEASURE_MOVED
    calls, seen, view, panel, dev, cfg, _ = _typed_only_send(qtbot, tmp_path, monkeypatch)
    panel.send()
    _wait_sent_or_refused(qtbot, panel, cfg)
    assert panel.message.text() == msg.MEASURE_MOVED and panel.details.text() == "Details: why"
    assert calls == [] and cfg.last_send is None and nothing_written(dev)
    assert not panel.busy and panel.status.text() == ""


def test_the_portrait_layout_never_measures(qtbot, tmp_path, fw, monkeypatch, probes):
    from rmdesk.compose import frame as fr
    calls, seen, view, panel, dev, cfg, _ = _typed_only_send(qtbot, tmp_path, monkeypatch,
                                                             mode="Portrait")
    panel.send()
    _wait_sent_or_refused(qtbot, panel, cfg)
    assert probes.made == [] and cfg.last_send is not None
    assert seen["layout"] == fr.PORTRAIT_UI


def test_without_control_the_old_refusal_stays(qtbot, tmp_path, fw, monkeypatch, probes):
    calls, seen, view, panel, dev, cfg, _ = _typed_only_send(qtbot, tmp_path, monkeypatch,
                                                             control=False)
    panel.send()
    _wait_sent_or_refused(qtbot, panel, cfg)
    assert panel.message.text() == msg.SCROLL_NO_STROKES and probes.made == []
    assert cfg.last_send is None and seen == {}


def test_a_page_with_handwriting_is_not_measured(qtbot, tmp_path, fw, monkeypatch, probes):
    from rmdesk.compose import scroll_fit
    monkeypatch.setattr(scroll_fit, "fit_details",
                        lambda picture, points: {"scroll": 678.0, "points": 100})
    calls, seen, view, panel, dev, cfg, texts = _typed_only_send(
        qtbot, tmp_path, monkeypatch, handwriting=True)
    panel.send()
    _wait_sent_or_refused(qtbot, panel, cfg)
    assert probes.made == [] and seen["scroll"] == 678.0
    assert msg.MEASURED_NOTE not in texts[0]


def test_a_stale_hint_measures_after_the_refusal_and_sends_again(qtbot, tmp_path, fw,
                                                                monkeypatch, probes):
    """The page xochitl had recorded had handwriting, the open one has none: that send is
    refused inside (nothing written, xochitl back), then measured and sent again."""
    from rmdesk.ui import live_compose as lc
    monkeypatch.setattr(lc, "needs_measuring", lambda dev, picture, fx: False)
    probes.scroll = 250
    calls, seen, view, panel, dev, cfg, texts = _typed_only_send(qtbot, tmp_path, monkeypatch)
    panel.send()
    _wait_sent_or_refused(qtbot, panel, cfg)
    assert len(calls) == 2 and "new_page" not in calls[0] and calls[1]["n"] > 0
    assert seen["scroll"] == 250.0 and len(probes.made) == 1
    assert probes.made[0].job_at_start is None
    assert texts[-1].endswith(msg.MEASURED_NOTE) and panel.message.text().startswith("Sent to \u201cPhysics notes\u201d")


def test_no_touch_screen_refuses_as_before(qtbot, tmp_path, fw, monkeypatch):
    """The real probe and Control: the fake tablet lists no touch screen."""
    from tests.scroll_sim import frame_at, text_page
    calls, seen, view, panel, dev, cfg, _ = _typed_only_send(qtbot, tmp_path, monkeypatch)
    view.frame = Frame(1404, 1872, frame_at(text_page(seed=2), 300))
    panel.send()
    _wait_sent_or_refused(qtbot, panel, cfg)
    assert panel.message.text() == msg.SCROLL_NO_STROKES
    assert "touch screen" in panel.details.text()
    assert calls == [] and nothing_written(dev)


def test_the_real_probe_on_a_simulated_tablet_measures_and_sends(qtbot, tmp_path, fw,
                                                                monkeypatch):
    """End to end: ScrollProbe swipes a simulated page to its top; the send uses the scroll."""
    from rmdesk.compose import frame as fr
    from rmdesk.ui.compose.scroll_probe import ScrollProbe
    from rmdesk.ui.live_compose import LiveCompose
    from tests.scroll_sim import frame_at, text_page
    from tests.ui.test_scroll_probe import FAST, Session, Tablet, Touch
    page = text_page(seed=4)
    tablet = Tablet(page, 777)
    session = Session(tablet)
    monkeypatch.setattr(LiveCompose, "probe_factory", staticmethod(
        lambda f0, touch, frames, busy, parent=None:
        ScrollProbe(f0, Touch(session), tablet, busy, parent, **FAST)))
    calls, seen, view, panel, dev, cfg, texts = _typed_only_send(qtbot, tmp_path, monkeypatch)
    view.frame = Frame(1404, 1872, frame_at(page, 777))
    panel.send()
    _wait_sent_or_refused(qtbot, panel, cfg)
    assert cfg.last_send is not None, panel.message.text()
    assert seen == {"layout": fr.KEYBOARD, "scroll": 777.0} and tablet.scroll == 0
    import math
    from rmdesk.compose import scroll_measure as sm
    # the check swipe down (+200), then 400 px swipes up over 977, then two that move nothing
    assert len(session.swiped) == 1 + math.ceil((777 + sm.CHECK_LENGTH) / sm.SWIPE_LENGTH) + 2


# -- line art pictures ------------------------------------------------------------------

def _diagram(path: Path, w=300, h=200) -> Path:
    """A flowchart-like picture: two boxes, an arrow between them, a label."""
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    p = QPainter(img)
    p.setPen(QPen(QColor("black"), 3))
    p.drawRect(20, 40, 90, 60)
    p.drawRect(190, 40, 90, 60)
    p.drawLine(110, 70, 190, 70)
    p.drawLine(180, 62, 190, 70)
    p.drawLine(180, 78, 190, 70)
    p.drawText(35, 75, "Start")
    p.end()
    img.save(str(path))
    return path


def _photo(path: Path, w=300, h=200) -> Path:
    rng = np.random.default_rng(3)
    import cv2
    g = cv2.GaussianBlur(rng.integers(0, 256, (h, w), dtype=np.uint8), (0, 0), 5)
    cv2.imwrite(str(path), cv2.normalize(g, None, 0, 255, cv2.NORM_MINMAX))
    return path


def _lineart_seen(monkeypatch):
    from rmdesk.compose import lineart
    seen = []
    real = shade_job.lineart_item
    monkeypatch.setattr(shade_job, "lineart_item",
                        lambda m, *a: seen.append((m.pen.tool,) + tuple(a)) or real(m, *a))
    return seen, lineart


def test_line_art_style_controls_follow_the_style(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_image(_draw(tmp_path / "pic.png"))
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    assert panel.style_controls.isVisible() and panel.style_seg.index() == 0
    assert panel.shade_controls.isVisible() and not panel.lineart_controls.isVisible()
    assert panel.cross_check.isVisible() and panel.invert_check.isVisible()
    panel.style_seg.buttons[1].click()
    assert item.settings.style == shade_job.LINEART
    assert not panel.shade_controls.isVisible() and panel.lineart_controls.isVisible()
    assert not panel.cross_check.isVisible() and panel.invert_check.isVisible()
    assert (panel.detail_spin.minimum(), panel.detail_spin.maximum(),
            panel.detail_spin.value()) == (1, 5, 3)
    assert (panel.threshold_spin.minimum(), panel.threshold_spin.maximum(),
            panel.threshold_spin.value()) == (0.5, 2.0, 1.0)
    # the pencil (shading's pen) became the fineliner (line art's), and back
    assert item.pen.tool == "fineliner" and panel.tool_combo.currentText() == "fineliner"
    panel.style_seg.buttons[0].click()
    assert item.pen.tool == "pencil" and panel.shade_controls.isVisible()
    # a pen the user chose stays
    panel.tool_combo.setCurrentText("ballpoint")
    panel.style_seg.buttons[1].click()
    assert item.pen.tool == "ballpoint"
    # selecting another picture shows its own style
    other = panel.add_image(_draw(tmp_path / "b.png"))
    assert panel.style_seg.index() == 0 and panel.shade_controls.isVisible()
    panel.scene.clearSelection()
    item.setSelected(True)
    assert panel.style_seg.index() == 1 and panel.lineart_controls.isVisible()
    panel.scene.clearSelection()
    assert not panel.style_controls.isVisible() and not panel.lineart_controls.isVisible()
    assert other.settings.style == shade_job.SHADED


def test_style_switch_redraws_debounced(qtbot, tmp_path, fw, monkeypatch):
    seen, _ = _lineart_seen(monkeypatch)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_image(_diagram(tmp_path / "d.png"))
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    shaded = item.preview_strokes()
    panel.style_seg.buttons[1].click()
    for v in (4, 5, 2):
        panel.detail_spin.setValue(v)
    panel.threshold_spin.setValue(1.2)
    panel.invert_check.setChecked(True)
    panel.invert_check.setChecked(False)
    assert not item.current()
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    # one redraw, after the last change (sharp text on when Tesseract is there)
    assert seen == [("fineliner", 2, 1.2, False, sharptext.available())]
    assert item.shaded_key[-1].style == shade_job.LINEART and item.ready()
    lines = item.preview_strokes()
    assert lines != shaded
    # the lines at the pen's width; the label's letters finer, at their own stroke width
    widths = {w for st in lines for _x, _y, w in st}
    assert max(widths) == build.PEN_WIDTH["fineliner"] and min(widths) >= 4


def test_line_art_preview_is_lineart_item_for_the_current_box(qtbot, tmp_path, fw):
    from rmdesk.compose import lineart
    view, panel, *_ = make_screen(qtbot, tmp_path)
    src = _diagram(tmp_path / "d.png")
    item = panel.add_image(src, style=shade_job.LINEART)
    assert item.pen.tool == "fineliner"
    item.setPos(310, 420)
    item.set_rotation(30)
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    res = lineart.lineart_item(item.model(), sharp_text=item.settings.sharp_text)
    assert item.shaded.result.strokes == res.strokes and res.strokes
    b = item.box()
    want = [[(p.x + 702 - b.x, p.y - b.y, p.width) for p in ln.points]
            for ln in build.shade_to_lines(item.model(), res, item.pen)]
    _approx_strokes(item.preview_strokes(), want)


def test_sharp_text_check_in_the_line_art_controls(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    ok, why = sharptext.status()
    assert panel.sharp_check.isEnabled() == ok and panel.sharp_check.isChecked() == ok
    if not ok:
        assert panel.sharp_check.toolTip() == why
    item = panel.add_image(_diagram(tmp_path / "d.png"), style=shade_job.LINEART)
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    assert panel.sharp_check.isVisible() and item.settings.sharp_text == ok
    panel.style_seg.buttons[0].click()
    assert not panel.sharp_check.isVisible()


def test_sharp_text_reads_in_the_worker_debounced(qtbot, tmp_path, fw, monkeypatch):
    seen = []
    monkeypatch.setattr(sharptext, "recognise",
                        lambda path, raw=None, invert=False:
                        seen.append(threading.current_thread()) or ())
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_image(_diagram(tmp_path / "d.png"), style=shade_job.LINEART)
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    panel.sharp_check.setChecked(False)
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    seen.clear()
    off = item.shaded.result.strokes
    panel.sharp_check.setChecked(True)
    panel.sharp_check.setChecked(False)
    panel.sharp_check.setChecked(True)
    assert item.settings.sharp_text and not item.current()
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    assert seen and all(t is not threading.main_thread() for t in seen)
    assert len(seen) == 1  # one job after the last change (no words: nothing else to do)
    assert item.shaded.result.strokes == off  # nothing recognised: the same line art
    assert item.shaded_key[-1].sharp_text


@pytest.mark.parametrize("rotation", [0, 35])
def test_line_art_sent_equals_preview(qtbot, tmp_path, fw, monkeypatch, rotation):
    calls = record_send(monkeypatch, tmp_path)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_image(_diagram(tmp_path / "d.png"), style=shade_job.LINEART)
    item.setPos(300, 500)
    item.set_rotation(rotation)
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    preview = item.preview_strokes()
    panel.send()
    qtbot.waitUntil(lambda: bool(calls) and not panel.busy, timeout=10000)
    _approx_strokes(_sent_local(calls[0], (300, 500)), preview)
    lines = _new_lines(calls[0]["new_page"])
    assert all(ln.tool == build._TOOL["fineliner"] for ln in lines)
    assert {pt.pressure for ln in lines for pt in ln.points} == {build.PRESSURE}


def test_line_art_keyboard_scroll_send_maps_the_preview(qtbot, tmp_path, fw, monkeypatch):
    from rmdesk.compose import frame as fr
    from rmdesk.compose import scroll_fit
    from rmdesk.notebook.opened import OpenedPage
    op = OpenedPage(NB, "Physics notes", PG2, 1, 2, "portrait", ORIG, vertical_scroll=678.0)
    calls = record_send(monkeypatch, tmp_path, opened=op)
    monkeypatch.setattr(scroll_fit, "fit_details", lambda picture, points: {"scroll": 678.0})
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path)
    view.frame = frame(255)
    dev.run_handler = folio_handler("Landscape")
    item = panel.add_image(_diagram(tmp_path / "d.png"), style=shade_job.LINEART)
    item.setPos(400, 600)
    item.set_rotation(20)
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    preview = item.preview_strokes()
    panel.send()
    qtbot.waitUntil(lambda: not panel.busy and cfg.last_send is not None, timeout=10000)
    got = [[(p.x, p.y, p.width) for p in ln.points] for ln in _new_lines(calls[0]["new_page"])]
    want = [[(*fr.frame_to_page(x + 400, y + 600, "portrait", layout=fr.KEYBOARD, scroll=678.0),
              w) for x, y, w in st] for st in preview]
    _approx_strokes(got, want)


def test_line_art_anchored_send_maps_the_preview(qtbot, tmp_path, fw, monkeypatch):
    from rmdesk.notebook.opened import OpenedPage
    data, gray, a1 = _anchored_page_and_picture(const=820.0)
    op = OpenedPage(NB, "Physics notes", PG2, 1, 2, "portrait", data)
    calls = record_send(monkeypatch, tmp_path, opened=op)
    from rmdesk.ui import live_compose as lc
    monkeypatch.setattr(lc, "DEBUG_DIR", tmp_path / "debug")
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path)
    view.frame = Frame(1404, 1872, gray)
    dev.run_handler = folio_handler("Landscape")
    item = panel.add_image(_diagram(tmp_path / "d.png"), style=shade_job.LINEART)
    item.setPos(500, 700)
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    preview = item.preview_strokes()
    panel.send()
    qtbot.waitUntil(lambda: not panel.busy and (cfg.last_send is not None
                                                or bool(panel.message.text())), timeout=10000)
    assert cfg.last_send is not None, panel.message.text()
    new = calls[0]["new_page"]
    pagemod.verify(data, new, calls[0]["n"], None)
    old_ids = set(rmscene.read_tree(io.BytesIO(data))._node_ids)
    tree = rmscene.read_tree(io.BytesIO(new))
    (g,) = [tree[i] for i in tree._node_ids if i not in old_ids and tree[i].anchor_id]
    got = [[(p.x, p.y, p.width) for p in ln.points] for ln in g.children.values()]
    want = [[(y + 700 - 936 + 464, 820.0 - (x + 500), w) for x, y, w in st] for st in preview]
    assert len(got) == len(want) > 0
    for sa, sb in zip(got, want):
        assert len(sa) == len(sb)
        for pa, pb in zip(sa, sb):
            assert max(abs(u - v) for u, v in zip(pa, pb)) <= 2.5


def test_added_picture_style_is_guessed_from_its_content(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    diagram = panel.add_image(_diagram(tmp_path / "d.png"), style=shade_job.AUTO)
    qtbot.waitUntil(_shaded(panel, diagram), timeout=10000)
    assert diagram.settings.style == shade_job.LINEART and diagram.pen.tool == "fineliner"
    assert diagram.shaded_key == diagram.key()  # no second drawing for the guess
    assert panel.style_seg.index() == 1 and panel.lineart_controls.isVisible()
    assert panel.tool_combo.currentText() == "fineliner"
    photo = panel.add_image(_photo(tmp_path / "p.png"), centre=QPointF(700, 1400),
                            style=shade_job.AUTO)
    qtbot.waitUntil(_shaded(panel, photo), timeout=10000)
    assert photo.settings.style == shade_job.SHADED and photo.pen.tool == "pencil"
    assert panel.style_seg.index() == 0 and panel.shade_controls.isVisible()
    # the user can switch: the photo as line art
    panel.style_seg.buttons[1].click()
    qtbot.waitUntil(_shaded(panel, photo), timeout=10000)
    assert photo.settings.style == shade_job.LINEART
    # the confirm sheet says which style each picture is drawn in
    from rmdesk.ui.live_compose import LiveCompose
    assert [LiveCompose._describe(i) for i in (diagram, photo)] == [
        "Picture (line art): d.png", "Picture (line art): p.png"]


def test_describe_names_the_picture_style(qtbot, tmp_path, fw):
    from rmdesk.ui.live_compose import LiveCompose
    view, panel, *_ = make_screen(qtbot, tmp_path)
    a = panel.add_image(_draw(tmp_path / "a.png"))
    b = panel.add_image(_draw(tmp_path / "b.png"), style=shade_job.LINEART)
    assert LiveCompose._describe(a) == "Picture (shaded): a.png"
    assert LiveCompose._describe(b) == "Picture (line art): b.png"


def test_a_picked_picture_is_guessed(qtbot, tmp_path, fw, monkeypatch):
    from rmdesk.ui import live_compose as lc
    view, panel, *_ = make_screen(qtbot, tmp_path)
    path = _diagram(tmp_path / "d.png")
    monkeypatch.setattr(lc.QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    panel._pick_image()
    (item,) = [i for i in panel.placed_items() if isinstance(i, ImageSceneItem)]
    assert item.settings.style == shade_job.AUTO and item.placeholder() == "Shading…"
    qtbot.waitUntil(_shaded(panel, item), timeout=10000)
    assert item.settings.style == shade_job.LINEART



# ---------------------------------------------------------------- no confirm by default

def test_send_goes_straight_in_without_the_sheet_by_default(qtbot, tmp_path, fw):
    assert Config().confirm_sends is False
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path, confirm=False)
    asked = []
    panel.bridge.confirm = lambda *a, **k: asked.append(a) or True
    panel.add_ink_text("Hello")
    panel.send()
    qtbot.waitUntil(lambda: cfg.last_send is not None and not panel.busy, timeout=10000)
    assert asked == []  # clicking Send was the confirmation
    assert panel.message.text().startswith("Sent to \u201cPhysics notes\u201d, page 2 of 2.")
    assert "Undo last send" in panel.message.text()


def test_the_setting_brings_the_sheet_back(qtbot, tmp_path, fw):
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path, confirm=True)
    asked = []
    panel.bridge.confirm = lambda text, listing=None: asked.append(text) or True
    panel.add_ink_text("Hello")
    panel.send()
    qtbot.waitUntil(lambda: cfg.last_send is not None and not panel.busy, timeout=10000)
    assert len(asked) == 1
