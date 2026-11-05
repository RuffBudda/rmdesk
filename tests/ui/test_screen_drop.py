"""Files dragged onto the Screen view: PDFs/EPUBs upload to My files through the Files view's
upload (the same ops call, Fast USB uploads, the job slot), pictures are placed where they
are dropped (Place), and the drag shows an outline saying what will happen."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QThreadPool, QUrl
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QDropEvent, QImage
from PySide6.QtWidgets import QApplication

from rmdesk.config import Config
from rmdesk.library import ops
from rmdesk.ui import screen_view as sv
from rmdesk.ui.compose.scene import ImageSceneItem
from rmdesk.ui.files_view import FilesView
from tests.ui.test_screen_pointer import FwConnection, make
from tests.ui.test_screen_view import NoSecrets


@pytest.fixture(autouse=True)
def drain_workers(qtbot):
    yield
    QThreadPool.globalInstance().waitForDone(5000)
    QApplication.processEvents()


class NoWeb:
    def available(self):
        return False


class FastWeb:
    """The tablet's USB web interface switched on (Fast USB uploads)."""
    uploads: list = []

    def available(self):
        return True

    def upload(self, path, folder_id):
        FastWeb.uploads.append((path.name, folder_id))


def _mime(paths):
    m = QMimeData()
    m.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    return m


