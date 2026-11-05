import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path

import pytest

from PySide6.QtCore import QBuffer, QThreadPool, QIODevice, QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent, QImage
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from rmdesk.config import Config
from rmdesk.connection import Connection, State
from rmdesk.device import DeviceError
from rmdesk.library import ops
from rmdesk.library.model import XOCHITL
from rmdesk.ui import files_view as fv
from rmdesk.ui.files_view import FilesView
from tests.fakes import FakeDevice

FIX = Path(__file__).parent.parent / "fixtures" / "xochitl"
def _png() -> bytes:
    img = QImage(4, 4, QImage.Format.Format_RGB32)
    img.fill(0xFF0000)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


PNG = _png()


@pytest.fixture(autouse=True)
def drain_workers(qtbot):
    """Finish this test's workers and deliver their callbacks while its widgets still exist."""
    yield
    QThreadPool.globalInstance().waitForDone(5000)
    QApplication.processEvents()


class NoSecrets:
    def get(self, device_id):
        return None

    def set(self, device_id, password):
        pass


def make_device(extra=None):
    files = {f"{XOCHITL}/{p.name}": p.read_bytes() for p in FIX.iterdir() if p.is_file()}
    files[f"{XOCHITL}/doc-nb.thumbnails/pg-1.png"] = PNG
    files.update(extra or {})
    return FakeDevice(files)


def make_conn(dev, connect=True):
    conn = Connection(Config(), factory=lambda h, p: dev, secrets=NoSecrets(),
                      sleep=lambda s: None, persist=lambda c: None)
    if connect:
        conn.connect()
    return conn


class StubWeb:
    ok = False

    def available(self):
        return StubWeb.ok


def make_view(qtbot, dev=None, conn=None):
    dev = dev or make_device()
    conn = conn or make_conn(dev)
    view = FilesView(conn, web_factory=StubWeb)
    qtbot.addWidget(view)
    view.refresh()
    qtbot.waitUntil(lambda: view.library is not None, timeout=5000)
    return view, conn, dev


def names(view):
    return [view.list.item(i).text() for i in range(view.list.count())
            if view.list.item(i).data(fv.ID_ROLE) is not None]  # the last item is the drop tile


def test_files_view_lists_root(qtbot):
    view, _, _ = make_view(qtbot)
    assert names(view) == ["Alpha", "beta", "Meeting notes", "Novel", "Paper"]
    assert view.tree.model().rowCount(view.tree.model().index(0, 0)) == 2


def test_search_filters(qtbot):
    view, _, _ = make_view(qtbot)
    view.search.setText("NOTES")
    assert names(view) == ["Meeting notes"]
    view.search.setText("")
    assert len(names(view)) == 5


def test_thumbnail_loaded_for_notebook(qtbot):
    view, _, _ = make_view(qtbot)
    qtbot.waitUntil(lambda: "doc-nb" in view.thumbnails, timeout=5000)
    assert view.thumbnails["doc-nb"] is not None


def test_trash_action_calls_ops(qtbot, monkeypatch):
    view, conn, dev = make_view(qtbot)
    calls = []
    monkeypatch.setattr(ops, "trash_many", lambda d, items: (
        calls.append((d, [i.id for i in items])), [ops.TransferResult(Path("x"), True)])[1])
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    view.trash_item(view.library.items["doc-nb"])
    qtbot.waitUntil(lambda: bool(calls) and not view._busy, timeout=5000)
    assert calls == [(dev, ["doc-nb"])]
    assert conn.pending_refresh == 1


def test_actions_disabled_offline(qtbot):
    dev = make_device()
    conn = make_conn(dev, connect=False)
    view = FilesView(conn, web_factory=StubWeb)
    qtbot.addWidget(view)
    assert not view.refresh_btn.isEnabled()
    assert view.refresh_btn.toolTip() == "Tablet not connected"
    conn.connect()
    assert view.refresh_btn.isEnabled()
    conn.connection_lost(DeviceError("x"))
    assert not view.refresh_btn.isEnabled()


def test_save_pdf_disabled_for_pdf_over_wifi(qtbot):
    view, conn, _ = make_view(qtbot)
    pdf = view.library.items["doc-pdf"]
    nb = view.library.items["doc-nb"]
    assert view.can_save_pdf(nb)
    assert not view.can_save_pdf(pdf)
    assert view.pdf_tooltip(pdf) == "Connect over USB to export annotated PDFs"


def test_save_pdf_notebook_uses_writer(qtbot, monkeypatch, tmp_path):
    view, _, _ = make_view(qtbot)
    dest = tmp_path / "Meeting notes.pdf"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(dest), ""))
    seen = []

    def fake_download(device, transport, item, d, web=None, writer=None):
        seen.append((item.id, d, writer))
        d.write_bytes(b"%PDF")

    monkeypatch.setattr(ops, "download_pdf", fake_download)
    view.save_pdf(view.library.items["doc-nb"])
    qtbot.waitUntil(lambda: bool(seen), timeout=5000)
    assert seen[0][0] == "doc-nb" and seen[0][1] == dest
    from rmdesk.ui.pdf_export import write_pdf
    assert seen[0][2] is write_pdf


