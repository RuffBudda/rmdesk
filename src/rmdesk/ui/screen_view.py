"""Screen view (live mirror of the tablet) and the Present window (crop / invert / pen dot).

One StreamClient serves both: the stream runs while the Screen view or the Present window is
visible (whatever the pointer; the placed items are drawn only in the Screen view, never in
Present). Frames arrive on the client thread and reach the GUI through a queued signal; only the
newest frame is kept, so a slow GUI drops intermediate frames instead of queueing them.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

import shiboken6
import numpy as np
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QImage, QPainter, QPen, QTransform
from PySide6.QtWidgets import QProgressBar, QPushButton, QVBoxLayout, QWidget

from rmdesk import config, firmware, safety
from rmdesk.config import Config
from rmdesk.connection import BUSY_MSG as JOB_BUSY_MSG, USB_HOST, Connection, State
from rmdesk.device import DeviceError
from rmdesk.library.ops import UPLOAD_SUFFIXES
from rmdesk.stream import install as gms
from rmdesk.stream.catchup import DELAY as CATCHUP_DELAY, CatchUp
from rmdesk.stream.client import Frame, StreamClient, StreamError
from rmdesk.stream.events import PenClient
from rmdesk.ui.compose.shade_job import AUTO as AUTO_STYLE
from rmdesk.ui.live_compose import IMAGE_SUFFIXES, LiveCompose
from rmdesk.ui import remote_control as rc
from rmdesk.ui.theme import icon
from rmdesk.ui.theme import TOKENS
from rmdesk.ui.widgets import Chip, DevicePill, Segmented, repolish
from rmdesk.ui.widgets.battery import BatteryBadge, battery_hint
from rmdesk.ui.widgets.cluster import BETWEEN, Cluster
from rmdesk.ui.widgets.drop import DropOutline
from rmdesk.ui.widgets.flow import FlowLayout
from rmdesk.ui.widgets.hint import HoverHint
from rmdesk.ui.widgets.overlay import OverlayPin
from rmdesk.ui.workers import run_in_thread

PRESENT_TITLE = "rmdesk — Present"
WAITING_MSG = "Waiting for the tablet…"
OFFLINE_MSG = "Tablet not connected."
NOT_INSTALLED_MSG = "Install screen streaming in Device."
NOT_RUNNING_MSG = "Screen streaming is installed but not running — reinstall it in Device."
UNSUPPORTED_MSG = "Screen streaming needs tablet firmware 3.24 or newer."
log = logging.getLogger(__name__)
AUTH_MSG = "Screen streaming login failed — reinstall screen streaming in Device."
BUSY_MSG = ("The tablet's stream is busy: another viewer is connected, or a connection is "
            "left over from switching between USB and Wi-Fi. Close other viewers or press "
            "Restart stream.")
RESTART_JOB = "restarting screen streaming"
EXIT_FULL_HIDE_MS = 2500  # the fullscreen exit button hides after this long without mouse moves
RESTARTING_MSG = "Restarting the tablet's stream…"
RESTART_FAILED_MSG = "Could not restart the tablet's stream: {}"
# the Refresh button: restart the stream service (a resync can't fix a picture it latched onto
# the wrong memory for, a band of noise), then wait for its first frame
REFRESH_JOB = "refreshing the screen picture"
REFRESH_TIMEOUT = 15.0  # seconds from the press to the fresh frame
REFRESH_FAILED_MSG = "Couldn't refresh the picture; try again."
REFRESH_TIP = "Refresh: restart the tablet's screen stream and fetch a fresh picture"
REFRESHING_TIP = "Refreshing…"
REFRESH_BAR_H = 3  # px: the progress line along the top of the picture
KEEP_TIP = ("Fetches a fresh picture every second — use while typing on the keyboard or "
            "presenting. Uses more tablet battery.")
KEEP_INTERVAL = 1.0  # seconds between "Keep refreshing" resyncs
PRESENT_KEYS = [("R", "Refresh"), ("K", "Keep refreshing"), ("I", "Invert"), ("D", "Pen dot"),
                ("F", "Fullscreen"), ("Esc", "Reset crop")]
POINTERS = ["control", "place"]
PLACE_TIP = ("Place: put pictures, ink text and shapes on the page open on the tablet; "
             "nothing reaches the tablet until Send to tablet")
ZOOM_RESET_TIP = "Back to 1x: taps the tablet's zoom label (Ctrl+0)"
# the in-window hint line (widgets.hint): short, always shown on hover or keyboard focus
HINTS = {
    "control": "Control: the mouse and keys drive the tablet",
    "place": "Place: add pictures, ink text and shapes to the open page",
    "zoom": "1x: reset the tablet's zoom",
    "refresh": "Refresh the picture",
    "rotate": "Rotate the view",
    "shot": "Save a screenshot",
    "expand": "Expand: hide everything but the picture (F11)",
    "present": "Present in a window",
    "undo": "Undo last send",
    "send": "Send the placed items to the page open on the tablet",
    "exit": "Show the controls again (Esc)",
    "files": "Files on the tablet",
}
CONN_STATE = {State.USB: "usb", State.WIFI: "wifi", State.OFFLINE: "offline",
              State.CONNECTING: "connecting"}
CONN_TEXT = {State.USB: "USB", State.WIFI: "Wi-Fi", State.OFFLINE: "Offline",
             State.CONNECTING: "Connecting…"}


def conn_hint(label: str) -> str:
    """The Connection button's hint: "Connection and settings: Wi-Fi"."""
    return f"Connection and settings: {label}"
PLACE_HINTS = {"add_image_btn": "Add picture", "add_ink_btn": "Add ink text",
               "add_rect_btn": "Rectangle", "add_line_btn": "Line", "add_arrow_btn": "Arrow",
               "add_note_btn": "Sticky note", "remove_btn": "Remove selected"}
# files dragged onto the view: PDFs/EPUBs upload to My files, pictures are placed (Place)
DROP_UPLOAD = "Upload to tablet"
DROP_PLACE = "Place picture here"
DROP_BOTH = "Upload to tablet and place pictures"
DROP_OFFLINE = "Connect the tablet first."
DROP_UNSUPPORTED = "Drop PDFs, EPUBs or pictures."
DROP_PLACE_BUSY = "Wait until the send finishes, then drop the picture again."
UPLOADING_MSG = "Uploading {}…"
UPLOADED_MSG = "Uploaded {}"
UPLOAD_PARTLY_MSG = "Uploaded {ok} of {n} files"
UPLOAD_FAILED_MSG = "Upload failed"
DROP_MSG_MS = 5000  # a drop's result or refusal stays this long over the picture
DROP_STAGGER = 48.0  # frame px between several pictures dropped at once
HEADER_PAD = (20, 16, 20, 12)  # the view's margins outside Expand
BATTERY_INSET = 8  # px: the Expand battery indicator's inset from the picture's corner


def keep_hint(on: bool) -> str:
    return f"Keep refreshing: {'on' if on else 'off'}"


def present_tip() -> str:
    """The Present button's tooltip, with the keys the Present window takes."""
    keys = "\n".join(f"{k}  {what}" for k, what in PRESENT_KEYS)
    return ("Present: the picture in its own window for screen sharing (no rmdesk "
            f"controls, no placed items). Drag to crop. In Present:\n{keys}")


def drop_paths(mime) -> list[Path]:
    """The local files in a drag's data."""
    if mime is None or not mime.hasUrls():
        return []
    return [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile()]


def sort_drop(paths: list[Path]) -> tuple[list[Path], list[Path], list[Path]]:
    """(documents to upload, pictures to place, the rest)."""
    docs, pics, other = [], [], []
    for p in paths:
        ext = p.suffix.lower()
        (docs if ext in UPLOAD_SUFFIXES else pics if ext in IMAGE_SUFFIXES else other).append(p)
    return docs, pics, other


def _names(paths: list[Path]) -> str:
    return paths[0].name if len(paths) == 1 else f"{len(paths)} files"


def _icon_btn(name: str, tip: str, label: str) -> QPushButton:
    """An icon-only ghost button (36 px square); `label` is its accessible name."""
    b = QPushButton()
    b.setIcon(icon(name, 16))
    b.setProperty("variant", "ghost")
    b.setProperty("iconOnly", True)
    b.setFixedSize(36, 36)
    b.setToolTip(tip)
    b.setAccessibleName(label)
    return b
