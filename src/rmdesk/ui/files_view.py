"""Files view: folder tree, document list with thumbnails, upload/download/trash."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import shiboken6
from PySide6.QtCore import QObject, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QPainter, QPainterPath, QPen, QPixmap, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QListView, QListWidget, QListWidgetItem, QMenu, QMessageBox,
                               QPushButton, QSizePolicy, QSplitter, QStackedWidget, QTreeView, QVBoxLayout, QWidget)

from rmdesk import safety
from rmdesk.connection import BUSY_MSG, Connection, State
from rmdesk.device import DeviceError
from rmdesk.library import ops
from rmdesk.library.model import XOCHITL, Item, Library, load
from rmdesk.ui.pdf_export import write_pdf
from rmdesk.ui.theme import TOKENS, icon
from rmdesk.ui.widgets import Banner, EmptyState
from rmdesk.ui.widgets.back import back_button
from rmdesk.ui.widgets.doc_delegate import (CARD_SIZE, KIND_ROLE, PAGES_ROLE, THUMB_ROLE, TILE,
                                            DocDelegate)
from rmdesk.ui.workers import run_in_thread

OFFLINE_TIP = "Tablet not connected"
USB_TIP = "Connect over USB to export annotated PDFs"
BANNER = "Tip: turn on Fast USB uploads in Device to avoid the restart."
PENDING_NOTE = "{} change(s) will show on the tablet after it restarts."
REFRESH_CONFIRM = ("Restart the tablet's screen now to show the changes? "
                   "It takes a few seconds.")
SAVE_ONE_TIP = "Select one item to save"
RESTART_MSG = ("Files were copied, but the tablet's interface didn't restart: {}. "
               "Restart the tablet to see them.")
THUMB_CAP = 60
ID_ROLE = Qt.ItemDataRole.UserRole
TILE_TEXT = "Drop PDFs or EPUBs here"
COMPACT_WIDTH = 640  # below this the selection bar shows icons only


def report_text(report: ops.UploadReport) -> str:
    lines = [f"{r.path.name}: {r.error}" for r in report.results if not r.ok]
    if report.restart_error:
        lines.append(RESTART_MSG.format(report.restart_error))
    return "\n".join(lines)


def _urls_to_paths(mime) -> list[Path]:
    return [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile()]


class _Bridge(QObject):
    changed = Signal(object)
    pending = Signal(int)


class DropList(QListWidget):
    files_dropped = Signal(list)
    delete_pressed = Signal()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Delete:
            self.delete_pressed.emit()
            e.accept()
            return
        super().keyPressEvent(e)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, e):
        paths = _urls_to_paths(e.mimeData())
        if paths:
            e.acceptProposedAction()
            self.files_dropped.emit(paths)


class DropTree(QTreeView):
    files_dropped = Signal(list, str)

    def drawBranches(self, painter: QPainter, rect, index) -> None:
        """Only a chevron for folders with subfolders: no style-drawn highlight or focus
        corners in the indentation (they showed as blue squares on the selected row)."""
        painter.fillRect(rect, QColor(TOKENS["surface-1"]))  # cover the row panel's highlight
        if not self.model().hasChildren(index):
            return
        cell = self.indentation()
        cx = rect.right() - cell / 2 + 1
        cy = rect.center().y() + 0.5
        path = QPainterPath()
        if self.isExpanded(index):
            path.moveTo(cx - 4, cy - 2)
            path.lineTo(cx, cy + 2)
            path.lineTo(cx + 4, cy - 2)
        else:
            path.moveTo(cx - 2, cy - 4)
            path.lineTo(cx + 2, cy)
            path.lineTo(cx - 2, cy + 4)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(TOKENS["text-3"]), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawPath(path)
        painter.restore()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, e):
        paths = _urls_to_paths(e.mimeData())
        idx = self.indexAt(e.position().toPoint())
        if paths and idx.isValid():
            e.acceptProposedAction()
            self.files_dropped.emit(paths, idx.data(ID_ROLE) or "")


class _Status(QLabel):
    """Status line that takes no room while empty."""

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setVisible(bool(text))


class _DropEmpty(EmptyState):
    files_dropped = Signal(list)

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, e):
        paths = _urls_to_paths(e.mimeData())
        if paths:
            e.acceptProposedAction()
            self.files_dropped.emit(paths)


class FilesView(QWidget):
    go_device = Signal()
    go_screen = Signal()  # the back button

    def __init__(self, conn: Connection, web_factory: Callable[[], ops.UsbWeb] | None = None):
        super().__init__()
        self.setObjectName("page")
        self.conn = conn
        # ops.UsbWeb looked up when used (tests replace it)
        self._web_factory = web_factory if web_factory is not None else (lambda: ops.UsbWeb())
        self.library: Library | None = None
        self.folder = ""
        self.thumbnails: dict[str, QPixmap | None] = {}
        self._gen = 0
        self._usb_web = False
        self._busy = False
        self._tree_items: dict[str, QStandardItem] = {}

        self.back_btn = back_button()
        self.title = QLabel("Files")
        self.title.setObjectName("display")
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search")
        self.search.setMinimumWidth(120)
        self.search.setMaximumWidth(320)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setIcon(icon("refresh", 16))
        self.upload_btn = QPushButton("Upload")
        self.upload_btn.setProperty("variant", "primary")
        self.upload_btn.setIcon(icon("upload", 16, "#FFFFFF"))
        self.status = _Status("")
        self.status.setObjectName("hint")
        self.status.hide()
        self.banner = Banner("info", BANNER, "Open Device")
        self.banner.hide()
        self.pending_note = Banner("info", "", "Refresh tablet")
        self.refresh_tablet_btn = self.pending_note.button
        self.pending_note.hide()
        self.tree = DropTree()
        self.tree.setHeaderHidden(True)
        self.tree.setModel(QStandardItemModel())
        self.tree.setAcceptDrops(True)
        self.tree.setMinimumWidth(120)
        self.list = DropList()
        self.list.setObjectName("docGrid")
        self.list.setViewMode(QListView.ViewMode.IconMode)
        self.list.setResizeMode(QListView.ResizeMode.Adjust)
        self.list.setGridSize(CARD_SIZE)
        self.list.setUniformItemSizes(True)
        self.list.setItemDelegate(DocDelegate(self.list))
        self.list.setMovement(QListView.Movement.Static)
        self.list.setAcceptDrops(True)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.empty = _DropEmpty("folder", "This folder is empty", "Upload")
        self.offline = EmptyState("offline", "Tablet not connected", "Go to Device")
        self.pages = QStackedWidget()
        for w in (self.list, self.empty, self.offline):
            self.pages.addWidget(w)

        self.act_pdf = QAction("Save as PDF…", self)
        self.act_orig = QAction("Save original…", self)
        self.act_trash = QAction("Move to Trash", self)
        self.act_pdf.triggered.connect(lambda: self._with_selected(self.save_pdf))
        self.act_orig.triggered.connect(lambda: self._with_selected(self.save_original))
        self.act_trash.triggered.connect(self.trash_selected)

        # selection bar: appears when at least one document is selected
        self.selbar = QFrame()
        self.selbar.setObjectName("selBar")
        self.sel_label = QLabel("")
        self.sel_label.setObjectName("selCount")
        self.sel_pdf = self._bar_button(self.act_pdf, "download")
        self.sel_orig = self._bar_button(self.act_orig, "download")
        self.sel_trash = self._bar_button(self.act_trash, "trash")
        self.sel_trash.setProperty("variant", "danger")
        self.sel_clear = QPushButton()
        self.sel_clear.setIcon(icon("x", 16))
        self.sel_clear.setProperty("variant", "ghost")
        self.sel_clear.setProperty("dense", True)
        self.sel_clear.setToolTip("Clear selection")
        self.sel_clear.setAccessibleName("Clear selection")
        self.sel_clear.clicked.connect(self.list.clearSelection)
        bar = QHBoxLayout(self.selbar)
        bar.setContentsMargins(14, 6, 8, 6)
        bar.setSpacing(6)
        bar.addWidget(self.sel_label, 1)
        for w in (self.sel_pdf, self.sel_orig, self.sel_trash, self.sel_clear):
            bar.addWidget(w)
        self.selbar.hide()
        self._compact = False

        top = QHBoxLayout()
        top.setSpacing(10)
        top.addWidget(self.back_btn)
        top.addWidget(self.title)
        top.addStretch(1)
        top.addWidget(self.search, 2)
        top.addWidget(self.refresh_btn)
        top.addWidget(self.upload_btn)
        right = QWidget()
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(0, 0, 0, 0)
        rlay.setSpacing(10)
        rlay.addWidget(self.selbar)
        rlay.addWidget(self.pages, 1)
        split = QSplitter()
        split.addWidget(self.tree)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([190, 600])
        split.setChildrenCollapsible(False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 12)
        lay.setSpacing(14)
        lay.addLayout(top)
        lay.addWidget(self.pending_note)
        lay.addWidget(self.banner)
        lay.addWidget(self.status)
        lay.addWidget(split, 1)

        order = [self.back_btn, self.search, self.refresh_btn, self.upload_btn, self.refresh_tablet_btn,
                 self.banner.button, self.tree, self.sel_pdf, self.sel_orig, self.sel_trash,
                 self.sel_clear, self.list]
        for a, b in zip(order, order[1:]):
            QWidget.setTabOrder(a, b)

        self.back_btn.clicked.connect(self.go_screen.emit)
        self.search.textChanged.connect(lambda _t: self._populate())
        self.refresh_btn.clicked.connect(self.refresh)
        self.refresh_tablet_btn.clicked.connect(self.refresh_tablet)
        self.upload_btn.clicked.connect(self.pick_upload)
        self.empty.action.connect(self.pick_upload)
        self.empty.files_dropped.connect(lambda paths: self.upload_files(paths, self.folder))
        self.offline.action.connect(self.go_device.emit)
        self.banner.action.connect(self.go_device.emit)
        self.list.itemClicked.connect(self._tile_hit)
        self.list.itemActivated.connect(self._tile_hit)
        self.list.itemSelectionChanged.connect(self._update_selection)
        self.list.delete_pressed.connect(self.trash_selected)
        self.tree.selectionModel().currentChanged.connect(self._on_folder)
        self.tree.files_dropped.connect(self.upload_files)
        self.list.files_dropped.connect(lambda paths: self.upload_files(paths, self.folder))
        self.list.customContextMenuRequested.connect(self._menu)
        self.list.itemDoubleClicked.connect(self._open_folder)

        self._bridge = _Bridge()
        self._bridge.changed.connect(self._on_state)
        conn.on_state.append(self._bridge.changed.emit)
        self._bridge.pending.connect(lambda _n: self._update_pending())
        conn.on_pending.append(self._bridge.pending.emit)
        self._update_actions()
        self._update_pending()

    def _bar_button(self, act: QAction, icon_name: str) -> QPushButton:
        btn = QPushButton(act.text())
        btn.setIcon(icon(icon_name, 16))
        btn.setProperty("variant", "ghost")
        btn.setProperty("dense", True)
        btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)  # never elide
        btn.clicked.connect(act.trigger)
        return btn

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        compact = self.width() < COMPACT_WIDTH
        if compact != self._compact:
            self._compact = compact
            self._sync_bar()

    def _sync_bar(self) -> None:
        """Mirror the actions' enabled state and tooltips onto the selection bar buttons."""
        for btn, act in ((self.sel_pdf, self.act_pdf), (self.sel_orig, self.act_orig),
                         (self.sel_trash, self.act_trash)):
            btn.setEnabled(act.isEnabled())
            btn.setText("" if self._compact else act.text())
            btn.setToolTip(act.toolTip() or (act.text().rstrip("…") if self._compact else ""))
            btn.setAccessibleName(act.text())

    def _update_selection(self) -> None:
        targets = self._targets()
        self.selbar.setVisible(bool(targets))
        if not targets:
            return
        self.sel_label.setText(f"{len(targets)} selected")
        self._apply_rules(targets)
        self._sync_bar()

    def _apply_rules(self, targets: list[Item]) -> None:
        idle = self._online() and not self._busy
        offline = "" if self._online() else OFFLINE_TIP
        if len(targets) == 1:
            item = targets[0]
            self.act_pdf.setEnabled(idle and self.can_save_pdf(item))
            self.act_pdf.setToolTip(self.pdf_tooltip(item))
            self.act_orig.setEnabled(idle)
            self.act_orig.setToolTip(offline)
        else:
            for a in (self.act_pdf, self.act_orig):
                a.setEnabled(False)
                a.setToolTip(SAVE_ONE_TIP)
        self.act_trash.setEnabled(idle)
        self.act_trash.setToolTip(offline)

    def _update_page(self) -> None:
        real = [i for i in range(self.list.count()) if self.list.item(i).data(ID_ROLE) is not None]
        if self.library is None and not self._online():
            self.pages.setCurrentWidget(self.offline)
        elif self.library is not None and not real:
            searching = bool(self.search.text().strip())
            self.empty.label.setText("No documents match your search" if searching
                                     else "This folder is empty")
            self.empty.button.setVisible(not searching)
            self.pages.setCurrentWidget(self.empty)
        else:
            self.pages.setCurrentWidget(self.list)

    def _tile_hit(self, li: QListWidgetItem) -> None:
        if li.data(KIND_ROLE) == TILE and self._online() and not self._busy:
            self.pick_upload()

    def pick_upload(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Upload to tablet", "",
                                                "Documents (*.pdf *.epub)")
        if paths:
            self.upload_files([Path(p) for p in paths], self.folder)

    # -- worker helper: ensure() errors pass through; only operation errors drop the link --
    def _on_device(self, op: Callable, done: Callable, err: Callable | None = None,
                   release: Callable[[], None] | None = None) -> None:
        """`release` runs on the GUI thread when the work ends, even if the view is gone."""
        def work():
            dev = self.conn.ensure()
            try:
                return op(dev, self.conn.transport)
            except DeviceError as e:
                self.conn.connection_lost(e)
                raise
        err = err or self._fail

        def alive(fn):
            # a late result must not touch a view that has been destroyed
            def call(r):
                if release is not None:
                    release()
                if shiboken6.isValid(self):
                    fn(r)
            return call

        try:
            run_in_thread(work, alive(done), alive(err))
        except BaseException:
            if release is not None:
                release()
            raise

    def _job(self, label: str, op: Callable, done: Callable, ok_status: str = "",
             device_job: str | None = None,
             after_fail: Callable[[Exception], None] | None = None) -> bool:
        """Run one exclusive device job; returns False (and says so) if one is running.

        `device_job` names a job that restarts xochitl; it also claims the
        connection-wide job slot so no other view stops/restarts xochitl meanwhile.
        """
        if self._busy:
            self.status.setText("Busy: wait for the current operation to finish")
            return False
        if device_job is not None and not self.conn.try_begin_job(device_job):
            self.status.setText(BUSY_MSG.format(self.conn.current_job))
            return False
        self._busy = True
        self.status.setText(label)
        self._update_actions()

        def finished(res):
            self._busy = False
            self.status.setText(ok_status)
            self._update_actions()
            done(res)

        def failed(e):
            self._busy = False
            self.status.setText("")
            self._update_actions()
            self._fail(e)
            if after_fail is not None:
                after_fail(e)

        self._on_device(op, finished, failed,
                        release=self.conn.end_job if device_job is not None else None)
        return True

    def _fail(self, e: Exception) -> None:
        self.show_error(str(e))

    def show_error(self, msg: str) -> None:
        QMessageBox.warning(self, "rmdesk", msg)

    def show_report(self, report: ops.UploadReport, title: str = "Upload problems") -> None:
        text = report_text(report)
        if text:
            QMessageBox.warning(self, title, text)

    # -- state --
    def _on_state(self, state: State) -> None:
        self._update_actions()
        if state in (State.USB, State.WIFI):
            self.refresh()

    def _online(self) -> bool:
        return self.conn.state != State.OFFLINE

    def can_save_pdf(self, item: Item) -> bool:
        return item.kind == "notebook" or (self.conn.transport == "usb" and self._usb_web)

    def pdf_tooltip(self, item: Item) -> str:
        if not self._online():
            return OFFLINE_TIP
        return "" if self.can_save_pdf(item) else USB_TIP

    def _update_actions(self) -> None:
        online = self._online()
        idle = online and not self._busy
        self.refresh_btn.setEnabled(idle)
        self.upload_btn.setEnabled(idle)
        self.upload_btn.setToolTip("" if online else OFFLINE_TIP)
        self.empty.button.setEnabled(idle)
        self.refresh_btn.setToolTip("" if online else OFFLINE_TIP)
        self.list.setAcceptDrops(idle)
        self.empty.setAcceptDrops(idle)
        self.refresh_tablet_btn.setEnabled(idle)
        self.refresh_tablet_btn.setToolTip("" if online else OFFLINE_TIP)
        self.tree.setAcceptDrops(idle)
        # on USB without the web interface, uploads go over SSH and restart xochitl
        no_web = (self.conn.state == State.USB and not self._usb_web
                  and self.library is not None)
        self.banner.setVisible(no_web)
        self._update_page()
        self._update_selection()

    def _update_pending(self) -> None:
        n = self.conn.pending_refresh
        self.pending_note.set_text(PENDING_NOTE.format(n) if n else "")
        self.pending_note.setVisible(n > 0)
        self.refresh_tablet_btn.setVisible(n > 0)

    # -- loading --
    def refresh(self) -> None:
        self._gen += 1
        gen = self._gen

        def op(dev, transport):
            lib = load(dev)
            web_ok = False
            if transport == "usb":
                web_ok = self._web_factory().available()
            return lib, web_ok

        def done(res):
            if gen != self._gen:
                return
            self.library, self._usb_web = res
            self.thumbnails = {}
            self._build_tree()
            self._populate()
            self._update_actions()

        self._on_device(op, done)

    def _build_tree(self) -> None:
        model: QStandardItemModel = self.tree.model()  # type: ignore[assignment]
        model.clear()
        self._tree_items = {}
        root = QStandardItem("All documents")
        root.setData("", ID_ROLE)
        model.appendRow(root)
        self._tree_items[""] = root
        lib = self.library
        assert lib is not None
        pending = [("", root)]
        while pending:
            fid, parent = pending.pop()
            for it in lib.children(fid):
                if it.kind == "folder":
                    node = QStandardItem(it.name)
                    node.setData(it.id, ID_ROLE)
                    parent.appendRow(node)
                    self._tree_items[it.id] = node
                    pending.append((it.id, node))
        if self.folder not in self._tree_items:
            self.folder = ""
        self.tree.expandAll()
        self.tree.setCurrentIndex(self._tree_items[self.folder].index())

    def _on_folder(self, index, _prev=None) -> None:
        if index.isValid():
            self.folder = index.data(ID_ROLE) or ""
            self._populate()

    def _open_folder(self, li: QListWidgetItem) -> None:
        it = self.library.items.get(li.data(ID_ROLE)) if self.library else None
        if it is not None and it.kind == "folder":
            self.search.clear()
            self.tree.setCurrentIndex(self._tree_items[it.id].index())

    def _populate(self) -> None:
        if self.library is None:
            return
        text = self.search.text().strip()
        items = self.library.search(text) if text else self.library.children(self.folder)
        self.list.clear()
        for it in items:
            li = QListWidgetItem(it.name)
            li.setData(ID_ROLE, it.id)
            li.setData(KIND_ROLE, it.kind)
            li.setData(PAGES_ROLE, 0 if it.kind == "folder" else len(it.page_ids))
            li.setData(THUMB_ROLE, self.thumbnails.get(it.id))
            self.list.addItem(li)
        if items and not text:
            tile = QListWidgetItem(TILE_TEXT)
            tile.setData(KIND_ROLE, TILE)
            tile.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.list.addItem(tile)
        self._load_thumbnails([i for i in items if i.kind != "folder"][:THUMB_CAP])
        self._update_page()
        self._update_selection()

    def _load_thumbnails(self, items: list[Item]) -> None:
        todo = [i for i in items if i.id not in self.thumbnails and i.page_ids]
        if not todo:
            return
        gen = self._gen

        def op(dev, _transport):
            out: dict[str, bytes | None] = {}
            for it in todo:
                path = f"{XOCHITL}/{it.id}.thumbnails/{it.page_ids[0]}.png"
                try:
                    out[it.id] = dev.read_bytes(path) if dev.exists(path) else None
                except DeviceError:
                    out[it.id] = None
            return out

        def done(res):
            if gen != self._gen:
                return
            for iid, data in res.items():
                pm = QPixmap()
                self.thumbnails[iid] = pm if data and pm.loadFromData(data) else None
            for i in range(self.list.count()):
                li = self.list.item(i)
                pm = self.thumbnails.get(li.data(ID_ROLE))
                if pm is not None:
                    li.setData(THUMB_ROLE, pm)

        self._on_device(op, done, lambda e: None)

    # -- actions --
    def _targets(self) -> list[Item]:
        """The selected documents (folders are not actionable), in list order."""
        if self.library is None:
            return []
        out = []
        for i in range(self.list.count()):
            li = self.list.item(i)
            if li.isSelected():
                it = self.library.items.get(li.data(ID_ROLE))
                if it is not None and it.kind != "folder":
                    out.append(it)
        return out

    def _with_selected(self, fn: Callable[[Item], None]) -> None:
        targets = self._targets()
        if len(targets) == 1:
            fn(targets[0])

    def _menu_actions_for(self, pos) -> list[QAction]:
        """Settle the selection for a right-click at `pos`; return the actions to offer.

        Clicking inside the selection keeps it; clicking elsewhere selects just that item.
        """
        li = self.list.itemAt(pos)
        if li is None or self.library is None:
            return []
        if li.isSelected():
            self.list.selectionModel().setCurrentIndex(
                self.list.indexFromItem(li), self.list.selectionModel().SelectionFlag.NoUpdate)
        else:
            self.list.clearSelection()
            self.list.setCurrentItem(li)
            li.setSelected(True)
        targets = self._targets()
        if not targets:
            return []
        self._apply_rules(targets)
        self._sync_bar()
        return [self.act_pdf, self.act_orig, self.act_trash]

    def _menu(self, pos) -> None:
        actions = self._menu_actions_for(pos)
        if not actions:
            return
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        for a in actions:
            menu.addAction(a)
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def _ask_dest(self, name: str) -> Path | None:
        start = str(Path.home() / "Downloads" / name)
        dest, _ = QFileDialog.getSaveFileName(self, "Save as", start)
        return Path(dest) if dest else None

    def save_pdf(self, item: Item) -> None:
        dest = self._ask_dest(f"{item.name}.pdf")
        if dest is None:
            return

        def op(dev, transport):
            web = self._web_factory() if transport == "usb" else None
            ops.download_pdf(dev, transport or "wifi", item, dest, web=web, writer=write_pdf)

        self._job("Saving PDF…", op, lambda _r: None, f"Saved to {dest}")

    def save_original(self, item: Item) -> None:
        ext = "epub" if item.kind == "epub" else "pdf"
        dest = self._ask_dest(f"{item.name}.{ext}")
        if dest is None:
            return
        self._job("Saving original…", lambda dev, _t: ops.download_original(dev, item, dest),
                  lambda _r: None, f"Saved to {dest}")

    def trash_selected(self) -> None:
        if self._online() and not self._busy:
            self.trash_items(self._targets())

    def trash_item(self, item: Item) -> None:
        self.trash_items([item])

    def _claim_check(self) -> bool:
        """False (saying why) if a job is running; checked before asking to confirm."""
        if self._busy:
            self.status.setText("Busy: wait for the current operation to finish")
            return False
        if self.conn.current_job is not None:
            self.status.setText(BUSY_MSG.format(self.conn.current_job))
            return False
        return True

    def trash_items(self, items: list[Item]) -> None:
        if not items:
            return
        if len(items) == 1:
            text = (f"Move \u201c{items[0].name}\u201d to the tablet's Trash? "
                    "You can restore it from Trash on the tablet.")
        else:
            text = (f"Move {len(items)} items to the tablet's Trash? "
                    "You can restore them from Trash on the tablet.")
        if not self._claim_check():
            return
        answer = QMessageBox.question(self, "Move to Trash", text)
        if answer != QMessageBox.StandardButton.Yes:
            return

        def done(results):
            failed = [r for r in results if not r.ok]
            moved = 0
            for it, r in zip(items, results):
                if r.ok:
                    it.parent = "trash"  # same as a fresh load of the edited metadata
                    moved += 1
            self._populate()
            self.conn.add_pending(moved)
            if failed:
                self.show_report(ops.UploadReport(failed), "Move to Trash problems")

        self._job("Moving to Trash…", lambda dev, _t: ops.trash_many(dev, items), done,
                  device_job="moving to Trash")

    def refresh_tablet(self) -> None:
        if not self._claim_check():
            return
        answer = QMessageBox.question(self, "Refresh tablet", REFRESH_CONFIRM)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._job("Restarting the tablet's screen…",
                  lambda dev, _t: safety.restart_xochitl(dev),
                  lambda _r: self.conn.clear_pending(), device_job="refreshing the tablet")

    def upload_files(self, paths: list[Path], folder_id: str,
                     on_done: Callable[[ops.UploadReport | None], None] | None = None) -> bool:
        """Upload to `folder_id` ("" = My files) in the job slot: the USB web interface (Fast
        USB uploads) when it is on, else SSH and a restart. False (saying why in the status
        line) when a job runs. `on_done` gets the report, or None when the upload failed;
        problems are reported in a dialog either way."""
        def op(dev, transport):
            web = self._web_factory() if transport == "usb" else None
            return ops.upload(dev, transport or "wifi", paths, folder_id, web=web)

        def done(report):
            if report.restarted:
                self.conn.clear_pending()
            self.show_report(report)
            self.refresh()
            if on_done is not None:
                on_done(report)

        n = len(paths)
        return self._job(f"Uploading {n} file{'s' if n != 1 else ''}…", op, done,
                         device_job="uploading files",
                         after_fail=(lambda _e: on_done(None)) if on_done is not None else None)

    def upload_dropped(self, paths: list[Path],
                       on_done: Callable[[ops.UploadReport | None], None]) -> str | None:
        """Files dropped elsewhere (the Screen view): upload to My files like Upload does.
        None when it started, else why not (the busy text)."""
        if self.upload_files(paths, "", on_done=on_done):
            return None
        return self.status.text() or BUSY_MSG.format(self.conn.current_job)