def test_needs_usb_message_shown(qtbot, monkeypatch, tmp_path):
    view, _, _ = make_view(qtbot)
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        lambda *a, **k: (str(tmp_path / "p.pdf"), ""))
    msgs = []
    monkeypatch.setattr(view, "show_error", msgs.append)
    view.save_pdf(view.library.items["doc-pdf"])
    qtbot.waitUntil(lambda: bool(msgs), timeout=5000)
    assert msgs == ["Connect over USB to export annotated PDFs"]


def test_save_original(qtbot, monkeypatch, tmp_path):
    dev = make_device({f"{XOCHITL}/doc-pdf.pdf": b"%PDF-orig"})
    view, _, _ = make_view(qtbot, dev)
    dest = tmp_path / "Paper.pdf"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(dest), ""))
    view.save_original(view.library.items["doc-pdf"])
    qtbot.waitUntil(dest.exists, timeout=5000)
    assert dest.read_bytes() == b"%PDF-orig"


def test_upload_shows_report_and_refreshes(qtbot, monkeypatch, tmp_path):
    view, _, _ = make_view(qtbot)
    f = tmp_path / "a.txt"
    f.write_text("x")
    reports = []
    monkeypatch.setattr(view, "show_report", reports.append)
    view.upload_files([f], "")
    qtbot.waitUntil(lambda: bool(reports), timeout=5000)
    r = reports[0]
    assert not r.results[0].ok and r.results[0].error == "not a PDF/EPUB"


def test_report_text_lists_failures_and_restart_error():
    rep = ops.UploadReport([ops.TransferResult(Path("/a/b.txt"), False, "not a PDF/EPUB"),
                            ops.TransferResult(Path("/a/c.pdf"), True)], "boom")
    text = fv.report_text(rep)
    assert "b.txt: not a PDF/EPUB" in text
    assert "c.pdf" not in text
    assert ("Files were copied, but the tablet's interface didn't restart: boom. "
            "Restart the tablet to see them.") in text


def test_drop_on_list_uploads_to_current_folder(qtbot, monkeypatch, tmp_path):
    view, _, _ = make_view(qtbot)
    got = []
    monkeypatch.setattr(view, "upload_files", lambda paths, folder: got.append((paths, folder)))
    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(f))])
    ev = QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, mime,
                    Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    view.list.dropEvent(ev)
    assert got == [([f], "")]


def test_offline_error_does_not_call_connection_lost(qtbot):
    dev = make_device()
    conn = make_conn(dev)
    view, _, _ = make_view(qtbot, dev, conn)
    lost = []
    orig = conn.connection_lost
    conn.connection_lost = lambda e: (lost.append(e), orig(e))
    conn.device = None  # ensure() will reconnect
    conn._factory = lambda h, p: (_ for _ in ()).throw(DeviceError("down"))
    errs = []
    view.show_error = errs.append
    view.refresh()
    qtbot.waitUntil(lambda: bool(errs), timeout=5000)
    assert lost == []


def test_usb_web_enables_pdf_and_hides_banner(qtbot, monkeypatch):
    monkeypatch.setattr(StubWeb, "ok", True)
    view, _, _ = make_view(qtbot)
    assert view.can_save_pdf(view.library.items["doc-pdf"])
    assert view.banner.isHidden()


def test_banner_shown_on_usb_without_web(qtbot, monkeypatch):
    monkeypatch.setattr(StubWeb, "ok", False)
    view, _, _ = make_view(qtbot)
    assert not view.banner.isHidden()
    assert view.banner.text() == "Tip: turn on Fast USB uploads in Device to avoid the restart."


def test_trash_declined_does_nothing(qtbot, monkeypatch):
    view, _, _ = make_view(qtbot)
    calls = []
    monkeypatch.setattr(ops, "trash_many", lambda d, items: calls.append(1))
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda p, t, text, *a, **k: (asked.append(text),
                                                     QMessageBox.StandardButton.No)[1])
    view.trash_item(view.library.items["doc-nb"])
    assert asked == ["Move \u201cMeeting notes\u201d to the tablet's Trash? "
                     "You can restore it from Trash on the tablet."]
    assert calls == [] and not view._busy


def test_second_upload_rejected_while_busy(qtbot, monkeypatch, tmp_path):
    view, _, _ = make_view(qtbot)
    gate = threading.Event()
    calls = []

    def slow_upload(dev, transport, paths, folder, web=None):
        calls.append(paths)
        gate.wait(5)
        return ops.UploadReport([])

    monkeypatch.setattr(ops, "upload", slow_upload)
    monkeypatch.setattr(view, "show_report", lambda r: None)
    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF")
    view.upload_files([f], "")
    assert view._busy and view.status.text() == "Uploading 1 file…"
    assert not view.refresh_btn.isEnabled()
    view.upload_files([f], "")
    gate.set()
    qtbot.waitUntil(lambda: not view._busy, timeout=5000)
    assert len(calls) == 1
    assert view.refresh_btn.isEnabled()


def test_busy_cleared_after_error(qtbot, monkeypatch, tmp_path):
    view, _, _ = make_view(qtbot)
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        lambda *a, **k: (str(tmp_path / "p.pdf"), ""))
    monkeypatch.setattr(view, "show_error", lambda m: None)
    view.save_pdf(view.library.items["doc-pdf"])
    qtbot.waitUntil(lambda: not view._busy, timeout=5000)