CONTROL_MSG_MS = 8000  # a "Control is off" reason stays this long
PEN_STALE_MS = 1500  # the tablet sends no pen events once the pen leaves hover range
MIN_DRAG = 8  # pixels; smaller drags are clicks, not crops


def rotated_size(size: QSize, rotation: int) -> QSize:
    return QSize(size.height(), size.width()) if rotation in (90, 270) else QSize(size)


class Geometry:
    """Where a frame region lands in a view: fit to the view (aspect kept), rotated clockwise.

    `region` is in frame pixels (the whole frame, or a crop). Rotation is applied to the region.
    """

    def __init__(self, view: QRectF, region: QRect, rotation: int):
        self.region, self.rotation = QRect(region), rotation % 360
        rs = rotated_size(region.size(), self.rotation)
        rw, rh = max(rs.width(), 1), max(rs.height(), 1)
        self.scale = min(view.width() / rw, view.height() / rh)
        w, h = rw * self.scale, rh * self.scale
        self.target = QRectF(view.x() + (view.width() - w) / 2,
                             view.y() + (view.height() - h) / 2, w, h)

    def to_view(self, p: QPointF) -> QPointF:
        w, h = self.region.width(), self.region.height()
        x, y = p.x() - self.region.x(), p.y() - self.region.y()
        u, v = {0: (x, y), 90: (h - y, x), 180: (w - x, h - y), 270: (y, w - x)}[self.rotation]
        return QPointF(self.target.x() + u * self.scale, self.target.y() + v * self.scale)

    def from_view(self, p: QPointF) -> QPointF:
        w, h = self.region.width(), self.region.height()
        u, v = (p.x() - self.target.x()) / self.scale, (p.y() - self.target.y()) / self.scale
        x, y = {0: (u, v), 90: (v, h - u), 180: (w - u, h - v), 270: (w - v, u)}[self.rotation]
        return QPointF(x + self.region.x(), y + self.region.y())


def crop_rect(view_rect: QRectF, frame_size: QSize, drag: QRectF, rotation: int = 0,
              region: QRect | None = None) -> QRect:
    """Map a drag box in the window to FRAME pixels.

    `region` is what the window currently shows (default: the whole frame); a new crop is
    chosen inside it, so successive drags zoom further in. `rotation` is the display rotation.
    The result is clamped to `region`.
    """
    full = QRect(0, 0, frame_size.width(), frame_size.height())
    region = full if region is None else region.intersected(full)
    g = Geometry(view_rect, region, rotation)
    a, b = g.from_view(drag.topLeft()), g.from_view(drag.bottomRight())
    box = QRectF(a, b).normalized()
    left, top = round(box.left()), round(box.top())
    out = QRect(left, top, round(box.right()) - left, round(box.bottom()) - top)
    return out.intersected(region)


class _Problem(Exception):
    """Streaming can't start for a reason the user fixes in the Device view."""


class _Runner:
    """Keeps a start()/stop() resource matching a target, one blocking call at a time, off the
    GUI thread. Calls only from the GUI thread; callbacks run there too."""

    def __init__(self, start: Callable[[object], object], stop: Callable[[object], None],
                 on_failed: Callable[[object, Exception], None], alive: Callable[[], bool]):
        self._start, self._stop, self._on_failed, self._alive = start, stop, on_failed, alive
        self.target: object | None = None
        self.running: object | None = None
        self.handle: object | None = None
        self.failed: object | None = None
        self.busy = False

    def set_target(self, target: object | None, retry: bool = False) -> None:
        self.target = target
        if retry:
            self.failed = None
        self.pump()

    def pump(self) -> None:
        if self.busy or not self._alive():
            return
        if self.running is not None and self.running != self.target:
            self.busy, handle = True, self.handle
            run_in_thread(lambda: self._stop(handle), lambda _r: self._stopped(),
                          lambda _e: self._stopped())
        elif self.running is None and self.target is not None and self.target != self.failed:
            self.busy, t = True, self.target
            run_in_thread(lambda: self._start(t), lambda h: self._started(t, h),
                          lambda e: self._start_failed(t, e))

    def _stopped(self) -> None:
        self.busy, self.running, self.handle = False, None, None
        self.pump()

    def _started(self, t, handle) -> None:
        self.busy, self.running, self.handle = False, t, handle
        self.pump()

    def _start_failed(self, t, e: Exception) -> None:
        self.busy, self.failed = False, t
        if self._alive():
            self._on_failed(t, e)
        self.pump()


class _Bridge(QObject):
    """Client threads emit these; connected to ScreenView slots they arrive queued."""
    frame = Signal(object)
    error = Signal(object)
    pen = Signal(float, float, bool)
    state = Signal(object)