def drag_enter(target, paths, pos=QPoint(50, 50)):
    mime = _mime(paths)
    e = QDragEnterEvent(pos, Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(target, e)
    return e, mime


def drop(target, paths, pos=QPoint(50, 50)):
    """Enter, move and drop at `pos` on `target` (as a real drag does)."""
    enter, mime = drag_enter(target, paths, pos)
    mv = QDragMoveEvent(pos, Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(target, mv)
    e = QDropEvent(QPointF(pos), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                   Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(target, e)
    return e


def picture(tmp_path, name="pic.png", w=40, h=20):
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(Qt.GlobalColor.black)
    p = tmp_path / name
    assert img.save(str(p))
    return p


def document(tmp_path, name="notes.pdf"):
    p = tmp_path / name
    p.write_bytes(b"%PDF-1.4\n%%EOF\n")
    return p


def with_files(qtbot, view, conn, web=NoWeb):
    """The Screen view uploads through a Files view, as in the main window."""
    from rmdesk.firmware import XOCHITL_DIR
    conn.device.files.setdefault(f"{XOCHITL_DIR}/.keep", b"")  # an empty library
    files = FilesView(conn, web_factory=web)
    qtbot.addWidget(files)
    reports = []
    files.show_report = lambda report, title="": reports.append(report)
    files.show_error = lambda msg: reports.append(msg)
    view.upload_fn = files.upload_dropped
    return files, reports


def spy_upload(monkeypatch, result=None):
    calls = []

    def upload(dev, transport, paths, folder_id, web=None):
        calls.append((dev, transport, [p.name for p in paths], folder_id, web))
        return result or ops.UploadReport([ops.TransferResult(p, True) for p in paths])
    monkeypatch.setattr(ops, "upload", upload)
    return calls


# ---------------------------------------------------------------- PDFs and EPUBs

def test_pdf_drop_uploads_to_my_files_like_files_upload(qtbot, tmp_path, monkeypatch):
    view, conn, _ = make(qtbot)
    files, reports = with_files(qtbot, view, conn)
    calls = spy_upload(monkeypatch)
    tablet = conn.device
    pdf = document(tmp_path)
    drop(view.canvas.viewport(), [pdf])
    assert view.drop_message() == "Uploading notes.pdf…"
    qtbot.waitUntil(lambda: view.drop_message() == "Uploaded notes.pdf", timeout=5000)
    assert len(calls) == 1
    dev, transport, names, folder, web = calls[0]
    assert dev is tablet and transport == "usb" and names == ["notes.pdf"]
    assert folder == ""  # the tablet's top level: My files
    assert isinstance(web, NoWeb)  # the Files view's web interface client (Fast USB uploads)
    assert view.drop_chip.state() == "ok" and conn.current_job is None
    assert view.pointer == "control"  # an upload doesn't change the pointer


def test_fast_usb_uploads_go_through_the_web_interface(qtbot, tmp_path):
    view, conn, _ = make(qtbot)
    FastWeb.uploads = []
    files, reports = with_files(qtbot, view, conn, web=FastWeb)
    drop(view, [document(tmp_path, "a.pdf"), document(tmp_path, "b.epub")])  # on the header
    qtbot.waitUntil(lambda: view.drop_message() == "Uploaded 2 files", timeout=5000)
    assert FastWeb.uploads == [("a.pdf", ""), ("b.epub", "")]
    assert not any(cmd.startswith("systemctl restart xochitl")
                   for m, cmd in conn.device.log if m == "run")  # no restart needed


def test_upload_waits_for_the_job_slot(qtbot, tmp_path, monkeypatch):
    view, conn, _ = make(qtbot)
    with_files(qtbot, view, conn)
    calls = spy_upload(monkeypatch)
    assert conn.try_begin_job("sending to the tablet")
    drop(view.canvas.viewport(), [document(tmp_path)])
    assert view.drop_message() == "The tablet is busy with: sending to the tablet. " \
                                  "Try again when it's done."
    assert view.drop_chip.state() == "err"
    conn.end_job()
    QThreadPool.globalInstance().waitForDone(3000)
    assert calls == []


def test_upload_problems_use_the_files_texts(qtbot, tmp_path, monkeypatch):
    view, conn, _ = make(qtbot)
    files, reports = with_files(qtbot, view, conn)
    pdf = document(tmp_path)
    spy_upload(monkeypatch, ops.UploadReport([ops.TransferResult(pdf, False, "tablet storage full")]))
    drop(view.canvas.viewport(), [pdf])
    qtbot.waitUntil(lambda: view.drop_message() == "Upload failed", timeout=5000)
    assert len(reports) == 1 and ops.UploadReport is type(reports[0])
    from rmdesk.ui.files_view import report_text
    assert report_text(reports[0]) == "notes.pdf: tablet storage full"  # Files' dialog text


def test_restart_clears_the_pending_banner(qtbot, tmp_path, monkeypatch):
    view, conn, _ = make(qtbot)
    files, _r = with_files(qtbot, view, conn)
    conn.add_pending(2)
    pdf = document(tmp_path)
    spy_upload(monkeypatch, ops.UploadReport([ops.TransferResult(pdf, True)], restarted=True))
    drop(view.canvas.viewport(), [pdf])
    qtbot.waitUntil(lambda: view.drop_message() == "Uploaded notes.pdf", timeout=5000)
    assert conn.pending_refresh == 0


# ---------------------------------------------------------------- pictures

def test_picture_drop_switches_to_place_and_centres_on_the_drop_point(qtbot, tmp_path):
    view, conn, _ = make(qtbot)
    assert view.pointer == "control"
    vp = view.canvas.viewport()
    pos = QPoint(vp.width() // 3, vp.height() // 4)
    want = view.canvas.mapToScene(pos)
    drop(vp, [picture(tmp_path)], pos)
    assert view.pointer == "place"
    items = [i for i in view.compose.placed_items() if isinstance(i, ImageSceneItem)]
    assert len(items) == 1
    it = items[0]
    centre = it.pos() + QPointF(it.w / 2, it.h / 2)
    assert abs(centre.x() - want.x()) < 1 and abs(centre.y() - want.y()) < 1
    assert it.isSelected()
    QThreadPool.globalInstance().waitForDone(5000)  # the shading


def test_picture_drop_in_a_rotated_view_maps_through_the_view(qtbot, tmp_path):
    view, conn, _ = make(qtbot)
    view.rotate()
    QApplication.processEvents()
    vp = view.canvas.viewport()
    pos = QPoint(vp.width() // 2 + 60, vp.height() // 2 - 90)
    want = view.canvas.mapToScene(pos)
    drop(vp, [picture(tmp_path, "pic.jpg")], pos)
    it = view.compose.placed_items()[0]
    assert abs(it.sceneBoundingRect().center().x() - want.x()) < 2
    assert abs(it.sceneBoundingRect().center().y() - want.y()) < 2


def test_picture_dropped_on_the_header_goes_in_the_middle(qtbot, tmp_path):
    view, conn, _ = make(qtbot)
    drop(view, [picture(tmp_path, "pic.webp")], QPoint(10, 10))
    it = view.compose.placed_items()[0]
    c = view.compose.scene.sceneRect().center()
    assert abs(it.sceneBoundingRect().center().x() - c.x()) < 1


def test_mixed_drop_uploads_and_places(qtbot, tmp_path, monkeypatch):
    view, conn, _ = make(qtbot)
    with_files(qtbot, view, conn)
    calls = spy_upload(monkeypatch)
    paths = [document(tmp_path), picture(tmp_path), picture(tmp_path, "b.bmp")]
    enter, _m = drag_enter(view.canvas.viewport(), paths)
    assert view.drop_outline.text() == sv.DROP_BOTH
    drop(view.canvas.viewport(), paths)
    qtbot.waitUntil(lambda: view.drop_message() == "Uploaded notes.pdf", timeout=5000)
    assert [c[2] for c in calls] == [["notes.pdf"]]  # only the document is uploaded
    assert len(view.compose.placed_items()) == 2 and view.pointer == "place"


def test_unsupported_files_are_refused(qtbot, tmp_path, monkeypatch):
    view, conn, _ = make(qtbot)
    with_files(qtbot, view, conn)
    calls = spy_upload(monkeypatch)
    txt = tmp_path / "notes.txt"
    txt.write_text("hi")
    enter, _m = drag_enter(view.canvas.viewport(), [txt])
    assert view.drop_outline.isVisible() and view.drop_outline.refused()
    assert view.drop_outline.text() == sv.DROP_UNSUPPORTED
    drop(view.canvas.viewport(), [txt])
    assert view.drop_message() == sv.DROP_UNSUPPORTED and view.drop_chip.state() == "err"
    assert not view.drop_outline.isVisible()
    QThreadPool.globalInstance().waitForDone(3000)
    assert calls == [] and view.compose.placed_items() == [] and view.pointer == "control"


def test_offline_drops_are_refused(qtbot, tmp_path, monkeypatch):
    view, conn, _ = make(qtbot)
    with_files(qtbot, view, conn)
    calls = spy_upload(monkeypatch)
    conn.disconnect()
    qtbot.waitUntil(lambda: not view.compose.allowed, timeout=2000)
    paths = [document(tmp_path), picture(tmp_path)]
    drag_enter(view.canvas.viewport(), paths)
    assert view.drop_outline.text() == "Connect the tablet first." and view.drop_outline.refused()
    drop(view.canvas.viewport(), paths)
    assert view.drop_message() == "Connect the tablet first."
    QThreadPool.globalInstance().waitForDone(3000)
    assert calls == [] and view.compose.placed_items() == []


# ---------------------------------------------------------------- the outline

def test_drag_outline_appears_over_the_picture_and_clears(qtbot, tmp_path):
    view, conn, _ = make(qtbot)
    vp = view.canvas.viewport()
    out = view.drop_outline
    assert not out.isVisible()
    drag_enter(vp, [document(tmp_path)])
    assert out.isVisible() and out.text() == "Upload to tablet" and not out.refused()
    page = view.canvas.page_rect().toAlignedRect().intersected(vp.rect())
    assert out.geometry().topLeft() == vp.mapTo(view, page.topLeft())
    assert out.size() == page.size()
    assert out.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    QApplication.sendEvent(vp, QDragLeaveEvent())
    assert not out.isVisible()
    drag_enter(vp, [picture(tmp_path)])
    assert out.isVisible() and out.text() == "Place picture here"
    img = view.grab().toImage()
    edge = out.geometry().topLeft() + QPoint(out.width() // 2, 2)
    c = img.pixelColor(edge)
    assert c.red() > 150 and c.green() < 90  # the red outline
    QApplication.sendEvent(vp, QDragLeaveEvent())
    assert not out.isVisible()


def test_drop_messages_clear_after_a_while(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "DROP_MSG_MS", 50)
    view, conn, _ = make(qtbot)
    txt = tmp_path / "a.doc"
    txt.write_text("x")
    view._drop_timer.setInterval(50)
    drop(view, [txt])
    assert view.drop_message() and view.overlay.isVisible()
    qtbot.waitUntil(lambda: view.drop_message() == "", timeout=2000)


def test_add_image_takes_what_a_drop_takes():
    from rmdesk.ui.live_compose import IMAGE_FILTER, IMAGE_SUFFIXES
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"):
        assert ext in IMAGE_SUFFIXES and f"*{ext}" in IMAGE_FILTER
    assert sv.sort_drop([Path("a.PDF"), Path("b.Png"), Path("c.zip")]) == \
        ([Path("a.PDF")], [Path("b.Png")], [Path("c.zip")])


def test_main_window_uploads_drops_through_its_files_view(qtbot, tmp_path, monkeypatch):
    from rmdesk.ui.app import MainWindow
    view, conn, _ = make(qtbot)
    view.hide()
    from rmdesk.firmware import XOCHITL_DIR
    conn.device.files.setdefault(f"{XOCHITL_DIR}/.keep", b"")
    win = MainWindow(conn, Config(), save_config=lambda c: None, web_factory=NoWeb)
    qtbot.addWidget(win)
    win.files_view.show_report = lambda *a, **k: None
    calls = spy_upload(monkeypatch)
    win.show()
    win.show_view("Screen")
    s = win.screen_view
    assert s.upload_fn == win.files_view.upload_dropped
    drop(s.canvas.viewport(), [document(tmp_path)])
    qtbot.waitUntil(lambda: s.drop_message() == "Uploaded notes.pdf", timeout=5000)
    assert [c[2] for c in calls] == [["notes.pdf"]] and isinstance(calls[0][4], NoWeb)
    win.close()


def test_a_dropped_picture_starts_with_its_style_guessed(qtbot, tmp_path):
    from rmdesk.ui.compose import shade_job
    view, conn, _ = make(qtbot)
    vp = view.canvas.viewport()
    drop(vp, [picture(tmp_path)], QPoint(vp.width() // 2, vp.height() // 2))
    (it,) = [i for i in view.compose.placed_items() if isinstance(i, ImageSceneItem)]
    assert it.settings.style == shade_job.AUTO  # the first drawing decides (content guess)
    QThreadPool.globalInstance().waitForDone(5000)
    QApplication.processEvents()
    assert it.settings.style in (shade_job.SHADED, shade_job.LINEART)


# ---------------------------------------------------------------- Ctrl+V

def _ctrl_v(widget):
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtCore import QEvent
    for t in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
        QApplication.sendEvent(widget, QKeyEvent(t, Qt.Key.Key_V,
                                                 Qt.KeyboardModifier.ControlModifier, "\x16"))


def _clip_image():
    from PySide6.QtGui import QGuiApplication
    img = QImage(60, 30, QImage.Format.Format_RGB32)
    img.fill(Qt.GlobalColor.white)
    QGuiApplication.clipboard().setImage(img)


def test_ctrl_v_with_a_picture_places_it_from_control(qtbot):
    view, conn, made = make(qtbot)
    qtbot.waitUntil(lambda: view.control.session is not None, timeout=5000)
    _clip_image()
    view.canvas.setFocus()
    _ctrl_v(view.canvas)
    assert view.pointer == "place"
    items = [i for i in view.compose.placed_items() if isinstance(i, ImageSceneItem)]
    assert len(items) == 1 and Path(items[0].source).name.startswith("pasted-")
    kb = [w for w in made if w.path.endswith("event3")]
    assert kb and kb[0].data() == b""  # nothing typed on the tablet


def test_ctrl_v_with_text_in_control_still_types(qtbot):
    from PySide6.QtGui import QGuiApplication
    view, conn, made = make(qtbot)
    qtbot.waitUntil(lambda: view.control.session is not None, timeout=5000)
    QGuiApplication.clipboard().setText("ab")
    view.canvas.setFocus()
    _ctrl_v(view.canvas)
    assert view.pointer == "control" and view.compose.placed_items() == []
    qtbot.waitUntil(lambda: not view.control._paste_timer.isActive(), timeout=3000)
    assert "Typed 2 characters" in view.control.message


def test_ctrl_v_in_place_pastes_a_picture_or_makes_an_ink_text(qtbot):
    from PySide6.QtGui import QGuiApplication
    from rmdesk.ui.compose.scene import InkSceneItem
    view, conn, _ = make(qtbot)
    view.set_pointer("place")
    view.canvas.setFocus()
    _clip_image()
    _ctrl_v(view.canvas)
    assert len([i for i in view.compose.placed_items() if isinstance(i, ImageSceneItem)]) == 1
    QGuiApplication.clipboard().setText("Hello from the clipboard")
    view.compose.scene.clearSelection()
    _ctrl_v(view.canvas)
    inks = [i for i in view.compose.placed_items() if isinstance(i, InkSceneItem)]
    assert len(inks) == 1 and inks[0].text == "Hello from the clipboard"


def test_ctrl_v_with_copied_files_acts_like_a_drop(qtbot, tmp_path, monkeypatch):
    view, conn, _ = make(qtbot)
    md = _mime([picture(tmp_path)])  # copied files (a file manager's Copy)

    class Clip:  # handing a QMimeData to the real clipboard double-frees it at exit (PySide)
        def mimeData(self):
            return md
    monkeypatch.setattr(sv.QGuiApplication, "clipboard", staticmethod(lambda: Clip()))
    view.set_pointer("place")
    view.canvas.setFocus()
    _ctrl_v(view.canvas)
    assert len([i for i in view.compose.placed_items() if isinstance(i, ImageSceneItem)]) == 1