def test_saved_status_message(qtbot, monkeypatch, tmp_path):
    view, _, _ = make_view(qtbot)
    dest = tmp_path / "n.pdf"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(dest), ""))
    monkeypatch.setattr(ops, "download_pdf", lambda *a, **k: None)
    view.save_pdf(view.library.items["doc-nb"])
    qtbot.waitUntil(lambda: not view._busy, timeout=5000)
    assert view.status.text() == f"Saved to {dest}"


def test_menu_uses_item_under_cursor(qtbot):
    view, _, _ = make_view(qtbot)
    view.show()
    qtbot.waitUntil(lambda: view.list.visualItemRect(view.list.item(2)).isValid())
    # highlight item 0, right-click item 2
    view.list.setCurrentRow(0)
    rect = view.list.visualItemRect(view.list.item(2))
    acts = view._menu_actions_for(rect.center())
    assert len(acts) == 3
    assert view.list.currentItem().text() == view.list.item(2).text()
    assert view._menu_actions_for(QPoint(2000, 2000)) == []


FAST_TIP = "Tip: turn on Fast USB uploads in Device to avoid the restart."


def test_fast_tip_shown_on_usb_without_web(qtbot, monkeypatch):
    monkeypatch.setattr(StubWeb, "ok", False)
    view, _, _ = make_view(qtbot)
    assert not view.banner.isHidden()
    assert view.banner.text() == FAST_TIP


def test_fast_tip_hidden_with_web(qtbot, monkeypatch):
    monkeypatch.setattr(StubWeb, "ok", True)
    view, _, _ = make_view(qtbot)
    assert view.banner.isHidden()


def test_fast_tip_hidden_over_wifi(qtbot, monkeypatch):
    monkeypatch.setattr(StubWeb, "ok", False)
    dev = make_device()
    cfg = Config(wifi_ip="192.168.1.50")

    def factory(host, password):
        if host == "10.11.99.1":
            raise OSError("no usb")
        return dev
    conn = Connection(cfg, factory=factory, secrets=NoSecrets(),
                      sleep=lambda s: None, persist=lambda c: None)
    conn.connect()
    view, _, _ = make_view(qtbot, dev, conn)
    assert conn.state == State.WIFI
    assert view.banner.isHidden()


def test_refresh_rechecks_web_availability(qtbot, monkeypatch):
    monkeypatch.setattr(StubWeb, "ok", False)
    view, _, _ = make_view(qtbot)
    assert not view.banner.isHidden()
    monkeypatch.setattr(StubWeb, "ok", True)
    view.refresh()
    qtbot.waitUntil(lambda: view.banner.isHidden(), timeout=5000)
    assert view.banner.isHidden()



# --- shared device job guard ---

BUSY = "The tablet is busy with: changing fast USB uploads. Try again when it's done."


@pytest.fixture
def no_modal(monkeypatch):
    """A modal box would block an offscreen test forever; record it instead."""
    shown = []
    for name in ("warning", "information", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: shown.append(a[1:3]))
    return shown


def test_upload_refused_while_device_job_runs(qtbot, no_modal, monkeypatch, tmp_path):
    view, conn, _ = make_view(qtbot)
    calls = []
    monkeypatch.setattr(ops, "upload", lambda *a, **k: calls.append(1))
    assert conn.try_begin_job("changing fast USB uploads")
    p = tmp_path / "a.pdf"
    p.write_bytes(b"%PDF-1.4")
    view.upload_files([p], "")
    QThreadPool.globalInstance().waitForDone(5000)
    QApplication.processEvents()
    assert calls == []
    assert view.status.text() == BUSY
    assert not view._busy
    assert conn.current_job == "changing fast USB uploads"  # not stolen or released
    conn.end_job()


def test_trash_refused_while_device_job_runs(qtbot, no_modal, monkeypatch):
    view, conn, _ = make_view(qtbot)
    calls = []
    monkeypatch.setattr(ops, "trash_many", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes)
    conn.try_begin_job("changing fast USB uploads")
    view.trash_item(view.library.items["doc-pdf"])
    QThreadPool.globalInstance().waitForDone(5000)
    QApplication.processEvents()
    assert calls == [] and view.status.text() == BUSY
    conn.end_job()


def test_upload_holds_and_releases_job(qtbot, no_modal, monkeypatch, tmp_path):
    view, conn, _ = make_view(qtbot)
    seen = []

    def fake_upload(dev, transport, paths, folder, web=None):
        seen.append(conn.current_job)
        return ops.UploadReport([])
    monkeypatch.setattr(ops, "upload", fake_upload)
    p = tmp_path / "a.pdf"
    p.write_bytes(b"%PDF-1.4")
    view.upload_files([p], "")
    qtbot.waitUntil(lambda: bool(seen) and not view._busy, timeout=5000)
    assert seen == ["uploading files"]
    assert conn.current_job is None


def test_upload_error_releases_job(qtbot, no_modal, monkeypatch, tmp_path):
    view, conn, _ = make_view(qtbot)
    errs = []
    view.show_error = errs.append

    def boom(*a, **k):
        raise RuntimeError("upload broke")
    monkeypatch.setattr(ops, "upload", boom)
    p = tmp_path / "a.pdf"
    p.write_bytes(b"%PDF-1.4")
    view.upload_files([p], "")
    qtbot.waitUntil(lambda: bool(errs), timeout=5000)
    assert conn.current_job is None