class FrameCanvas(QWidget):
    """Draws the source's latest frame fit-to-widget (the Present window; the Screen view shows
    LiveComposeView). Subclasses add crop/invert/pen dot."""

    def __init__(self, source: "ScreenView", bg: str = "#202020"):
        super().__init__()
        self.source = source
        self.bg = QColor(bg)
        self.invert = False
        self.crop: QRect | None = None  # frame pixels
        self.show_pen = False
        self._drag: QRectF | None = None
        self._arr: np.ndarray | None = None
        self._key: tuple | None = None
        self._img: QImage | None = None
        source.frame_changed.connect(self.update)
        source.redraw.connect(self.update)

    @property
    def frame(self) -> Frame | None:
        return self.source.frame

    def _image(self) -> QImage | None:
        f = self.frame
        if f is None:
            return None
        key = (f, self.invert)  # holds the Frame: compared with `is`, never a reusable id()
        if self._key is None or self._key[0] is not f or self._key[1] != self.invert:
            arr = np.ascontiguousarray(255 - f.gray if self.invert else f.gray)
            self._arr = arr  # QImage doesn't own its buffer
            self._img = QImage(arr.data, f.width, f.height, arr.strides[0],
                               QImage.Format.Format_Grayscale8)
            self._key = key
        return self._img

    def region(self) -> QRect:
        f = self.frame
        full = QRect(0, 0, f.width, f.height)
        return full if self.crop is None else self.crop.intersected(full)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), self.bg)
        img = self._image()
        if img is None:
            return
        region = self.region()
        g = Geometry(QRectF(self.rect()), region, self.source.rotation)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.translate(g.target.center())
        p.rotate(g.rotation)
        w, h = region.width() * g.scale, region.height() * g.scale
        p.drawImage(QRectF(-w / 2, -h / 2, w, h), img, QRectF(region))
        p.resetTransform()
        pen = self.source.pen
        if self.show_pen and pen is not None and region.contains(round(pen[0]), round(pen[1])):
            c = g.to_view(QPointF(*pen))
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 0, 0))
            p.drawEllipse(c, 7, 7)
        if self._drag is not None:
            p.setPen(QPen(QColor(255, 0, 0), 2, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(self._drag)


class PresentWindow(FrameCanvas):
    """Frameless window for screen sharing. Drag = crop, Esc = reset, I = invert, D = pen dot,
    F = fullscreen, R = refresh (resync the stream), K = keep refreshing on/off. Crop is stored in frame pixels, so it stays on the same part of the page
    when the rotation changes."""

    def __init__(self, source: "ScreenView"):
        super().__init__(source, bg="#000000")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle(PRESENT_TITLE)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.resize(540, 720)
        self._visible = False
        self._press: QPointF | None = None

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self._visible = True
        self.setFocus()
        self.source._present_visibility_changed()

    def hideEvent(self, e) -> None:
        super().hideEvent(e)
        self._visible = False
        self.source._present_visibility_changed()

    def keyPressEvent(self, e) -> None:
        k = e.key()
        mods = Qt.KeyboardModifier
        plain = not e.isAutoRepeat() and not (
            e.modifiers() & (mods.ControlModifier | mods.AltModifier | mods.MetaModifier))
        if k in (Qt.Key.Key_R, Qt.Key.Key_K) and not plain:
            super().keyPressEvent(e)  # held key or a shortcut chord: not a refresh toggle
        elif k == Qt.Key.Key_Escape:
            self.crop = None
            self.update()
        elif k == Qt.Key.Key_I:
            self.invert = not self.invert
            self.update()
        elif k == Qt.Key.Key_D:
            self.show_pen = not self.show_pen
            self.source._present_visibility_changed()
            self.update()
        elif k == Qt.Key.Key_R:
            self.source.refresh()
        elif k == Qt.Key.Key_K:
            self.source.set_keep_refreshing(not self.source.keep_refreshing)
        elif k == Qt.Key.Key_F:
            if self.isFullScreen():
                self.showNormal()
            else:
                self.showFullScreen()
        else:
            super().keyPressEvent(e)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._press = e.position()
            self._drag = QRectF(self._press, self._press)

    def mouseMoveEvent(self, e) -> None:
        if self._press is not None:
            self._drag = QRectF(self._press, e.position()).normalized()
            self.update()

    def mouseReleaseEvent(self, e) -> None:
        if self._press is None or e.button() != Qt.MouseButton.LeftButton:
            return
        drag = QRectF(self._press, e.position()).normalized()
        self._press, self._drag = None, None
        f = self.frame
        if f is not None and drag.width() >= MIN_DRAG and drag.height() >= MIN_DRAG:
            r = crop_rect(QRectF(self.rect()), QSize(f.width, f.height), drag,
                          self.source.rotation, self.region())
            if r.width() > 0 and r.height() > 0:
                self.crop = r
        self.update()


_DRAG_EVENTS = {
    QEvent.Type.DragEnter: lambda v, e, w: v._drag_enter(e, w),
    QEvent.Type.DragMove: lambda v, e, w: v._drag_move(e, w),
    QEvent.Type.DragLeave: lambda v, e, w: v._drag_leave(e, w),
    QEvent.Type.Drop: lambda v, e, w: v._drop(e, w),
}


class ScreenView(QWidget):
    frame_changed = Signal()
    redraw = Signal()
    open_device = Signal()  # the Connection button, Open Device: the Device page (settings)
    open_files = Signal()  # the Files button
    fullscreen_changed = Signal(bool)  # expanded: the window hides its status bar

    def __init__(self, conn: Connection, *, client_factory=StreamClient, pen_factory=PenClient,
                 status_fn=gms.status, restart_fn=gms.restart,
                 local_only_fn=gms.ensure_local_only, keep_default: bool = True,
                 control_default: bool = True, secrets: gms.SecretStore | None = None,
                 pictures_dir: Path | None = None, catchup_delay: float = CATCHUP_DELAY,
                 keep_interval: float = KEEP_INTERVAL, cfg: Config | None = None,
                 save_config: Callable[[Config], None] = config.save,
                 backup_root: Path = safety.BACKUP_ROOT, input_writer_factory=None,
                 refresh_timeout: float = REFRESH_TIMEOUT,
                 upload_fn: Callable[[list[Path], Callable], str | None] | None = None):
        super().__init__()
        self.setObjectName("page")
        self.conn = conn
        self._client_factory, self._pen_factory = client_factory, pen_factory
        self._status_fn, self._secrets = status_fn, secrets
        self._restart_fn = restart_fn
        self._local_only_fn = local_only_fn
        self._restarting = False
        # the Refresh button's run: None, "restart" (the service restarts) or "frame" (resynced,
        # waiting for the first new frame); _refresh_token tells a late callback it is stale
        self._refresh_stage: str | None = None
        self._refresh_token = 0
        self.fullscreen = False
        self._pictures_dir = pictures_dir
        self.frame: Frame | None = None
        self._cfg, self._save_config = cfg, save_config
        rot = cfg.screen_rotation if cfg is not None else 0
        self.rotation = rot if rot in (0, 90, 180, 270) else 0
        self.pen: tuple[float, float] | None = None
        self.present: PresentWindow | None = None
        self._shown = False
        self._problem: str | None = None
        self._error: str | None = None
        self._have_frame = False
        self._lock = threading.Lock()
        self._latest: Frame | None = None
        self._pending = False
        # dropped PDFs/EPUBs: upload_fn(paths, on_done) starts the Files view's upload (None,
        # or why it can't); MainWindow sets it to FilesView.upload_dropped
        self.upload_fn = upload_fn
        self._uploading = 0  # drops whose upload is running

        # the pointer: what a click on the picture does (Control / Place)
        self.pointer_seg = Segmented(["Control", "Place"])
        self.pointer_seg.setAccessibleName("Pointer")
        self.pointer_seg.layout().setContentsMargins(0, 0, 0, 0)  # the cluster is the track
        self.pointer_seg.layout().setSpacing(4)
        self.control_btn, self.place_btn = self.pointer_seg.buttons
        for b, name, label, tip in ((self.control_btn, "pointer", "Control",
                                     f"Control: {rc.CONTROL_TIP}"),
                                    (self.place_btn, "move", "Place", PLACE_TIP)):
            b.setIcon(icon(name, 16))
            b.setToolTip(tip)
            b.setAccessibleName(label)
            b.setAccessibleDescription(tip)
            b.setFixedHeight(36)
        self.refresh_btn = _icon_btn("refresh", REFRESH_TIP, "Refresh")
        self.keep_btn = _icon_btn("keep", f"Keep refreshing: {KEEP_TIP}", "Keep refreshing")
        self.keep_btn.setCheckable(True)
        # 1x (Control only): taps the tablet's own zoom label; Ctrl+0 does the same
        self.zoom_reset_btn = QPushButton("1x")
        self.zoom_reset_btn.setProperty("variant", "ghost")
        self.zoom_reset_btn.setProperty("compact", True)
        self.zoom_reset_btn.setFixedHeight(36)
        self.zoom_reset_btn.setToolTip(ZOOM_RESET_TIP)
        self.zoom_reset_btn.setAccessibleName("Back to 1x")
        self.zoom_reset_btn.setEnabled(False)  # until Control is on
        self.rotate_btn = _icon_btn("rotate", "Rotate the picture a quarter turn", "Rotate")
        self.shot_btn = _icon_btn("camera", "Screenshot: save the picture to your Pictures folder",
                                  "Screenshot")
        # Expand is the main button (Present was dropped: Expand covers it)
        self.full_btn = QPushButton("Expand")
        self.full_btn.setIcon(icon("maximize", 16, "#FFFFFF"))
        self.full_btn.setProperty("variant", "primary")
        self.full_btn.setProperty("compact", True)
        self.full_btn.setFixedHeight(36)
        self.full_btn.setToolTip("Expand: hide the controls so the picture fills the "
                                 "window (F11; Esc or F11 to go back)")
        self.full_btn.setAccessibleName("Expand")
        self.present_btn = QPushButton("Present")
        self.present_btn.setProperty("variant", "primary")
        self.present_btn.setProperty("compact", True)
        self.present_btn.setFixedHeight(36)
        self.present_btn.setToolTip(present_tip())
        self.present_btn.setAccessibleName("Present")
        self.pointer = "control"
        # Control turns itself on when it can (on by default); without control_default it
        # waits until Control is chosen (tests start with it off this way)
        self._auto_on = bool(control_default)
        # status: a chip over the picture (and a button when it is a Device problem)
        self.status = Chip("neutral", "")
        self.status.setWordWrap(True)
        self.status.setMaximumHeight(16777215)
        self.status.setMinimumHeight(24)
        self.status.hide()
        self.install_btn = QPushButton("Open Device")
        self.install_btn.setProperty("dense", True)
        self.install_btn.hide()
        self.restart_btn = QPushButton("Restart stream")
        self.restart_btn.setProperty("dense", True)
        self.restart_btn.setToolTip("Restart the stream service on the tablet (your notes and "
                                    "the tablet's screen are not touched)")
        self.restart_btn.hide()
        # a drop's progress or result ("Uploading a.pdf…", "Uploaded a.pdf", a refusal)
        self.drop_chip = Chip("neutral", "")
        self.drop_chip.setWordWrap(True)
        self.drop_chip.setMaximumHeight(16777215)
        self.drop_chip.setMinimumHeight(24)
        self.drop_chip.hide()
        self._drop_timer = QTimer(self)
        self._drop_timer.setSingleShot(True)
        self._drop_timer.setInterval(DROP_MSG_MS)
        self._drop_timer.timeout.connect(self._clear_drop_message)
        self.overlay = QWidget(self)
        self.overlay.setObjectName("overlay")
        ov = QVBoxLayout(self.overlay)
        ov.setContentsMargins(0, 0, 0, 0)
        ov.setSpacing(8)
        ov.addWidget(self.drop_chip, 0, Qt.AlignmentFlag.AlignLeft)
        ov.addWidget(self.status, 0, Qt.AlignmentFlag.AlignLeft)
        ov.addWidget(self.install_btn, 0, Qt.AlignmentFlag.AlignLeft)
        ov.addWidget(self.restart_btn, 0, Qt.AlignmentFlag.AlignLeft)
        # the one picture: the live frame with the placed items on top (every pointer)
        self.compose = LiveCompose(self, conn, cfg if cfg is not None else Config(),
                                   save_config, backup_root)
        self.canvas = self.compose.view
        # an indeterminate line along the top of the picture while Refresh runs
        self.refresh_bar = QProgressBar(self)
        self.refresh_bar.setObjectName("refreshBar")
        self.refresh_bar.setRange(0, 0)
        self.refresh_bar.setTextVisible(False)
        self.refresh_bar.setFixedHeight(REFRESH_BAR_H)
        self.refresh_bar.setAccessibleName(REFRESHING_TIP)
        self.refresh_bar.hide()
        self._compose_working = False
        self._refresh_on = False
        self.compose.busy_changed.connect(self._compose_busy)  # send/Undo: the same bar
        self.canvas.installEventFilter(self)
        # files dragged anywhere on the view (the picture's viewport takes drags itself)
        self.setAcceptDrops(True)
        self.canvas.viewport().installEventFilter(self)
        self.drop_outline = DropOutline(self)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(round(refresh_timeout * 1000))
        self._refresh_timer.timeout.connect(self._refresh_timed_out)
        self.send_btn, self.undo_btn = self.compose.send_btn, self.compose.undo_btn
        # the header: clusters of related buttons (8 px apart inside, 16 px between), wrapping
        # as units in a narrow window; the send cluster sits at the right end of its row
        self.header = QWidget()
        self.header.setAccessibleName("Screen tools")
        self.pointer_cluster = Cluster(self.pointer_seg, self.zoom_reset_btn, name="Pointer")
        self.picture_cluster = Cluster(self.refresh_btn, self.keep_btn, self.rotate_btn,
                                       name="Picture")
        self.present_btn.hide()  # kept for the Present window code, not shown
        self.output_cluster = Cluster(self.shot_btn, self.full_btn,
                                      name="Output")
        self.send_cluster = Cluster(self.undo_btn, self.send_btn, name="Send")
        # the way to the other pages: Files, and the connection state (opens Device)
        self.files_btn = _icon_btn("files", "Files on the tablet (Ctrl+1)", "Files")
        self.conn_btn = DevicePill(CONN_STATE[conn.state])
        self.conn_btn.set_state(CONN_STATE[conn.state], CONN_TEXT[conn.state])
        self.nav_cluster = Cluster(self.files_btn, self.conn_btn, name="Tablet")
        # the tablet's battery at the right end, next to Undo (shown even with nothing to send)
        self.battery = None
        self.battery_badge = BatteryBadge()
        self.battery_badge.setFixedHeight(self.picture_cluster.sizeHint().height())  # centred
        top = FlowLayout(self.header, spacing=BETWEEN, vspacing=8)
        for w in (self.pointer_cluster, self.picture_cluster, self.output_cluster):
            top.addWidget(w)
        top.add_right(self.nav_cluster)
        top.add_right(self.battery_badge)
        top.add_right(self.send_cluster)
        lay = QVBoxLayout(self)
        self._lay = lay
        lay.setContentsMargins(*HEADER_PAD)
        lay.setSpacing(12)
        lay.addWidget(self.header)
        lay.addWidget(self.compose, 1)
        self._pin = OverlayPin(self.overlay, self)
        self._pin.set_host(self.canvas)
        self.overlay.raise_()
        # while Control is on: a chip over the picture (top right), plus the folio note
        self.control_chip = Chip("warn", rc.CONTROL_CHIP)
        self.typing_chip = Chip("neutral", rc.NO_FOLIO_MSG)
        for chip in (self.control_chip, self.typing_chip):
            chip.setWordWrap(True)  # OverlayPin sizes the overlay by height-for-width
            chip.setMaximumHeight(16777215)
            chip.setMinimumHeight(24)
        # a short fixed note: never wrapped (padding 10 px a side, see theme)
        self.typing_chip.setMinimumWidth(
            self.typing_chip.fontMetrics().horizontalAdvance(rc.NO_FOLIO_MSG) + 28)
        self.control_overlay = QWidget(self)
        co = QVBoxLayout(self.control_overlay)
        co.setContentsMargins(0, 0, 0, 0)
        co.setSpacing(6)
        co.addWidget(self.control_chip, 0, Qt.AlignmentFlag.AlignRight)
        co.addWidget(self.typing_chip, 0, Qt.AlignmentFlag.AlignRight)
        self.control_overlay.hide()
        self._control_pin = OverlayPin(self.control_overlay, self, corner="tr")
        self._control_pin.set_host(self.canvas)
        # Expand hides the header: a small battery indicator in the picture's top-right corner
        self.battery_overlay = BatteryBadge(overlay=True, parent=self)
        self._battery_pin = OverlayPin(self.battery_overlay, self, corner="tr",
                                       margin=BATTERY_INSET)
        self._battery_pin.set_host(self.canvas)
        self._control_msg_timer = QTimer(self)
        self._control_msg_timer.setSingleShot(True)
        self._control_msg_timer.setInterval(CONTROL_MSG_MS)
        self._control_msg_timer.timeout.connect(self._clear_control_message)
        # keys and focus on the view; real mouse events arrive on its viewport (same pixels)
        self.control = rc.RemoteControl(conn, self.canvas, self._canvas_geometry,
                                        writer_factory=input_writer_factory,
                                        mouse_widget=self.canvas.viewport())
        self.control.changed.connect(self._sync_control)
        self.control.paste_rich = self.paste_from_clipboard  # Ctrl+V with a picture: place it
        self.canvas.paste_handler = lambda: self.paste_from_clipboard(text_too=True)
        self.control._job.connect(self._auto_control)  # back on when a tablet job ends
        # expanded: a small button to come back, shown while the mouse moves
        self.exit_full_btn = QPushButton("Show controls", self)
        self.exit_full_btn.setIcon(icon("minimize", 16))
        self.exit_full_btn.setProperty("dense", True)
        self.exit_full_btn.setToolTip("Bring back the controls (Esc or F11)")
        self.exit_full_btn.hide()
        self.exit_full_btn.clicked.connect(lambda: self.set_fullscreen(False))
        self._exit_timer = QTimer(self)
        self._exit_timer.setSingleShot(True)
        self._exit_timer.setInterval(EXIT_FULL_HIDE_MS)
        self._exit_timer.timeout.connect(self.exit_full_btn.hide)
        c = self.compose
        chain = [self.control_btn, self.place_btn, self.zoom_reset_btn,
                 self.refresh_btn, self.keep_btn, self.rotate_btn, self.shot_btn, self.full_btn,
                 self.present_btn, self.files_btn, self.conn_btn, self.undo_btn, self.send_btn,
                 c.add_image_btn, c.add_ink_btn,
                 *c.shape_btns.values(), c.remove_btn, c.tool_combo, c.color_combo,
                 c.thick_spin, c.spacing_spin, c.contrast_spin, c.cross_check, c.invert_check,
                 c.fill_check, c.double_check, self.canvas, self.install_btn, self.restart_btn]
        for a_, b_ in zip(chain, chain[1:]):
            QWidget.setTabOrder(a_, b_)

        self.refresh_btn.clicked.connect(self.refresh_picture)
        self.keep_btn.toggled.connect(self.set_keep_refreshing)
        self.zoom_reset_btn.clicked.connect(self.control.zoom_reset)
        self.rotate_btn.clicked.connect(self.rotate)
        self.shot_btn.clicked.connect(lambda: self.screenshot())
        self.present_btn.clicked.connect(self.show_present)
        self.full_btn.clicked.connect(lambda: self.set_fullscreen(True))
        self.install_btn.clicked.connect(self.open_device.emit)
        self.files_btn.clicked.connect(self.open_files.emit)
        self.conn_btn.clicked.connect(self.open_device.emit)
        self.restart_btn.clicked.connect(self.restart_stream)
        self.pointer_seg.changed.connect(lambda i: self.set_pointer(POINTERS[i]))
        self.compose.actions_changed.connect(self._update_send_area)
        # the in-window hint line for the header and the Place row (tooltips stay too)
        self.hint = HoverHint(self)
        for w, key in ((self.control_btn, "control"), (self.place_btn, "place"),
                       (self.zoom_reset_btn, "zoom"),
                       (self.rotate_btn, "rotate"), (self.shot_btn, "shot"),
                       (self.full_btn, "expand"), (self.present_btn, "present"),
                       (self.undo_btn, "undo"), (self.send_btn, "send"),
                       (self.exit_full_btn, "exit"), (self.files_btn, "files")):
            self.hint.watch(w, HINTS[key])
        self.hint.watch(self.conn_btn, lambda: conn_hint(self.conn_btn.label.text()))
        self.hint.watch(self.keep_btn, lambda: keep_hint(self.keep_refreshing))
        self.hint.watch(self.battery_badge, lambda: battery_hint(self.battery))
        self.hint.watch(self.refresh_btn,
                        lambda: REFRESHING_TIP if self.refreshing else HINTS["refresh"])
        self.refresh_btn.setAccessibleDescription(HINTS["refresh"])
        for attr, text in PLACE_HINTS.items():
            self.hint.watch(getattr(c, attr), text)
        self.pointer_seg.set_index(POINTERS.index(self.pointer))
        self._apply_pointer()

        self._bridge = _Bridge()
        self._bridge.frame.connect(self._take_frame)
        self._bridge.error.connect(self._on_error)
        self._bridge.pen.connect(self._on_pen)
        self._bridge.state.connect(self._on_state)
        bridge = self._bridge

        def on_state(state):
            if shiboken6.isValid(bridge):
                bridge.state.emit(state)
        conn.on_state.append(on_state)

        alive = lambda: shiboken6.isValid(self)  # noqa: E731
        self._stream = _Runner(self._start_stream, self._stop_client, self._start_failed, alive)
        self._pen_run = _Runner(self._start_pen, self._stop_client, lambda t, e: None, alive)
        self._pen_timer = QTimer(self)
        self._pen_timer.setSingleShot(True)
        self._pen_timer.timeout.connect(self._clear_pen)
        # goMarkableStream stops sending soon after input ends; resync once per burst so a
        # late tablet redraw still shows up (rmdesk.stream.catchup)
        self._catchup_timer = QTimer(self)
        self._catchup_timer.setSingleShot(True)
        self._catchup = CatchUp(self._catchup_resync,
                                lambda s: self._catchup_timer.start(round(s * 1000)),
                                self._catchup_timer.stop, catchup_delay)
        self._catchup_timer.timeout.connect(self._catchup.fire)
        self._catchup_host: str | None = None
        # "Keep refreshing" (keyboard typing sends no frames at all): resync every interval;
        # _keep_pending is the client whose resync hasn't produced a frame yet (no pile-up)
        self.keep_refreshing = False
        self._keep_pending: object | None = None
        self._keep_timer = QTimer(self)
        self._keep_timer.setInterval(round(keep_interval * 1000))
        self._keep_timer.timeout.connect(self._keep_tick)
        self.set_keep_refreshing(keep_default)  # on by default (tablet typing sends no frames)
        self._update_status()

    # -- visibility drives the stream --
    def showEvent(self, e) -> None:
        super().showEvent(e)
        self._shown = True
        self._retarget(retry=True)
        self._auto_control()

    def hideEvent(self, e) -> None:
        super().hideEvent(e)
        self._shown = False
        self.control.stop()  # leaving the view or a hidden window: let go of the tablet
        self._retarget()

    def _present_visibility_changed(self) -> None:
        self._retarget(retry=bool(self.present and self.present._visible))

    def _host(self) -> str | None:
        if self.conn.state in (State.OFFLINE, State.CONNECTING):
            return None
        return USB_HOST if self.conn.transport == "usb" else (self.conn.cfg.wifi_ip or None)

    def _retarget(self, retry: bool = False) -> None:
        if retry:
            self._problem = None
        p = self.present
        wanted = self._shown or bool(p and p._visible)
        host = self._host() if wanted else None
        self._stream.set_target(host, retry)
        if host != self._catchup_host:  # stopped or moved: a pending resync is moot
            self._catchup_host = host
            self._catchup.reset()
        pen_wanted = bool(p and p._visible and p.show_pen)
        self._pen_run.set_target(host if pen_wanted else None, retry)
        if not pen_wanted:
            self._clear_pen()
        self._update_status()

    def _on_state(self, state) -> None:
        self.control.on_state(state)
        self._problem = None
        self._retarget(retry=True)
        self.compose.on_state(state)
        self._auto_control()

    # -- the pointer: Control / Place --
    def set_pointer(self, name: str) -> None:
        """What the mouse and keys on the picture do. Control: they drive the tablet
        (RemoteControl). Place: they edit the placed items, nothing goes to the tablet. The
        stream keeps running and the items stay drawn whatever the pointer."""
        if name not in POINTERS:
            raise ValueError(f"unknown pointer {name!r}")
        self.pointer = name
        if name == "control":
            self._auto_on = True  # chosen: on whenever it can be from now on
        else:
            self.control.stop()  # only Control ever sends input
        if self.pointer_seg.index() != POINTERS.index(name):
            self.pointer_seg.set_index(POINTERS.index(name))
        self._apply_pointer()
        if name == "control" and self._shown and not self.control.active:
            self.control.start()  # the user's choice: says why when it can't
        self._sync_control()

    @property
    def control_enabled(self) -> bool:
        """Control is on whenever it can be (the default), so rmdesk may also send its own
        touch: Send swipes a typed-only page to its top to measure its scroll
        (ui.compose.scroll_probe), with Control's touch or one of its own in Place."""
        return self._auto_on

    @property
    def compose_mode(self) -> bool:
        """The Place pointer (the items are being edited)."""
        return self.pointer == "place"

    def set_compose_mode(self, on: bool) -> None:
        """Place on, or back to Control (the default pointer)."""
        self.set_pointer("place" if on else "control")

    def _apply_pointer(self) -> None:
        place = self.pointer == "place"
        if place and self.fullscreen:  # Expand is for watching: Place needs its tools
            self.set_fullscreen(False)
        self.compose.set_place(place)
        if place:
            self.canvas.setFocusPolicy(Qt.FocusPolicy.StrongFocus)  # typing into ink text
        elif not self.control.active:
            self.canvas.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # Control's start() sets it
            if self.canvas.hasFocus():
                self.canvas.clearFocus()
        self.full_btn.setVisible(not place)
        self.zoom_reset_btn.setVisible(not place)
        # one primary button per screen: Expand in Control (hidden in Place), Send in Place
        self._update_send_area()
        self._settle_header()
        self.canvas.viewport().update()

    def _settle_header(self) -> None:
        """Buttons came or went: size the clusters now, so the header's row count (and the
        picture's size) never passes through a stale, wider layout."""
        for cl in (self.pointer_cluster, self.picture_cluster, self.output_cluster,
                   self.nav_cluster, self.send_cluster):
            cl.layout().invalidate()
            cl.layout().activate()
        self.header.layout().invalidate()
        self.header.updateGeometry()

    def _update_send_area(self) -> None:
        """Send to tablet (with its count) shows in Place and, with any pointer, whenever there
        are items; Undo last send next to it (and whenever there is a send to undo)."""
        c = self.compose
        place = self.pointer == "place"
        items = bool(c.placed_items())
        send, undo = place or items, place or items or c.has_undo()
        self.send_btn.setVisible(send)
        self.undo_btn.setVisible(undo)
        self.send_cluster.setVisible(send or undo)
        want = "primary" if place else "secondary"
        if (self.send_btn.property("variant") or "") != want:
            self.send_btn.setProperty("variant", want)
            self.send_btn.setIcon(icon("send", 16, "#FFFFFF" if place else None))
            repolish(self.send_btn)

    # -- worker-side start/stop --
    def _credentials(self) -> tuple[str, str]:
        info = self.conn.info
        serial = (info.serial if info else "") or self.conn.cfg.serial
        creds = gms.stream_credentials(serial, self._secrets) if serial else None
        if creds is None:
            raise _Problem(NOT_INSTALLED_MSG)
        return creds

    def _start_stream(self, host: str):
        info, dev = self.conn.info, self.conn.device
        if info is not None and not gms.stream_supported(info.version):
            raise _Problem(UNSUPPORTED_MSG)
        if dev is None:
            raise _Problem(OFFLINE_MSG)
        # a stream installed by rmdesk 0.1.0 listens on every interface: move it to the
        # tablet's loopback (it restarts; the client's retries cover that)
        self._local_only_fn(dev)
        st = self._status_fn(dev)
        if st == "missing":
            raise _Problem(NOT_INSTALLED_MSG)
        if st != "running":
            raise _Problem(NOT_RUNNING_MSG)
        user, password = self._credentials()
        client = self._client_factory(host, password, self._on_frame_thread, username=user,
                                      dial=self._dial, on_error=self._on_error_thread)
        client.start()
        return client

    def _start_pen(self, host: str):
        user, password = self._credentials()
        client = self._pen_factory(host, password, self._on_pen_thread, username=user,
                                   dial=self._dial)
        client.start()
        return client

    def _dial(self, timeout: float):
        """A socket to goMarkableStream through the current SSH connection (the service
        listens on the tablet's loopback only). Called on the client threads."""
        dev = self.conn.device
        if dev is None:
            raise StreamError("the tablet is offline")
        return dev.open_tunnel(gms.PORT, timeout=timeout)

    @staticmethod
    def _stop_client(client) -> None:
        client.stop()

    def _start_failed(self, _target, e: Exception) -> None:
        self._problem = str(e) if isinstance(e, _Problem) else f"Could not start screen streaming: {e}"
        self._update_status()

    # -- callbacks from client threads --
    def _emit(self, signal, *args) -> None:
        try:
            signal.emit(*args)
        except RuntimeError:  # the view was deleted while a client was winding down
            pass

    def _on_frame_thread(self, frame: Frame) -> None:
        with self._lock:
            self._latest = frame
            if self._pending:
                return  # a delivery is already queued; it will pick up this newer frame
            self._pending = True
        self._emit(self._bridge.frame, frame)

    def _on_error_thread(self, e: Exception) -> None:
        self._emit(self._bridge.error, e)

    def _on_pen_thread(self, x: float, y: float, visible: bool) -> None:
        self._emit(self._bridge.pen, x, y, visible)

    # -- GUI-thread slots --
    @Slot(object)
    def _take_frame(self, _frame) -> None:
        with self._lock:
            frame, self._latest, self._pending = self._latest, None, False
        if frame is None:
            return
        self.frame = frame
        self._have_frame = True
        self._error = None
        self._keep_pending = None
        if self._refresh_stage == "frame":
            self._end_refresh()  # the restarted service's first frame
        if self._streaming() is not None:
            self._catchup.frame()
        self._update_status()
        self.frame_changed.emit()

    @Slot(object)
    def _on_error(self, e: Exception) -> None:
        text = str(e)
        status = getattr(e, "status", None)
        if status == 401:
            self._error = AUTH_MSG
        elif status == 429:
            self._error = BUSY_MSG
        else:
            self._error = f"Screen stream: {text or type(e).__name__}"
        self._update_status()

    @Slot(float, float, bool)
    def _on_pen(self, x: float, y: float, visible: bool) -> None:
        self.pen = (x, y) if visible else None
        if visible:
            self._pen_timer.start(PEN_STALE_MS)
        self.redraw.emit()

    def _clear_pen(self) -> None:
        if self.pen is not None:
            self.pen = None
            self.redraw.emit()

    def _update_status(self) -> None:
        if self._problem:
            text = self._problem
        elif self._error:
            text = self._error
        elif self._host() is None and (self._shown or (self.present and self.present._visible)):
            text = OFFLINE_MSG
        elif not self._have_frame and self._stream.target is not None:
            text = WAITING_MSG
        else:
            text = ""
        self.status.setText(text)
        problem = bool(self._problem or self._error)
        self.status.set_state("warn" if text == BUSY_MSG else "err" if problem
                              else "neutral")
        self.status.setVisible(bool(text))
        self.install_btn.setVisible(bool(self._problem))
        self.restart_btn.setVisible(text == BUSY_MSG)
        self.restart_btn.setEnabled(not self._restarting)
        self.overlay.setVisible(bool(text) or bool(self.drop_chip.text()))
        self._pin.update()

    def _say(self, text: str) -> None:
        """A one-off message (screenshot result) in the status chip over the picture."""
        self.status.set_state("neutral", text)
        self.status.show()
        self.overlay.show()
        self._pin.update()

    def shutdown(self) -> None:
        """App quit: answer a pending send confirm with No, close Present and stop the clients
        (blocks at most about a second each)."""
        self.control.shutdown()
        self.compose.shutdown()
        if self.present is not None and shiboken6.isValid(self.present):
            self.present.close()
        for r in (self._stream, self._pen_run):
            r.target = None
            if r.handle is not None:
                try:
                    r.handle.stop()
                except Exception:
                    pass
                r.handle, r.running = None, None

    # -- actions --
    def _streaming(self):
        """The running stream client, if the stream is meant to be running."""
        r = self._stream
        if r.target is None or r.running != r.target:
            return None
        return r.handle

    def _catchup_resync(self) -> None:
        client = self._streaming()
        if client is not None:
            client.resync()
        else:
            self._catchup.reset()

    def set_fullscreen(self, on: bool) -> None:
        """Expand (Control only): hides the header; the window reacts to fullscreen_changed by
        hiding its sidebar and status bar. The window keeps its size. Control keeps working:
        the picture gets the keys (Esc, reserved by RemoteControl, comes back up to
        keyPressEvent here), and entering or leaving never turns Control off."""
        on = bool(on)
        if on and self.pointer == "place":
            return  # Place needs its tools
        if on == self.fullscreen:
            return
        self.fullscreen = on
        self.hint.clear()
        self.header.setVisible(not on)
        self._lay.setContentsMargins(*((0, 0, 0, 0) if on else HEADER_PAD))
        for w in (self, self.canvas, self.compose):
            w.setMouseTracking(on)
        if on:
            self._install_move_filter()
            if self.focusPolicy() == Qt.FocusPolicy.NoFocus:
                self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
            self._focus_picture()  # Esc or F11 leaves (no on-screen button: nothing in the way)
        else:
            self._exit_timer.stop()
            self.exit_full_btn.hide()
            if self.control.active:
                self.canvas.setFocus(Qt.FocusReason.OtherFocusReason)  # keys stay on the tablet
        self._sync_battery_overlay()
        self.fullscreen_changed.emit(on)

    def set_battery(self, b) -> None:
        """The tablet's battery (a rmdesk.battery.Battery, None when unknown): the header
        badge, and the small indicator over the picture in Expand."""
        self.battery = b
        self.battery_badge.set_battery(b)
        self.battery_overlay.set_battery(b)
        self.hint.refresh()
        self._sync_battery_overlay()

    def _sync_battery_overlay(self) -> None:
        show = self.fullscreen and self.battery is not None
        self.battery_overlay.setVisible(show)
        if show:
            self._battery_pin.update()
        # the Control chips (same corner) move under it
        self._control_pin.extra_top = (self.battery_overlay.sizeHint().height() + 6) if show else 0
        self._control_pin.update()

    def _focus_picture(self) -> None:
        """Expanded: the keys go to the picture while Control is on (the focus outline shows),
        else to this view, so Esc still reaches keyPressEvent."""
        if self.control.active:
            self.canvas.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self.setFocus(Qt.FocusReason.OtherFocusReason)

    def _install_move_filter(self) -> None:
        for w in [self, self.canvas, self.compose, *self.compose.findChildren(QWidget)]:
            if not w.property("_fullMoveFilter"):
                w.installEventFilter(self)
                w.setProperty("_fullMoveFilter", True)
            w.setMouseTracking(True)

    def _show_exit_button(self) -> None:
        b = self.exit_full_btn
        b.adjustSize()
        y = 12  # under the Control chips and the battery when they show (same corner)
        if self.control_overlay.isVisible():
            y = self.control_overlay.geometry().bottom() + 9
        elif self.battery_overlay.isVisible():
            y = self.battery_overlay.geometry().bottom() + 9
        b.move(self.width() - b.width() - 12, y)
        b.show()
        b.raise_()
        self._exit_timer.start()

    def eventFilter(self, obj, e) -> bool:
        if obj is self.canvas and e.type() in (QEvent.Type.Resize, QEvent.Type.Move,
                                               QEvent.Type.Show):
            self._place_refresh_bar()
        elif obj is self.canvas.viewport() and e.type() in _DRAG_EVENTS:
            _DRAG_EVENTS[e.type()](self, e, obj)
            return True  # the scene never sees file drags
        return super().eventFilter(obj, e)

    # -- files dropped on the view --
    def dragEnterEvent(self, e) -> None:
        self._drag_enter(e, self)

    def dragMoveEvent(self, e) -> None:
        self._drag_move(e, self)

    def dragLeaveEvent(self, e) -> None:
        self._drag_leave(e, self)

    def dropEvent(self, e) -> None:
        self._drop(e, self)

    def _online(self) -> bool:
        return self.conn.state in (State.USB, State.WIFI)

    def drop_label(self, paths: list[Path]) -> tuple[str, bool]:
        """What a drop of `paths` would do, and whether it is refused."""
        docs, pics, _other = sort_drop(paths)
        if not docs and not pics:
            return DROP_UNSUPPORTED, True
        if not self._online():
            return DROP_OFFLINE, True
        if docs and pics:
            return DROP_BOTH, False
        return (DROP_UPLOAD if docs else DROP_PLACE), False

    def _drag_enter(self, e, _w) -> None:
        paths = drop_paths(e.mimeData())
        if not paths:
            e.ignore()
            return
        e.acceptProposedAction()
        text, refused = self.drop_label(paths)
        c = self.canvas
        area = c.page_rect().toAlignedRect().intersected(c.viewport().rect())
        if area.isEmpty():
            area = c.viewport().rect()
        top_left = c.viewport().mapTo(self, area.topLeft())
        self.drop_outline.show_for(QRect(top_left, area.size()), text, refused)

    def _drag_move(self, e, _w) -> None:
        if drop_paths(e.mimeData()):
            e.acceptProposedAction()
        else:
            e.ignore()

    def _drag_leave(self, e, _w) -> None:
        self.drop_outline.hide()
        e.accept()

    def _drop(self, e, w) -> None:
        self.drop_outline.hide()
        paths = drop_paths(e.mimeData())
        if not paths:
            e.ignore()
            return
        e.acceptProposedAction()
        vp = self.canvas.viewport()
        pos = vp.mapFrom(w, e.position().toPoint()) if w is not vp else e.position().toPoint()
        self.drop_files(paths, pos if vp.rect().contains(pos) else None)

    def drop_files(self, paths: list[Path], at: QPoint | None = None) -> None:
        """Dropped files: PDFs/EPUBs upload to My files (as Files → Upload does), pictures are
        placed (Place) centred on `at` (a point on the picture's viewport; None: the
        middle). Refused, with a short message, while offline or for other files."""
        docs, pics, other = sort_drop([Path(p) for p in paths])
        text, refused = self.drop_label(paths)
        if refused:
            self._drop_say(text, "err")
            return
        if pics:
            self._place_dropped(pics, at)
        if docs:
            self._upload_dropped(docs)
        elif other:
            self._drop_say(f"Skipped {_names(other)}: {DROP_UNSUPPORTED}", "warn")

    def _place_dropped(self, pics: list[Path], at: QPoint | None) -> None:
        c = self.compose
        if c.busy:
            self._drop_say(DROP_PLACE_BUSY, "err")
            return
        if not c.allowed:
            self._drop_say(firmware.COMPOSE_DISABLED_MSG, "err")
            return
        centre = None
        if at is not None:  # where it was dropped on the page as shown (before Place's tools)
            centre = self.canvas.mapToScene(at)
            r = self.compose.scene.sceneRect()
            centre = QPointF(min(max(centre.x(), r.left()), r.right()),
                             min(max(centre.y(), r.top()), r.bottom()))
        self.set_pointer("place")
        for i, path in enumerate(pics):
            here = centre
            if centre is not None and i:
                here = centre + QPointF(DROP_STAGGER * i, DROP_STAGGER * i)
            elif centre is None and i:
                r = self.compose.scene.sceneRect()
                here = r.center() + QPointF(DROP_STAGGER * i, DROP_STAGGER * i)
            c.add_image(path, here, style=AUTO_STYLE)
        self.canvas.setFocus(Qt.FocusReason.OtherFocusReason)

    def paste_from_clipboard(self, text_too: bool = False) -> bool:
        """Ctrl+V: a picture on the clipboard (a screenshot, an image copied from a browser) or
        copied picture/document files are handled like a drop at the mouse (or the middle).
        With `text_too` (Place), plain text becomes a new ink text box. False: nothing taken
        (Control then types the text on the tablet)."""
        md = QGuiApplication.clipboard().mimeData()
        if md is None:
            return False
        vp = self.canvas.viewport()
        at = vp.mapFromGlobal(QCursor.pos())
        at = at if vp.rect().contains(at) else None
        paths = drop_paths(md)
        if paths:
            self.drop_files(paths, at)
            return True
        if md.hasImage():
            img = QGuiApplication.clipboard().image()
            if img.isNull():
                return False
            folder = Path(self.compose._tmp.name)
            path = folder / datetime.now().strftime("pasted-%Y%m%d-%H%M%S-%f.png")
            if not img.save(str(path), "PNG"):
                self._drop_say("Couldn't read the picture on the clipboard.", "err")
                return True
            self._place_dropped([path], at)
            return True
        if text_too and md.hasText() and md.text().strip():
            self.set_pointer("place")
            self.compose.add_ink_text(md.text().replace("\r\n", "\n").replace("\r", "\n"))
            return True
        return False

    def _upload_dropped(self, docs: list[Path]) -> None:
        if self.upload_fn is None:
            self._drop_say(DROP_OFFLINE, "err")
            return
        names = _names(docs)

        def done(report) -> None:
            if not shiboken6.isValid(self):
                return
            self._uploading = max(self._uploading - 1, 0)
            if report is None:
                self._drop_say(UPLOAD_FAILED_MSG, "err")
                return
            ok = sum(1 for r in report.results if r.ok)
            n = len(report.results)
            if ok == n:
                self._drop_say(UPLOADED_MSG.format(names), "ok")
            elif ok:
                self._drop_say(UPLOAD_PARTLY_MSG.format(ok=ok, n=n), "warn")
            else:
                self._drop_say(UPLOAD_FAILED_MSG, "err")

        self._uploading += 1
        why = self.upload_fn(docs, done)
        if why is not None:
            self._uploading -= 1
            self._drop_say(why, "err")
            return
        self._drop_say(UPLOADING_MSG.format(names), "neutral", sticky=True)

    def _drop_say(self, text: str, state: str = "neutral", sticky: bool = False) -> None:
        """A drop's message over the picture; it goes after DROP_MSG_MS unless `sticky`
        (an upload in progress: its result replaces it)."""
        self.drop_chip.set_state(state, text)
        self.drop_chip.show()
        if sticky:
            self._drop_timer.stop()
        else:
            self._drop_timer.start()
        self._update_status()

    def drop_message(self) -> str:
        return self.drop_chip.text() if not self.drop_chip.isHidden() else ""

    def _clear_drop_message(self) -> None:
        self.drop_chip.setText("")
        self.drop_chip.hide()
        self._update_status()

    def _place_refresh_bar(self) -> None:
        c = self.canvas
        top = c.mapTo(self, c.rect().topLeft())
        self.refresh_bar.setGeometry(top.x(), top.y(), c.width(), REFRESH_BAR_H)
        self.refresh_bar.raise_()

    def keyPressEvent(self, e) -> None:
        if self.fullscreen and e.key() == Qt.Key.Key_Escape:
            self.set_fullscreen(False)
            return
        super().keyPressEvent(e)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        if self.exit_full_btn.isVisible():
            self._show_exit_button()

    def _restart_service(self, job: str, on_done: Callable[[], None],
                         on_failed: Callable[[Exception], None]) -> bool:
        """Claim the tablet job slot and restart the stream service (only goMarkableStream,
        never xochitl) in a worker; the slot is released before on_done/on_failed run on
        the GUI thread. False (with the busy message shown) when the slot is taken."""
        if not self.conn.try_begin_job(job):
            self._say(JOB_BUSY_MSG.format(self.conn.current_job))
            return False
        conn, restart = self.conn, self._restart_fn

        def work():
            dev = conn.ensure()
            try:
                restart(dev)
            except DeviceError as e:
                conn.connection_lost(e)
                raise

        def finish(cb, *args):
            conn.end_job()
            if shiboken6.isValid(self):
                cb(*args)

        try:
            run_in_thread(work, lambda _r: finish(on_done), lambda e: finish(on_failed, e))
        except BaseException:
            conn.end_job()
            raise
        return True

    def restart_stream(self) -> None:
        """Restart the stream service on the tablet (the busy chip's button). A dead viewer
        connection, e.g. from unplugging USB while streaming, holds the single viewer slot;
        a restart frees it. The running client reconnects on its own."""
        if self._restarting or self.refreshing:
            return

        def done():
            self._restarting = False
            self._error = None
            self._update_status()
            self.refresh()  # reconnect now instead of after the retry delay

        def failed(e):
            self._restarting = False
            self._error = RESTART_FAILED_MSG.format(e)
            self._update_status()

        self._restarting = True
        if not self._restart_service(RESTART_JOB, done, failed):
            self._restarting = False
            return
        self._update_status()
        self._say(RESTARTING_MSG)

    # -- Control (mouse and keyboard drive the tablet) --
    def _auto_control(self) -> None:
        """Control is the default pointer: while it is chosen, turn Control on whenever it can
        be (the view shown, connected, no tablet job). Quiet when it can't."""
        c = self.control
        if (self.pointer != "control" or not self._auto_on or c.active or not self._shown
                or self.conn.state not in (State.USB, State.WIFI) or self.conn.device is None
                or self.conn.current_job is not None):
            return
        c.start()
        self._sync_control()

    def _canvas_geometry(self) -> tuple:
        """Where the frame is drawn on the picture now (the same fit and rotation it is
        painted with; the Screen view never crops, only Present does)."""
        f = self.frame
        size = (f.width, f.height) if f is not None else (rc.FRAME_W, rc.FRAME_H)
        region = QRect(0, 0, *size)
        return Geometry(QRectF(self.canvas.viewport().rect()), region, self.rotation), size

    def _sync_control(self) -> None:
        c = self.control
        self.zoom_reset_btn.setEnabled(c.active)
        no_folio = c.active and c.session is not None and not c.typing
        tip = f"Control: {rc.CONTROL_TIP}"
        if no_folio:
            tip += f"\n{rc.NO_FOLIO_MSG}."
        elif self.pointer == "control" and not c.active and c.message:
            tip += f"\n{c.message}"
        self.control_btn.setToolTip(tip)
        if c.active:  # the focus outline shows Control: no chip over the picture
            self._control_msg_timer.stop()
            self.control_chip.hide()
            self.typing_chip.setToolTip(rc.NO_FOLIO_MSG)
            self.typing_chip.setVisible(no_folio)
        elif c.message:
            self.control_chip.set_state("err" if "Control is off" in c.message else "neutral",
                                        c.message)
            self.control_chip.setToolTip("")
            self.typing_chip.hide()
            self.control_chip.show()
            self._control_msg_timer.start()
        else:
            self.control_chip.hide()
            self.typing_chip.hide()
        self.control_overlay.setVisible((c.active and no_folio) or (not c.active and bool(c.message)))
        self._control_pin.update()
        self.canvas.viewport().update()  # the focus outline follows Control

    def _clear_control_message(self) -> None:
        if not self.control.active:
            self.control.message = ""
            self._sync_control()

    @property
    def refreshing(self) -> bool:
        """The Refresh button's run is going (restart, then the first new frame)."""
        return self._refresh_stage is not None

    def refresh_picture(self) -> None:
        """The Refresh button: restart the tablet's stream service (fixes a picture the
        service latched onto the wrong memory for, which a resync doesn't), resync, and
        show a progress line until the first new frame (or REFRESH_FAILED_MSG after the
        timeout). Keep refreshing waits meanwhile. The cheap resync (refresh()) stays for
        keep refreshing, catch-up and Present's R key."""
        if self.refreshing or self._restarting or self._streaming() is None:
            return
        self._refresh_token += 1
        token = self._refresh_token

        def done():
            if token != self._refresh_token or self._refresh_stage != "restart":
                return  # timed out meanwhile
            client = self._streaming()
            if client is None:  # the stream stopped (view hidden, link lost)
                self._end_refresh()
                return
            self._refresh_stage = "frame"
            self._keep_pending = None
            self._catchup.resynced()
            client.resync()  # reconnect now instead of after the retry delay

        def failed(_e):
            if token == self._refresh_token and self.refreshing:
                self._end_refresh(REFRESH_FAILED_MSG)

        if not self._restart_service(REFRESH_JOB, done, failed):
            return
        self._refresh_stage = "restart"
        self._error = None
        self._refresh_timer.start()
        self._show_refreshing(True)
        self._update_status()

    def _refresh_timed_out(self) -> None:
        if self.refreshing:
            self._end_refresh(REFRESH_FAILED_MSG)

    def _end_refresh(self, error: str | None = None) -> None:
        self._refresh_stage = None
        self._refresh_token += 1
        self._refresh_timer.stop()
        self._show_refreshing(False)
        if error:
            self._error = error
        self._update_status()

    def _compose_busy(self, on: bool) -> None:
        """A send or Undo is running: the same thin progress bar as Refresh."""
        self._compose_working = bool(on)
        if on:
            self._place_refresh_bar()
        self.refresh_bar.setVisible(on or self._refresh_on)

    def _show_refreshing(self, on: bool) -> None:
        self._refresh_on = bool(on)
        b = self.refresh_btn
        b.setEnabled(not on)
        b.setToolTip(REFRESHING_TIP if on else REFRESH_TIP)
        b.setAccessibleDescription(REFRESHING_TIP if on else HINTS["refresh"])
        if on:
            self._place_refresh_bar()
        self.refresh_bar.setVisible(on or getattr(self, "_compose_working", False))
        self.hint.refresh()

    def refresh(self) -> None:
        """Ask the tablet for a fresh full frame: a plain resync (keep refreshing, catch-up,
        R in Present, after Restart stream)."""
        client = self._streaming()
        if client is None:
            return
        self._catchup.resynced()
        client.resync()

    def set_keep_refreshing(self, on: bool) -> None:
        """Shared by the toolbar toggle and K in Present."""
        on = bool(on)
        self.keep_refreshing = on
        if self.keep_btn.isChecked() != on:
            self.keep_btn.setChecked(on)  # re-enters with the same value; harmless
        # on: the accent icon on the accent-tinted background (theme: iconOnly:checked)
        self.keep_btn.setIcon(icon("keep", 16, TOKENS["accent-text"] if on else None))
        self.keep_btn.setAccessibleDescription(keep_hint(on))
        if getattr(self, "hint", None) is not None:
            self.hint.refresh()
        if on:
            if not self._keep_timer.isActive():
                self._keep_timer.start()
        else:
            self._keep_timer.stop()
            self._keep_pending = None

    def _keep_tick(self) -> None:
        client = self._streaming()
        if client is None:  # stream not running: nothing to refresh
            self._keep_pending = None
            return
        if self._keep_pending is client or self.refreshing:
            return  # the previous resync hasn't produced a frame yet, or Refresh runs
        self._keep_pending = client
        self._catchup.resynced()
        client.resync()

    def rotate(self) -> None:
        self.rotation = (self.rotation + 90) % 360
        if self._cfg is not None:
            self._cfg.screen_rotation = self.rotation
            try:
                self._save_config(self._cfg)
            except OSError:  # remembering the rotation is a convenience; never break the view
                log.warning("could not save rotation", exc_info=True)
        self.redraw.emit()

    def show_present(self) -> None:
        if self.present is None:
            self.present = PresentWindow(self)
        self.present.show()
        self.present.raise_()
        self.present.activateWindow()

    def screenshot(self) -> Path | None:
        """Save the current frame (rotated as displayed, not inverted or cropped) as a PNG."""
        f = self.frame
        if f is None:
            self._say("No frame to save yet.")
            return None
        img = QImage(f.gray.data, f.width, f.height, f.gray.strides[0],
                     QImage.Format.Format_Grayscale8)
        if self.rotation:
            img = img.transformed(QTransform().rotate(self.rotation))
        folder = self._pictures_dir if self._pictures_dir is not None else Path.home() / "Pictures"
        path = folder / datetime.now().strftime("rmdesk-%Y%m%d-%H%M%S.png")
        try:
            folder.mkdir(parents=True, exist_ok=True)
            ok = img.save(str(path), "PNG")
        except OSError as e:
            self._say(f"Could not save screenshot: {e}")
            return None
        if not ok:
            self._say(f"Could not save screenshot to {path}")
            return None
        self._say(f"Saved {path}")
        return path