def test_job_released_even_if_view_is_gone(qtbot, no_modal, monkeypatch, tmp_path):
    import threading
    conn = make_conn(make_device())
    view = FilesView(conn, web_factory=StubWeb)  # not given to qtbot: deleted below
    view.refresh()
    qtbot.waitUntil(lambda: view.library is not None, timeout=5000)
    go = threading.Event()

    def slow_upload(*a, **k):
        go.wait(5)
        return ops.UploadReport([])
    monkeypatch.setattr(ops, "upload", slow_upload)
    p = tmp_path / "a.pdf"
    p.write_bytes(b"%PDF-1.4")
    view.upload_files([p], "")
    assert conn.current_job == "uploading files"
    import shiboken6
    shiboken6.delete(view)
    go.set()
    QThreadPool.globalInstance().waitForDone(5000)
    QApplication.processEvents()
    assert conn.current_job is None


def test_fast_toggle_in_progress_refuses_upload(qtbot, no_modal, monkeypatch, tmp_path):
    """Cross-view: while the Device view's toggle runs, a Files upload is refused."""
    import threading
    from rmdesk import webui
    from rmdesk.ui.device_view import DeviceView
    dev = make_device({webui.XOCHITL_CONF: b"[General]\nWebInterfaceEnabled=false\n"})
    view, conn, _ = make_view(qtbot, dev)

    class Web:
        def available(self):
            return True
    dview = DeviceView(conn, conn.cfg, save_config=lambda c: None, web_factory=Web,
                       backup_root=tmp_path)
    qtbot.addWidget(dview)
    dview.refresh()
    qtbot.waitUntil(lambda: dview.fast_label.text() == "Off", timeout=5000)
    started, go = threading.Event(), threading.Event()

    def slow_set(device, enabled, **kw):
        started.set()
        go.wait(5)
    monkeypatch.setattr(webui, "set_enabled", slow_set)
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes)
    calls = []
    monkeypatch.setattr(ops, "upload", lambda *a, **k: calls.append(1))
    dview.fast_btn.click()
    assert started.wait(5)
    p = tmp_path / "a.pdf"
    p.write_bytes(b"%PDF-1.4")
    view.upload_files([p], "")
    assert calls == [] and view.status.text() == BUSY
    go.set()
    qtbot.waitUntil(lambda: conn.current_job is None, timeout=5000)


# --- multi-select trash without restart, Refresh tablet ---

def _select(view, *names):
    view.list.clearSelection()
    for i in range(view.list.count()):
        li = view.list.item(i)
        if li.text() in names:
            li.setSelected(True)
    return [view.library.items[view.list.item(i).data(fv.ID_ROLE)]
            for i in range(view.list.count()) if view.list.item(i).isSelected()]


def _docs(view):
    return [view.list.item(i).text() for i in range(view.list.count())
            if view.list.item(i).data(fv.ID_ROLE) is not None
            and view.library.items[view.list.item(i).data(fv.ID_ROLE)].kind != "folder"]


def test_list_is_multi_select(qtbot):
    view, _, _ = make_view(qtbot)
    assert view.list.selectionMode() == fv.QAbstractItemView.SelectionMode.ExtendedSelection


def _pos_of(view, text):
    view.show()
    for i in range(view.list.count()):
        if view.list.item(i).text() == text:
            qtbot_wait = view.list.visualItemRect(view.list.item(i))
            return qtbot_wait.center()
    raise AssertionError(text)


def test_menu_acts_on_selection_and_disables_save(qtbot):
    view, _, _ = make_view(qtbot)
    docs = _docs(view)
    chosen = _select(view, docs[0], docs[1])
    qtbot.waitUntil(lambda: view.list.visualItemRect(view.list.item(0)).isValid(), timeout=5000)
    acts = view._menu_actions_for(_pos_of(view, docs[0]))
    assert len(acts) == 3
    assert view._targets() == chosen and len(chosen) == 2
    assert not view.act_pdf.isEnabled() and not view.act_orig.isEnabled()
    assert view.act_pdf.toolTip() == "Select one item to save"
    assert view.act_orig.toolTip() == "Select one item to save"
    assert view.act_trash.isEnabled()


def test_menu_outside_selection_selects_clicked_only(qtbot):
    view, _, _ = make_view(qtbot)
    docs = _docs(view)
    _select(view, docs[0], docs[1])
    qtbot.waitUntil(lambda: view.list.visualItemRect(view.list.item(0)).isValid(), timeout=5000)
    view._menu_actions_for(_pos_of(view, docs[2]))
    assert [i.name for i in view._targets()] == [docs[2]]
    assert view.act_orig.isEnabled()


def test_trash_many_one_confirm_removes_items_and_sets_pending(qtbot, no_modal, monkeypatch):
    view, conn, dev = make_view(qtbot)
    docs = _docs(view)
    chosen = _select(view, docs[0], docs[1])
    asked, calls, seen = [], [], []

    def fake(d, items):
        calls.append([i.id for i in items])
        seen.append(conn.current_job)
        return [ops.TransferResult(Path(i.name), True) for i in items]
    monkeypatch.setattr(ops, "trash_many", fake)
    monkeypatch.setattr(QMessageBox, "question",
                        lambda p, t, text, *a, **k: (asked.append(text),
                                                     QMessageBox.StandardButton.Yes)[1])
    view.trash_selected()
    qtbot.waitUntil(lambda: conn.pending_refresh == 2 and not view._busy, timeout=5000)
    assert asked == ["Move 2 items to the tablet's Trash? "
                     "You can restore them from Trash on the tablet."]
    assert calls == [[i.id for i in chosen]] and seen == ["moving to Trash"]
    assert conn.current_job is None
    assert not set(docs[:2]) & set(_docs(view))
    assert view.pending_note.text() == "2 change(s) will show on the tablet after it restarts."
    assert not view.pending_note.isHidden() and not view.refresh_tablet_btn.isHidden()
    assert no_modal == []


def test_trash_many_reports_failures_and_counts_only_ok(qtbot, no_modal, monkeypatch):
    view, conn, _ = make_view(qtbot)
    docs = _docs(view)
    chosen = _select(view, docs[0], docs[1])
    monkeypatch.setattr(ops, "trash_many", lambda d, items: [
        ops.TransferResult(Path(items[0].name), True),
        ops.TransferResult(Path(items[1].name), False, "boom")])
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    view.trash_selected()
    qtbot.waitUntil(lambda: bool(no_modal) and not view._busy, timeout=5000)
    assert conn.pending_refresh == 1
    assert chosen[0].name not in _docs(view) and chosen[1].name in _docs(view)
    assert f"{chosen[1].name}: boom" in no_modal[0][1]


def test_delete_key_trashes_selection(qtbot, monkeypatch):
    view, conn, _ = make_view(qtbot)
    docs = _docs(view)
    _select(view, docs[0], docs[1])
    calls = []
    monkeypatch.setattr(ops, "trash_many", lambda d, items: (
        calls.append(len(items)), [ops.TransferResult(Path(i.name), True) for i in items])[1])
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    qtbot.keyClick(view.list, Qt.Key.Key_Delete)
    qtbot.waitUntil(lambda: conn.pending_refresh == 2 and not view._busy, timeout=5000)
    assert calls == [2]


def test_pending_hidden_by_default(qtbot):
    view, _, _ = make_view(qtbot)
    assert view.pending_note.isHidden() and view.refresh_tablet_btn.isHidden()


def test_refresh_tablet_restarts_in_worker_under_job_and_clears(qtbot, no_modal, monkeypatch):
    view, conn, dev = make_view(qtbot)
    conn.add_pending(3)
    assert not view.refresh_tablet_btn.isHidden()
    asked, seen = [], []
    main = threading.get_ident()

    def fake_restart(d):
        seen.append((conn.current_job, threading.get_ident() != main))
    monkeypatch.setattr(fv.safety, "restart_xochitl", fake_restart)
    monkeypatch.setattr(QMessageBox, "question",
                        lambda p, t, text, *a, **k: (asked.append(text),
                                                     QMessageBox.StandardButton.Yes)[1])
    view.refresh_tablet_btn.click()
    qtbot.waitUntil(lambda: conn.pending_refresh == 0 and not view._busy, timeout=5000)
    assert asked == ["Restart the tablet's screen now to show the changes? "
                     "It takes a few seconds."]
    assert seen == [("refreshing the tablet", True)]
    assert conn.current_job is None
    assert view.refresh_tablet_btn.isHidden() and view.pending_note.isHidden()


def test_refresh_tablet_error_keeps_pending_and_releases_job(qtbot, no_modal, monkeypatch):
    view, conn, _ = make_view(qtbot)
    conn.add_pending(1)

    def boom(d):
        raise DeviceError("nope")
    monkeypatch.setattr(fv.safety, "restart_xochitl", boom)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    view.refresh_tablet()
    qtbot.waitUntil(lambda: bool(no_modal) and not view._busy, timeout=5000)
    assert "nope" in no_modal[0][1]
    assert conn.pending_refresh == 1 and conn.current_job is None


def test_refresh_tablet_declined_does_nothing(qtbot, monkeypatch):
    view, conn, dev = make_view(qtbot)
    conn.add_pending(1)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    view.refresh_tablet()
    assert conn.pending_refresh == 1 and not view._busy
    # only the read-only screen check made when connecting; nothing restarted
    assert [a for m, a in dev.log if "systemctl" in a] == ["systemctl is-active xochitl"]


def test_pending_cleared_after_ssh_upload_restart(qtbot, no_modal, monkeypatch, tmp_path):
    monkeypatch.setattr(StubWeb, "ok", False)
    view, conn, dev = make_view(qtbot)
    conn.add_pending(2)
    p = tmp_path / "a.pdf"
    p.write_bytes(b"%PDF-1.4")
    view.upload_files([p], "")
    qtbot.waitUntil(lambda: conn.pending_refresh == 0 and not view._busy, timeout=10000)
    assert any("systemctl restart xochitl" in a for m, a in dev.log)


def test_pending_kept_after_web_upload(qtbot, no_modal, monkeypatch, tmp_path):
    view, conn, _ = make_view(qtbot)
    conn.add_pending(2)
    monkeypatch.setattr(ops, "upload", lambda *a, **k: ops.UploadReport(
        [ops.TransferResult(tmp_path / "a.pdf", True)]))
    view.upload_files([tmp_path / "a.pdf"], "")
    qtbot.waitUntil(lambda: not view._busy and view.status.text() == "", timeout=5000)
    QApplication.processEvents()
    assert conn.pending_refresh == 2


# --- review additions ---

from tests.fakes import FailRule


def _ask_yes(monkeypatch):
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: (
        asked.append(1), QMessageBox.StandardButton.Yes)[1])
    return asked


def _meta(dev, uid):
    import json
    return json.loads(dev.files[f"{XOCHITL}/{uid}.metadata"])


def test_trash_two_real_ops_survive_refresh(qtbot, no_modal, monkeypatch):
    view, conn, dev = make_view(qtbot)
    _ask_yes(monkeypatch)
    chosen = _select(view, "Meeting notes", "Paper")
    assert len(chosen) == 2
    view.trash_selected()
    qtbot.waitUntil(lambda: conn.pending_refresh == 2 and not view._busy, timeout=5000)
    # only the read-only screen check made when connecting; nothing restarted
    assert [a for m, a in dev.log if "systemctl" in a] == ["systemctl is-active xochitl"]
    for it in chosen:
        assert _meta(dev, it.id)["parent"] == "trash"
    old = view.library
    view.refresh()
    qtbot.waitUntil(lambda: view.library is not old, timeout=5000)
    assert all(view.library.items[i.id].parent == "trash" for i in chosen)
    assert not {"Meeting notes", "Paper"} & set(_docs(view))


def test_trash_partial_failure_real_ops_stays_listed(qtbot, no_modal, monkeypatch):
    view, conn, dev = make_view(qtbot)
    _ask_yes(monkeypatch)
    chosen = _select(view, "Meeting notes", "Paper")
    bad = chosen[1]
    dev.fail_on["write_bytes"] = FailRule(DeviceError("disk"), match=f"{bad.id}.metadata")
    view.trash_selected()
    qtbot.waitUntil(lambda: bool(no_modal) and not view._busy, timeout=5000)
    assert conn.pending_refresh == 1
    assert _meta(dev, bad.id)["parent"] != "trash"
    assert _meta(dev, chosen[0].id)["parent"] == "trash"
    dev.fail_on.clear()
    old = view.library
    view.refresh()
    qtbot.waitUntil(lambda: view.library is not old, timeout=5000)
    assert bad.name in _docs(view) and chosen[0].name not in _docs(view)


def test_refresh_tablet_refused_while_job_runs(qtbot, monkeypatch):
    view, conn, _ = make_view(qtbot)
    conn.add_pending(1)
    called = []
    monkeypatch.setattr(fv.safety, "restart_xochitl", lambda d: called.append(1))
    asked = _ask_yes(monkeypatch)
    conn.try_begin_job("changing fast USB uploads")
    view.refresh_tablet()
    QThreadPool.globalInstance().waitForDone(5000)
    QApplication.processEvents()
    assert called == [] and asked == [] and view.status.text() == BUSY
    assert conn.current_job == "changing fast USB uploads" and conn.pending_refresh == 1
    conn.end_job()


def test_trash_refused_before_confirm_while_job_runs(qtbot, monkeypatch):
    view, conn, _ = make_view(qtbot)
    asked = _ask_yes(monkeypatch)
    conn.try_begin_job("changing fast USB uploads")
    view.trash_item(view.library.items["doc-pdf"])
    assert asked == [] and view.status.text() == BUSY
    conn.end_job()


def _delete_no_trash(qtbot, view, monkeypatch, target=None):
    calls = []
    monkeypatch.setattr(ops, "trash_many", lambda *a, **k: calls.append(1) or [])
    asked = _ask_yes(monkeypatch)
    qtbot.keyClick(target or view.list, Qt.Key.Key_Delete)
    QThreadPool.globalInstance().waitForDone(5000)
    QApplication.processEvents()
    assert calls == [] and asked == []


def test_delete_nothing_selected(qtbot, monkeypatch):
    view, _, _ = make_view(qtbot)
    view.list.clearSelection()
    _delete_no_trash(qtbot, view, monkeypatch)


def test_delete_only_folder_selected(qtbot, monkeypatch):
    view, _, _ = make_view(qtbot)
    for i in range(view.list.count()):
        li = view.list.item(i)
        if view.library.items[li.data(fv.ID_ROLE)].kind == "folder":
            view.list.clearSelection()
            li.setSelected(True)
            break
    else:
        raise AssertionError("no folder in list")
    _delete_no_trash(qtbot, view, monkeypatch)


def test_delete_offline(qtbot, monkeypatch):
    view, conn, _ = make_view(qtbot)
    _select(view, "Meeting notes")
    conn.connection_lost(DeviceError("gone"))
    _delete_no_trash(qtbot, view, monkeypatch)


def test_delete_while_busy(qtbot, monkeypatch):
    view, conn, _ = make_view(qtbot)
    _select(view, "Meeting notes")
    view._busy = True
    _delete_no_trash(qtbot, view, monkeypatch)
    view._busy = False


def test_delete_while_other_job_runs(qtbot, monkeypatch):
    view, conn, _ = make_view(qtbot)
    _select(view, "Meeting notes")
    conn.try_begin_job("changing fast USB uploads")
    _delete_no_trash(qtbot, view, monkeypatch)
    conn.end_job()


def test_delete_in_search_box_does_not_trash(qtbot, monkeypatch):
    view, _, _ = make_view(qtbot)
    _select(view, "Meeting notes")
    _delete_no_trash(qtbot, view, monkeypatch, target=view.search)


# --- redesign: cards, selection bar, banners, empty/offline states, layout ---

def _docs_rows(view):
    return [i for i in range(view.list.count()) if view.list.item(i).data(fv.ID_ROLE) is not None]


def test_delegate_paints_selection_ring(qtbot):
    from PySide6.QtGui import QColor, QPainter
    from PySide6.QtWidgets import QStyle, QStyleOptionViewItem
    from rmdesk.ui.theme import TOKENS
    from rmdesk.ui.widgets.doc_delegate import CARD_SIZE

    view, _, _ = make_view(qtbot)
    delegate = view.list.itemDelegate()
    idx = view.list.model().index(_docs_rows(view)[-1], 0)

    def paint(selected):
        img = QImage(CARD_SIZE, QImage.Format.Format_ARGB32)
        img.fill(QColor(TOKENS["bg"]))
        opt = QStyleOptionViewItem()
        opt.rect = img.rect()
        if selected:
            opt.state |= QStyle.StateFlag.State_Selected
        p = QPainter(img)
        delegate.paint(p, opt, idx)
        p.end()
        return img

    ring = QColor(TOKENS["accent"]).rgb()
    y = CARD_SIZE.height() // 2
    assert paint(True).pixelColor(8, y).rgb() == ring  # 2 px ring just inside the card edge
    assert paint(False).pixelColor(8, y).rgb() != ring
    assert paint(True).pixelColor(132, 25).rgb() == ring  # tick badge, top-right of the thumbnail


def test_drop_tile_is_last_and_opens_upload_dialog(qtbot, monkeypatch):
    view, _, _ = make_view(qtbot)
    tile = view.list.item(view.list.count() - 1)
    assert tile.text() == "Drop PDFs or EPUBs here" and tile.data(fv.ID_ROLE) is None
    assert not tile.flags() & Qt.ItemFlag.ItemIsSelectable
    seen = []
    monkeypatch.setattr(QFileDialog, "getOpenFileNames",
                        staticmethod(lambda *a, **k: seen.append(a[3]) or ([], "")))
    view.list.itemClicked.emit(tile)
    assert seen == ["Documents (*.pdf *.epub)"]


def test_upload_button_is_the_primary_and_uploads_chosen_files(qtbot, monkeypatch):
    view, _, _ = make_view(qtbot)
    assert view.upload_btn.property("variant") == "primary"
    assert view.refresh_btn.property("variant") != "primary"
    got = []
    monkeypatch.setattr(QFileDialog, "getOpenFileNames",
                        staticmethod(lambda *a, **k: (["/tmp/a.pdf"], "")))
    monkeypatch.setattr(view, "upload_files", lambda paths, folder: got.append((paths, folder)))
    view.upload_btn.click()
    assert got == [([Path("/tmp/a.pdf")], "")]


def test_selection_bar_shows_hides_and_calls_handlers(qtbot, monkeypatch):
    view, _, _ = make_view(qtbot)
    assert view.selbar.isHidden()
    docs = _docs(view)
    _select(view, docs[0])
    assert not view.selbar.isHidden() and view.sel_label.text() == "1 selected"
    assert view.sel_orig.isEnabled() and view.sel_trash.isEnabled()
    called = []
    monkeypatch.setattr(view, "save_original", lambda it: called.append(("orig", it.name)))
    monkeypatch.setattr(view, "trash_items", lambda items: called.append(("trash", len(items))))
    view.sel_orig.click()
    view.sel_trash.click()
    assert called == [("orig", docs[0]), ("trash", 1)]
    _select(view, docs[0], docs[1])
    assert view.sel_label.text() == "2 selected"
    assert not view.sel_pdf.isEnabled() and not view.sel_orig.isEnabled()
    assert view.sel_pdf.toolTip() == "Select one item to save"
    assert view.sel_trash.isEnabled()
    view.sel_clear.click()
    assert view.list.selectedItems() == [] and view.selbar.isHidden()


def test_selection_bar_disabled_offline(qtbot):
    view, conn, _ = make_view(qtbot)
    _select(view, "Meeting notes")
    conn.connection_lost(DeviceError("gone"))
    assert not view.sel_trash.isEnabled() and view.sel_trash.toolTip() == "Tablet not connected"


def test_banners(qtbot, monkeypatch):
    from rmdesk.ui.widgets import Banner
    monkeypatch.setattr(StubWeb, "ok", False)
    view, conn, _ = make_view(qtbot)
    assert isinstance(view.banner, Banner) and not view.banner.isHidden()
    jumps = []
    view.go_device.connect(lambda: jumps.append(1))
    view.banner.button.click()
    assert jumps == [1]
    conn.add_pending(2)
    assert not view.pending_note.isHidden() and not view.refresh_tablet_btn.isHidden()
    assert view.refresh_tablet_btn is view.pending_note.button


def test_empty_folder_state(qtbot, monkeypatch):
    view, _, _ = make_view(qtbot)
    lib = view.library
    empty = next(i for i in lib.items.values()
                 if i.kind == "folder" and not lib.children(i.id) and i.parent != "trash")
    view.tree.setCurrentIndex(view._tree_items[empty.id].index())
    assert view.pages.currentWidget() is view.empty
    assert view.empty.label.text() == "This folder is empty"
    got = []
    monkeypatch.setattr(view, "pick_upload", lambda: got.append(1))
    view.empty.action.connect(lambda: None)
    view.empty.button.click()
    view.tree.setCurrentIndex(view._tree_items[""].index())
    assert view.pages.currentWidget() is view.list


def test_offline_state_offers_go_to_device(qtbot):
    conn = make_conn(make_device(), connect=False)
    view = FilesView(conn, web_factory=StubWeb)
    qtbot.addWidget(view)
    assert view.pages.currentWidget() is view.offline
    assert view.offline.label.text() == "Tablet not connected"
    assert view.offline.button.text() == "Go to Device"
    jumps = []
    view.go_device.connect(lambda: jumps.append(1))
    view.offline.button.click()
    assert jumps == [1]


def test_narrow_window_no_clipping(qtbot):
    from PySide6.QtWidgets import QPushButton
    view, _, _ = make_view(qtbot)
    view.resize(700 - 208, 600)  # the area left of the sidebar in a 700 px window
    view.show()
    _select(view, "Meeting notes")
    qtbot.wait(50)
    assert view.minimumSizeHint().width() <= view.width()
    for w in (view.search, view.refresh_btn, view.upload_btn, view.sel_pdf, view.sel_orig,
              view.sel_trash, view.sel_clear):
        r = w.mapTo(view, w.rect().topLeft())
        assert r.x() >= 0 and r.x() + w.width() <= view.width(), w
        assert w.width() >= w.minimumSizeHint().width(), w
    assert view.upload_btn.width() >= view.upload_btn.sizeHint().width()


def test_tab_order(qtbot):
    view, conn, _ = make_view(qtbot)
    conn.add_pending(1)
    _select(view, "Meeting notes")
    view.show()
    chain, w = [], view.search
    while w not in chain:
        chain.append(w)
        w = w.nextInFocusChain()
    want = [view.search, view.refresh_btn, view.upload_btn, view.refresh_tablet_btn, view.tree,
            view.sel_pdf, view.sel_orig, view.sel_trash, view.sel_clear, view.list]
    pos = [chain.index(x) for x in want]
    assert pos == sorted(pos)
    assert all(x.focusPolicy() & Qt.FocusPolicy.TabFocus for x in want[:5] + want[5:9])


def test_folder_card_shows_no_selection_ring(qtbot):
    from PySide6.QtGui import QColor, QPainter
    from PySide6.QtWidgets import QStyle, QStyleOptionViewItem
    from rmdesk.ui.theme import TOKENS
    from rmdesk.ui.widgets.doc_delegate import CARD_SIZE
    view, _, _ = make_view(qtbot)
    row = next(i for i in _docs_rows(view)
               if view.library.items[view.list.item(i).data(fv.ID_ROLE)].kind == "folder")
    img = QImage(CARD_SIZE, QImage.Format.Format_ARGB32)
    img.fill(QColor(TOKENS["bg"]))
    opt = QStyleOptionViewItem()
    opt.rect = img.rect()
    opt.state |= QStyle.StateFlag.State_Selected
    p = QPainter(img)
    view.list.itemDelegate().paint(p, opt, view.list.model().index(row, 0))
    p.end()
    assert img.pixelColor(8, CARD_SIZE.height() // 2).rgb() != QColor(TOKENS["accent"]).rgb()


def test_drop_on_empty_state_uploads(qtbot):
    view, _, _ = make_view(qtbot)
    got = []
    view.upload_files = lambda paths, folder: got.append((paths, folder))
    view.empty.files_dropped.emit([Path("/tmp/a.pdf")])
    assert got == [([Path("/tmp/a.pdf")], "")]
    assert view.empty.acceptDrops()


def test_tree_branches_draw_no_style_highlight():
    """The folder tree paints its own chevrons; the style's selection/focus decoration in the
    indentation (blue corners on the selected row) is covered."""
    from PySide6.QtGui import QImage, QPainter, QStandardItem, QStandardItemModel
    from PySide6.QtCore import QRect, Qt
    from rmdesk.ui.files_view import DropTree
    from rmdesk.ui.theme import TOKENS
    t = DropTree()
    m = QStandardItemModel()
    a = QStandardItem("All documents")
    a.appendRow(QStandardItem("Alpha"))
    m.appendRow(a)
    t.setModel(m)
    img = QImage(40, 30, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.red)
    p = QPainter(img)
    t.drawBranches(p, QRect(0, 0, 40, 30), m.index(0, 0))
    p.end()
    assert img.pixelColor(1, 1).name() == TOKENS["surface-1"].lower()  # background covered
    leaf = QImage(40, 30, QImage.Format.Format_ARGB32)
    leaf.fill(Qt.GlobalColor.red)
    p = QPainter(leaf)
    t.drawBranches(p, QRect(0, 0, 40, 30), m.index(0, 0, m.index(0, 0)))
    p.end()
    assert leaf.pixelColor(20, 15).name() == TOKENS["surface-1"].lower()  # no chevron on a leaf
