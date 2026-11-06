"""Placing items in the Screen view: pictures, ink text and shapes over the live tablet picture,
added to the page open on the tablet by Send. The items stay drawn on the picture whatever the
Screen view's pointer; they are interactive only with the Place pointer (set_place).

Items live in a scene that is the stream frame (1404x1872 frame pixels, unrotated); the view
only rotates and scales how they are drawn (the Screen view's Rotate setting), so the stored
positions never change. The Present window draws frames only, never these items.

Send: pre-checks here (no device I/O) -> worker: safety.send_to_open_page stops xochitl,
identifies the open page, asks the confirm question on the GUI thread (ConfirmBridge, 60 s ->
No), builds the lines (frame -> page, clipped) into the page's rmdesk layer and writes it.

Keyboard layout (Type Folio): the scroll comes from the page's handwriting laid over the live
picture (compose.scroll_fit). A page with typed text and no handwriting has nothing to lay
over, so before the job (xochitl running, reads only) needs_measuring checks the page, and the
scroll is measured by swiping the page to its top (ui.compose.scroll_probe, Control's touch).

Items follow the page: on every new stream frame, while there are items, compose.scroll_track
measures how far the picture moved along frame x since the frame the items were lined up
with; a clear scroll moves every item by as much (an item being dragged excepted), anything
else moves nothing (and a picture that looks like another page shows PAGE_CHANGED). In the
keyboard layout the tablet's touch events are read too (ui.touch_watch, read only): a
one-finger drag predicts the scroll, refined by the picture (compose.scroll_fuse), so a page
showing only its dot or line template is still followed. Items
scrolled past the picture's ends stay in the scene, faded, counted at the picture's edge
(LiveComposeView.edge_marks), and are sent: the keyboard layout keeps them (compose.build).
"""
from __future__ import annotations

import logging
import re
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

import shiboken6
from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame,
                               QGraphicsDropShadowEffect, QGraphicsScene, QLabel, QMessageBox, QPushButton,
                               QSpinBox, QVBoxLayout, QWidget)

from rmdesk import config, firmware, safety
from rmdesk.compose import frame, scroll_fit, scroll_fuse, sharptext
from rmdesk.compose import build
from rmdesk.compose.place import rotate_polylines
from rmdesk.compose.frame import FRAME_H, FRAME_W
from rmdesk.compose.items import PenStyle
from rmdesk.compose.shapes import LABELS as SHAPE_LABELS
from rmdesk.compose.lineart import DETAIL_MAX, DETAIL_MIN, THRESHOLD_MAX, THRESHOLD_MIN
from rmdesk.compose.shade import CONTRAST_MAX, CONTRAST_MIN, SPACING_MAX, SPACING_MIN
from rmdesk.config import Config
from rmdesk.connection import BUSY_MSG, Connection, State
from rmdesk.device import DeviceError
from rmdesk.notebook import page as pagemod
from rmdesk.notebook.opened import NotEditable, OpenedPage, find_opened_page
from rmdesk.ui.compose import messages as msg
from rmdesk.ui.compose import shade_job
from rmdesk.ui.compose.confirm import ConfirmBridge
from rmdesk.ui.compose.format_bar import InkFormatBar
from rmdesk.ui.compose.live_view import LiveComposeView
from rmdesk.ui.compose.scroll_probe import ScrollProbe
from rmdesk.ui.touch_watch import TouchWatcher, can_watch
from rmdesk.ui.compose.scene import (ImageSceneItem, InkSceneItem, InkStyle, NoteSceneItem,
                                     PlacedItem, ShapeSceneItem, picture_size)
from rmdesk.ui.compose.shade_job import (AUTO, LINEART, SHADED, STYLE_PEN, ShadeSettings,
                                         ShadeSpec)
from rmdesk.ui.theme import TOKENS, icon
from rmdesk.ui.widgets import Chip, Segmented
from rmdesk.ui.widgets.cluster import BETWEEN, Cluster
from rmdesk.ui.widgets.flow import FlowLayout
TOOLBAR_INSET = 12  # px: the floating Place tools from the picture's top edge (and sides)
TOOLBAR_PAD = 8  # px: inside the floating panel
from rmdesk.ui.widgets.overlay import OverlayPin
from rmdesk.ui.workers import run_in_thread

log = logging.getLogger(__name__)

OFFLINE_MSG = "Tablet not connected"
SEND_JOB = "sending to the tablet"
UNDO_JOB = "undoing the last send"
DEBOUNCE_MS = 300
TOOLS = ["fineliner", "ballpoint", "marker", "pencil"]
STYLES = [SHADED, LINEART]  # the picture Style control's segments, in order
STYLE_LABELS = {SHADED: "shaded", LINEART: "line art"}  # the confirm sheet: "Picture (shaded)"
COLORS = ["black", "grey", "white"]
# the pictures Add image (and a drop on the Screen view) takes
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".svg")
IMAGE_FILTER = "Pictures (" + " ".join(f"*{s}" for s in IMAGE_SUFFIXES) + ")"
NEW_IMAGE = 600.0  # long side of a new picture's box, frame pixels
INK_SIZE = 40.0
NOTE_SIZE = 32.0  # a new sticky note's text
SHAPE_ICONS = {"rectangle": "rectangle", "line": "line", "arrow": "arrow", "note": "note"}
UNDO_LABEL = "Undo last send"
OFFSCREEN_OPACITY = 0.3  # an item wholly off the picture (scrolled away): drawn faint
EDGE_INSET = 28.0  # frame px: where the "N items above/below" marks sit inside the edge


def icon_button(name: str, tip: str, label: str, color: str | None = None) -> QPushButton:
    """A 36 px square icon-only button; `label` is its accessible name, `tip` its tooltip."""
    b = QPushButton()
    b.setIcon(icon(name, 16, color))
    b.setProperty("iconOnly", True)
    b.setFixedSize(36, 36)
    b.setToolTip(tip)
    b.setAccessibleName(label)
    return b


class _Note(QLabel):
    """A message line under the picture that takes no room while it is empty."""

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setVisible(bool(text))


def _hgroup(*widgets: QWidget, name: str = "") -> Cluster:
    """A cluster of the tools row (the row wraps clusters as units)."""
    return Cluster(*widgets, name=name)
# Raised while the xochitl service is merely slow; the SSH link itself is fine.
_NOT_LINK_ERRORS = (safety.StopTimeout, safety.RestartTimeout)


INPUT_DEVICES = "/proc/bus/input/devices"
KEYBOARD_NAME = 'Name="rM_Keyboard"'


DEBUG_DIR = Path.home() / ".local" / "share" / "rmdesk" / "debug"


def _save_fit_debug(picture, page_data: bytes, info: dict) -> None:
    """A failed scroll match: keep what rmdesk saw (the live picture, the page, the scores) on
    this PC only, newest 3, so the failure can be looked at. Never raises."""
    try:
        import json
        import cv2
        from datetime import datetime
        DEBUG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        DEBUG_DIR.chmod(0o700)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        cv2.imwrite(str(DEBUG_DIR / f"fit-{stamp}.png"), picture)
        (DEBUG_DIR / f"fit-{stamp}.rm").write_bytes(page_data)
        (DEBUG_DIR / f"fit-{stamp}.json").write_text(json.dumps(info, indent=1))
        for f in DEBUG_DIR.iterdir():
            f.chmod(0o600)
        stamps = sorted({f.name.split(".")[0] for f in DEBUG_DIR.glob("fit-*")})
        for old in stamps[:-3]:
            for f in DEBUG_DIR.glob(old + ".*"):
                f.unlink(missing_ok=True)
    except Exception:
        log.warning("could not save the scroll-fit debug files", exc_info=True)


def _fit_summary(fits: dict) -> dict:
    """scroll_fit.fit_references' result as plain JSON-able data, for the log and debug."""
    def key(k):
        return "absolute" if k is None else f"anchor {k.part1},{k.part2}"
    return {key(k): {n: v for n, v in f.items()
                     if isinstance(v, (int, float, str, list, tuple, type(None)))}
            for k, f in fits.items()}


def item_extent(item) -> tuple[float, float, float, float]:
    """(x0, y0, x1, y1): the frame-pixel bounds of a placed item's box, as rotated."""
    p = item.pos()
    x, y, w, h = p.x(), p.y(), item.w, item.h
    corners = [[(x, y), (x + w, y), (x + w, y + h), (x, y + h)]]
    pts = rotate_polylines(corners, x + w / 2, y + h / 2, item.rotation_deg)[0]
    xs, ys = [q[0] for q in pts], [q[1] for q in pts]
    return min(xs), min(ys), max(xs), max(ys)


def offscreen_side(extent, layout: str | None) -> str | None:
    """Where an item wholly off the picture is: "above"/"below" the visible part of the page
    (keyboard layout: the page's top is at high frame x; portrait: at low frame y), "beside"
    it, or None while any of it is on the picture."""
    x0, y0, x1, y1 = extent
    if x1 >= 0 and x0 <= FRAME_W and y1 >= 0 and y0 <= FRAME_H:
        return None
    if layout == frame.KEYBOARD:
        if x0 > FRAME_W:
            return "above"
        if x1 < 0:
            return "below"
        return "beside"
    if y1 < 0:
        return "above"
    if y0 > FRAME_H:
        return "below"
    return "beside"


def _edge_point(side: str, layout: str | None) -> tuple[float, float]:
    """Frame point (inside the picture's edge) where the mark for `side` is drawn."""
    if layout == frame.KEYBOARD:
        return {"above": (FRAME_W - EDGE_INSET, FRAME_H / 2),
                "below": (EDGE_INSET, FRAME_H / 2)}.get(side, (FRAME_W / 2, EDGE_INSET))
    return {"above": (FRAME_W / 2, EDGE_INSET),
            "below": (FRAME_W / 2, FRAME_H - EDGE_INSET)}.get(side, (EDGE_INSET, FRAME_H / 2))


def _frame_x_range(specs) -> tuple[float, float]:
    """The (min, max) frame x the send snapshot's items cover: their boxes, as rotated."""
    from rmdesk.compose.place import rotate_polylines
    xs = []
    for spec in specs:
        m = spec.model if isinstance(spec, ShadeSpec) else spec[0]
        b = m.box
        corners = [[(b.x, b.y), (b.x + b.w, b.y), (b.x + b.w, b.y + b.h), (b.x, b.y + b.h)]]
        rot = getattr(m, "rotation", 0.0)
        xs += [x for x, _ in rotate_polylines(corners, b.x + b.w / 2, b.y + b.h / 2, rot)[0]]
    return (min(xs), max(xs)) if xs else (0.0, float(FRAME_W))


ORIENTATION_LOG = "Setting new orientation"
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class NeedsMeasuring(NotEditable):
    """The open page has no handwriting to line up in the keyboard layout and its scroll
    wasn't measured: refused (nothing written) so the scroll can be measured, then sent again."""


def needs_measuring(dev, picture, items_fx) -> bool:
    """Before any job, xochitl running (reads only): would the send find nothing to line up?
    The tablet in its keyboard layout and the page xochitl last recorded as open (a hint: it
    records it when it stops, so it may be stale; the send checks the real one again) giving
    no reference that new items can be placed by. False when anything about it is unclear: the send then decides."""
    if tablet_layout(dev) != frame.KEYBOARD:
        return False
    try:
        opened = find_opened_page(dev)
    except NotEditable:
        return False
    if opened.orientation != "portrait":
        return False
    fits = scroll_fit.fit_references(picture, opened.data)
    # nothing usable to line up with (no handwriting, or only groups on the text's
    # pseudo-anchors, which can't be written to): measure first
    return scroll_fit.choose_reference(fits, items_fx) is None


def tablet_layout(dev) -> str:
    """How the tablet shows pages right now: frame.KEYBOARD (Type Folio attached, keyboard
    layout) or frame.PORTRAIT_UI.

    xochitl logs "rm.orientationmodule Setting new orientation Landscape|Portrait" when the
    folio is attached or detached (fw 3.29.1.0), and once at start with the folio attached.
    The last such line from the running xochitl decides; with none (or no log to read), the
    folio's input device does. An orientation word it doesn't know, or an unreadable device
    list, refuses the send (NotEditable)."""
    pid = dev.run("systemctl show -p MainPID --value xochitl")
    pid_s = pid.out.strip() if pid.code == 0 else ""
    if not pid_s.isdigit() or pid_s == "0":  # no log to read: the folio's device decides
        return frame.KEYBOARD if keyboard_attached(dev) else frame.PORTRAIT_UI
    res = dev.run(f"journalctl -b _PID={pid_s} --no-pager -o cat 2>/dev/null "
                  f"| grep '{ORIENTATION_LOG}' | tail -n 1")
    line = _ANSI.sub("", res.out).strip() if res.code == 0 else ""  # xochitl colours its log
    if ORIENTATION_LOG in line:
        word = line.split(ORIENTATION_LOG, 1)[1].split()
        mode = word[0] if word else ""
        if mode == "Landscape":
            return frame.KEYBOARD
        if mode == "Portrait":
            return frame.PORTRAIT_UI
        raise NotEditable(msg.LAYOUT_CHECK_FAILED)
    return frame.KEYBOARD if keyboard_attached(dev) else frame.PORTRAIT_UI


def keyboard_attached(dev) -> bool:
    """True when the reMarkable Type Folio is attached (its input device is listed).
    Fails closed: any error reading the list other than its absence refuses the send."""
    res = dev.run(f"cat {INPUT_DEVICES}")
    if res.code == 0:
        return KEYBOARD_NAME in res.out
    if res.code == 1 and "No such file" in (res.err or ""):
        return False  # no input list at all (not a real tablet): nothing attached
    raise NotEditable(msg.KEYBOARD_CHECK_FAILED)


class LiveCompose(QWidget):
    """The Place tools, the live picture with the placed items, and the send/undo logic.
    `send_btn` and `undo_btn` are shown by the Screen view in its header (any pointer)."""

    actions_changed = Signal()  # the items or what may be done with them changed
    busy_changed = Signal(bool)  # a send/undo (or its measuring) started or ended
    probe_factory = ScrollProbe  # measures a typed-only page's scroll (tests replace it)
    probe_layout_on_connect = True  # ask the tablet its layout once connected (tests: off)
    touch_watcher_factory = TouchWatcher  # reads the tablet's touch events (tests replace it)

    def __init__(self, screen, conn: Connection, cfg: Config,
                 save_config: Callable[[Config], None] = config.save,
                 backup_root: Path = safety.BACKUP_ROOT):
        super().__init__()
        self.screen, self.conn, self.cfg = screen, conn, cfg
        self._save_config, self._root = save_config, backup_root
        self._tmp = tempfile.TemporaryDirectory(prefix="rmdesk-compose-")
        self.busy: str | None = None
        self.mode = "off"
        self.allowed = False
        self._pen = PenStyle()
        self._ink_style = InkStyle()  # the last style used: new ink text boxes start with it
        # items follow the page: the tablet's layout (cached: on connect, after each send),
        # the frame tracker, and an item held by the mouse (its shift waits for the release)
        self.layout: str | None = None
        self._online = False
        self._tracker = scroll_fuse.Follower()
        self.touch_watch = self.touch_watcher_factory(parent=self)
        self.touch_watch.touched.connect(self._on_touch)
        self._tracked_frame = None
        self.track_ms = 0.0  # the last frame's tracking cost
        self._held: dict | None = None
        self._press_pos: list = []

        self.add_image_btn = icon_button("image", "Add image… (drawn in pen strokes: shaded, or line art for diagrams)",
                                         "Add image")
        self.add_ink_btn = icon_button("text", "Add ink text: a box to type into",
                                       "Add ink text")
        self.shape_btns: dict[str, QPushButton] = {}
        for kind, name in SHAPE_ICONS.items():
            b = icon_button(name, SHAPE_LABELS[kind], SHAPE_LABELS[kind])
            b.clicked.connect(lambda _c=False, k=kind: self.add_shape(k))
            self.shape_btns[kind] = b
        self.add_rect_btn = self.shape_btns["rectangle"]
        self.add_line_btn = self.shape_btns["line"]
        self.add_arrow_btn = self.shape_btns["arrow"]
        self.add_note_btn = self.shape_btns["note"]
        self.add_rect_btn.setToolTip("A rectangle in the middle of the picture")
        self.add_line_btn.setToolTip("A straight line: drag the corner to change its length, "
                                     "the circle to turn it")
        self.add_arrow_btn.setToolTip("An arrow: drag the corner to change its length, "
                                      "the circle to turn it")
        self.add_note_btn.setToolTip("A sticky note to type into")
        self.remove_btn = icon_button("trash", "Remove the selected items", "Remove",
                                      TOKENS["accent-text"])
        self.remove_btn.setProperty("variant", "danger")
        self.tool_combo = QComboBox()
        self.tool_combo.addItems(TOOLS)
        self.tool_combo.setToolTip("Pen")
        self.tool_combo.setAccessibleName("Pen")
        self.color_combo = QComboBox()
        self.color_combo.addItems(COLORS)
        self.color_combo.setToolTip("Colour")
        self.color_combo.setAccessibleName("Colour")
        self.thick_spin = QDoubleSpinBox()
        self.thick_spin.setRange(0.5, 3.0)
        self.thick_spin.setSingleStep(0.25)
        self.thick_spin.setValue(1.0)
        self.thick_spin.setToolTip("Thickness")
        self.thick_spin.setAccessibleName("Thickness")
        for w in (self.tool_combo, self.color_combo, self.thick_spin):
            w.setFixedHeight(36)  # level with the icon buttons in the tools row
        default = ShadeSettings()
        self.spacing_spin = QSpinBox()
        self.spacing_spin.setRange(SPACING_MIN, SPACING_MAX)
        self.spacing_spin.setValue(default.spacing)
        self.spacing_spin.setSuffix(" px")
        self.spacing_spin.setToolTip("Distance between the shading lines")
        self.spacing_spin.setAccessibleName("Spacing")
        self.contrast_spin = QDoubleSpinBox()
        self.contrast_spin.setRange(CONTRAST_MIN, CONTRAST_MAX)
        self.contrast_spin.setSingleStep(0.1)
        self.contrast_spin.setDecimals(1)
        self.contrast_spin.setValue(default.contrast)
        self.contrast_spin.setToolTip("Picture contrast")
        self.contrast_spin.setAccessibleName("Contrast")
        self.cross_check = QCheckBox("Cross-hatch")
        self.cross_check.setChecked(default.crosshatch)
        self.cross_check.setToolTip("Extra diagonal lines over the darkest parts, like an "
                                    "engraving: deeper blacks in photos (more ink, slower)")
        self.invert_check = QCheckBox("Invert")
        self.invert_check.setChecked(default.invert)
        self.invert_check.setToolTip("Swap light and dark: for a light logo, text or lines on a "
                                     "dark background (transparent parts stay empty)")
        self.style_seg = Segmented(["Shaded", "Line art"])
        self.style_seg.setToolTip("Shaded: the picture's tones as hatching (photos). Line art: "
                                  "its lines as clean pen strokes (diagrams, logos, charts)")
        self.style_seg.setAccessibleName("Style")
        self.detail_spin = QSpinBox()
        self.detail_spin.setRange(DETAIL_MIN, DETAIL_MAX)
        self.detail_spin.setValue(default.detail)
        self.detail_spin.setToolTip("How small a line still counts: higher keeps smaller lines "
                                    "and finer curves")
        self.detail_spin.setAccessibleName("Line detail")
        self.threshold_spin = QDoubleSpinBox()
        self.threshold_spin.setRange(THRESHOLD_MIN, THRESHOLD_MAX)
        self.threshold_spin.setSingleStep(0.1)
        self.threshold_spin.setDecimals(1)
        self.threshold_spin.setValue(default.threshold)
        self.threshold_spin.setToolTip("Which grey counts as a line: lower picks up fainter "
                                       "lines, higher keeps only the darkest")
        self.threshold_spin.setAccessibleName("Threshold")
        self.sharp_check = QCheckBox("Sharp text")
        ok, why = sharptext.status()
        self.sharp_check.setChecked(default.sharp_text and ok)
        self.sharp_check.setEnabled(ok)
        self.sharp_check.setToolTip(
            "Small words are read (on this computer, with Tesseract) and redrawn as clean "
            "pen letters, each in its own place; lines and boxes are drawn as before" if ok
            else why)
        self.sharp_check.setAccessibleName("Sharp text")
        # the selected item's extras, in the tools row: two small groups so the row can wrap
        for w in (self.spacing_spin, self.contrast_spin, self.detail_spin, self.threshold_spin,
                  self.style_seg):
            w.setFixedHeight(36)
        self.style_controls = _hgroup(self.style_seg, name="Picture style")
        self.shade_controls = _hgroup(QLabel("Spacing"), self.spacing_spin, QLabel("Contrast"),
                                      self.contrast_spin, name="Picture shading")
        self.lineart_controls = _hgroup(QLabel("Line detail"), self.detail_spin,
                                        QLabel("Threshold"), self.threshold_spin,
                                        self.sharp_check, name="Picture line art")
        self.shade_options = _hgroup(self.cross_check, self.invert_check,
                                     name="Picture options")  # while a picture is selected
        for w in (self.style_controls, self.shade_controls, self.lineart_controls,
                  self.shade_options):
            w.hide()
        self.fill_check = QCheckBox("Fill")
        self.fill_check.setToolTip("Hatch the inside of the rectangle")
        self.double_check = QCheckBox("Double")
        self.double_check.setToolTip("A head at both ends of the arrow")
        self.shape_controls = _hgroup(self.fill_check, self.double_check,
                                      name="Shape options")  # rectangle / arrow
        self.shape_controls.hide()
        self.send_btn = QPushButton("Send to tablet")
        self.send_btn.setIcon(icon("send", 16, "#FFFFFF"))
        self.send_btn.setProperty("variant", "primary")
        self.send_btn.setProperty("hasBadge", True)
        self.send_btn.setFixedHeight(36)
        self.badge = QLabel("0", self.send_btn)  # how many items the next send writes
        self.badge.setProperty("badge", True)
        self.badge.setAccessibleName("Items to send")
        self.badge.hide()
        self.send_btn.installEventFilter(self)
        self.undo_btn = icon_button("undo", UNDO_LABEL, UNDO_LABEL)
        self.undo_btn.setProperty("variant", "ghost")
        for b in (self.add_image_btn, self.add_ink_btn, *self.shape_btns.values()):
            b.setProperty("variant", "ghost")
        # one compact row of clusters: [add tools] [remove] [pen] [the selected item's
        # extras]; the clusters wrap as units
        self.add_tools = _hgroup(self.add_image_btn, self.add_ink_btn,
                                 *self.shape_btns.values(), name="Add")
        self.remove_tools = _hgroup(self.remove_btn, name="Remove")
        self.pen_controls = _hgroup(self.tool_combo, self.color_combo, self.thick_spin,
                                    name="Pen")
        self.tools_row = QWidget()
        bar = FlowLayout(self.tools_row, spacing=BETWEEN, vspacing=8)
        for w in (self.add_tools, self.remove_tools, self.pen_controls, self.style_controls,
                  self.shade_controls, self.lineart_controls, self.shade_options,
                  self.shape_controls):
            bar.addWidget(w)
        self.place = False

        self.disabled_label = QLabel(firmware.COMPOSE_DISABLED_MSG)
        self.disabled_label.setWordWrap(True)
        # "Testing mode" chip over the picture (top right)
        self.banner = Chip("warn", "")
        self.banner.setWordWrap(True)
        self.banner.setMaximumHeight(16777215)
        self.banner.setMinimumHeight(24)
        self.banner.hide()
        self.status = _Note("")
        self.status.setObjectName("hint")
        self.message = _Note("")
        self.message.setWordWrap(True)
        self.message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details = _Note("")
        self.details.setObjectName("muted")
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # Qt-owned (parent self): deleted with the panel on the GUI thread, never by Python's
        # cyclic GC on a worker thread (that would leave its index timer registered)
        self.scene = QGraphicsScene(0, 0, FRAME_W, FRAME_H, self)
        self.view = LiveComposeView(screen, self.scene)
        # floats over the picture above the selected ink text box (replaces the pen controls)
        self.format_bar = InkFormatBar(self.view.viewport())
        # the Place tools float over the picture (top centre): the picture keeps its size in
        # Control and Place. A child of the viewport: a click on the panel between its
        # widgets is not taken (QWidget ignores it) and reaches the view, so items under it
        # stay reachable
        self.toolbar = QFrame(self.view.viewport())
        self.toolbar.setObjectName("placeBar")
        self.toolbar.setAccessibleName("Place tools")
        self.toolbar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        shadow = QGraphicsDropShadowEffect(self.toolbar)
        shadow.setBlurRadius(18)
        shadow.setOffset(0, 3)
        shadow.setColor(QColor(0, 0, 0, 110))
        self.toolbar.setGraphicsEffect(shadow)
        tb = QVBoxLayout(self.toolbar)
        tb.setContentsMargins(*([TOOLBAR_PAD] * 4))
        tb.setSpacing(8)
        tb.addWidget(self.tools_row)
        tb.addWidget(self.disabled_label)
        self.toolbar.hide()
        self.format_bar.avoid = self.toolbar  # the ink format bar stays below the tools
        self.view.viewport().installEventFilter(self)
        self.tools_row.installEventFilter(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(self.view, 1)
        self.banner_box = QWidget(self)
        self.banner_box.setObjectName("overlay")
        bb = QVBoxLayout(self.banner_box)
        bb.setContentsMargins(0, 0, 0, 0)
        bb.addWidget(self.banner)
        self._banner_pin = OverlayPin(self.banner_box, self, corner="tr")
        self._banner_pin.set_host(self.view)
        self.banner_box.raise_()
        # "the tablet page changed" chip over the picture (top left)
        self.page_chip = Chip("warn", msg.PAGE_CHANGED)
        self.page_chip.setAccessibleName("Page changed")
        self.page_box = QWidget(self)
        self.page_box.setObjectName("overlay")
        pb = QVBoxLayout(self.page_box)
        pb.setContentsMargins(0, 0, 0, 0)
        pb.addWidget(self.page_chip)
        self._page_pin = OverlayPin(self.page_box, self, corner="tl")
        self._page_pin.set_host(self.view)
        self.page_box.hide()
        for w in (self.status, self.message, self.details):
            lay.addWidget(w)
            w.setVisible(bool(w.text()))

        self.bridge = ConfirmBridge(self)
        # teardown answers a pending confirm with No at once (plain Python: safe while dying)
        self.destroyed.connect(self.bridge.canceller())
        self._reshade = QTimer(self)
        self._reshade.setSingleShot(True)
        self._reshade.setInterval(DEBOUNCE_MS)
        self._reshade.timeout.connect(self._reshade_stale)
        self.add_image_btn.clicked.connect(self._pick_image)
        self.add_ink_btn.clicked.connect(self.new_ink_text)
        self.remove_btn.clicked.connect(self.remove_selected)
        self.tool_combo.currentTextChanged.connect(lambda _t: self._pen_changed())
        self.color_combo.currentTextChanged.connect(lambda _t: self._pen_changed())
        self.thick_spin.valueChanged.connect(lambda _v: self._pen_changed())
        self.spacing_spin.valueChanged.connect(lambda _v: self._settings_changed())
        self.contrast_spin.valueChanged.connect(lambda _v: self._settings_changed())
        self.cross_check.toggled.connect(lambda _v: self._settings_changed())
        self.invert_check.toggled.connect(lambda _v: self._settings_changed())
        self.style_seg.changed.connect(lambda _i: self._style_changed())
        self.detail_spin.valueChanged.connect(lambda _v: self._settings_changed())
        self.threshold_spin.valueChanged.connect(lambda _v: self._settings_changed())
        self.sharp_check.toggled.connect(lambda _v: self._settings_changed())
        self.fill_check.toggled.connect(lambda _v: self._shape_options_changed())
        self.double_check.toggled.connect(lambda _v: self._shape_options_changed())
        self.scene.selectionChanged.connect(self._selection_changed)
        self.view.fitted.connect(self.format_bar.place)
        self.format_bar.style_changed.connect(self._format_style)
        self.format_bar.size_changed.connect(self._format_size)
        self.format_bar.pen_changed.connect(self._format_pen)
        self.format_bar.done.connect(self._refocus_text)
        self.send_btn.clicked.connect(self.send)
        self.undo_btn.clicked.connect(self.undo)
        changed = getattr(screen, "frame_changed", None)
        if changed is not None:
            changed.connect(self._on_frame)
        self.scene.installEventFilter(self)
        self.scene.changed.connect(lambda _r: self._refresh_offscreen())
        self._prune()
        self.on_state(conn.state)
        self.set_place(False)

    def set_place(self, on: bool) -> None:
        """The Place pointer: the tools show and the items take the mouse and keys. Off, the
        items stay drawn but nothing edits them (any typing ends, the selection clears)."""
        on = bool(on)
        self.place = on
        if not on:
            for it in self.placed_items():
                it.end_editing()
            self.scene.clearSelection()
            self.format_bar.show_item(None)
        self.view.setInteractive(on)
        self.disabled_label.setVisible(on and not self.allowed)
        self.toolbar.setVisible(on)
        self._place_toolbar()
        self.view.viewport().update()

    def eventFilter(self, obj, e) -> bool:
        if obj is self.send_btn and e.type() == QEvent.Type.Resize:
            self._place_badge()
        elif obj is self.view.viewport() and e.type() == QEvent.Type.Resize:
            self._place_toolbar()
        elif obj is self.tools_row and e.type() == QEvent.Type.LayoutRequest:
            QTimer.singleShot(0, self._place_toolbar)  # a cluster came or went
        elif obj is self.scene and e.type() == QEvent.Type.GraphicsSceneMousePress:
            # where everything was when the mouse went down (a drag moves one from there)
            self._press_pos = [(it, it.pos()) for it in self.placed_items()]
        elif obj is self.scene and e.type() == QEvent.Type.GraphicsSceneMouseRelease \
                and self._held is not None:
            QTimer.singleShot(0, self._settle_held)  # once the item has had the release
        return super().eventFilter(obj, e)

    def _place_toolbar(self) -> None:
        """Top centre of the picture, TOOLBAR_INSET px in; as wide as one row of the clusters
        needs, wrapping to more rows when the picture is narrower (never past its edges)."""
        tb = self.toolbar
        if not shiboken6.isValid(tb) or not tb.isVisible():
            return
        vp = self.view.viewport()
        flow = self.tools_row.layout()
        items = [flow.itemAt(i) for i in range(flow.count())]
        widths = [it.sizeHint().width() for it in items if not it.isEmpty()]
        one_row = sum(widths) + flow.spacing() * max(len(widths) - 1, 0)
        if self.disabled_label.isVisibleTo(tb):
            one_row = max(one_row, min(self.disabled_label.sizeHint().width(), 480))
        cm = tb.contentsMargins()  # a styled frame's border, if any
        pad = 2 * TOOLBAR_PAD + cm.left() + cm.right()
        w = max(min(one_row + pad, vp.width() - 2 * TOOLBAR_INSET), 1)
        h = tb.layout().totalHeightForWidth(w) if tb.layout().hasHeightForWidth() \
            else tb.layout().totalSizeHint().height()
        tb.setFixedSize(w, max(h, 1))
        tb.move(max((vp.width() - w) // 2, 0), TOOLBAR_INSET)
        tb.raise_()
        if self.format_bar.isVisible():
            self.format_bar.place()

    def _place_badge(self) -> None:
        b, btn = self.badge, self.send_btn
        b.adjustSize()
        b.setFixedHeight(18)
        b.setMinimumWidth(18)
        b.move(btn.width() - b.width() - 10, (btn.height() - b.height()) // 2)
        b.raise_()

    # -- plumbing -------------------------------------------------------------
    def _alive(self, fn: Callable) -> Callable:
        # a late worker result must not touch a view that has been destroyed
        return lambda r: fn(r) if shiboken6.isValid(self) else None

    def _on_device(self, op: Callable, done: Callable, err: Callable,
                   release: Callable[[], None]) -> None:
        """Run op(device) in a worker. ensure() errors become msg.NotConnected; a refusal
        (NotEditable) or a No (Cancelled) is passed on as is; only link failures (not a slow
        xochitl) mark the connection lost. `release` runs on the GUI thread when the work
        ends, even if the view is gone."""
        conn = self.conn

        def job():
            try:
                dev = conn.ensure()
            except Exception as e:
                raise msg.NotConnected(str(e)) from e
            try:
                return op(dev)
            except safety.Cancelled:
                raise
            except NotEditable as e:  # still a refusal, but the link may have dropped under it
                cause = e.__cause__
                if isinstance(cause, DeviceError) and not isinstance(cause, _NOT_LINK_ERRORS):
                    conn.connection_lost(cause)
                raise
            except DeviceError as e:
                if not isinstance(e, _NOT_LINK_ERRORS):
                    conn.connection_lost(e)
                raise

        def then(fn):
            alive = self._alive(fn)

            def call(r):
                release()
                alive(r)
            return call
        try:
            run_in_thread(job, then(done), then(err))
        except BaseException:
            release()
            raise

    def shutdown(self) -> None:
        """Window closing: a confirm still waiting for an answer is answered No now; a scroll
        measurement stops (the finger is lifted)."""
        self.bridge.cancel()
        probe = getattr(self, "_probe", None)
        if probe is not None:
            probe.cancel()
        self.touch_watch.shutdown()

    def _claim_job(self, name: str) -> bool:
        """Claim the connection-wide xochitl job slot, or say who holds it."""
        if self.conn.try_begin_job(name):
            return True
        self.show_message(BUSY_MSG.format(self.conn.current_job))
        return False

    def _begin(self, label: str) -> None:
        self.busy = label
        self.status.setText(label)
        self._update_actions()
        self.busy_changed.emit(True)

    def _end(self) -> None:
        self.busy = None
        self.status.setText("")
        self._update_actions()
        self.busy_changed.emit(False)

    def show_message(self, text: str, details: str = "", error: bool = True) -> None:
        self.message.setObjectName("error" if error else "hint")
        self.message.style().unpolish(self.message)
        self.message.style().polish(self.message)
        self.message.setText(text)
        self.details.setText(f"Details: {details}" if details else "")

    def ask(self, text: str) -> bool:
        """A Yes/No question on the GUI thread (before anything touches the tablet)."""
        answer = QMessageBox.question(self, "rmdesk", text)
        return answer == QMessageBox.StandardButton.Yes

    def _save(self) -> None:
        try:
            self._save_config(self.cfg)
        except Exception as e:
            self.show_message("Couldn't save rmdesk's settings.", str(e))

    def _prune(self) -> None:
        last = self.cfg.last_send or {}
        protect = [Path(last["backup_dir"])] if last.get("backup_dir") else []
        root = self._root
        run_in_thread(lambda: safety.prune(root, protect=protect), lambda _r: None, lambda _e: None)

    # -- state ----------------------------------------------------------------
    def _version(self) -> str:
        info = self.conn.info
        return info.version if info is not None else ""

    def on_state(self, _state=None) -> None:
        online = self.conn.state in (State.USB, State.WIFI)
        if online and not self._online:
            self._probe_layout()
        elif not online:
            self.layout = None
            self._tracker.keyboard = False
        self._online = online
        info = self.conn.info
        self.mode = firmware.compose_mode(info) if online and info is not None else "off"
        self.allowed = self.mode != "off"
        test_only = self.mode == "test-only"
        self.banner.setText(firmware.testing_banner(self._version()) if test_only else "")
        self.banner.setVisible(test_only)
        self.banner_box.setVisible(test_only)
        self._banner_pin.update()
        self.disabled_label.setText(firmware.COMPOSE_DISABLED_MSG if online else OFFLINE_MSG)
        self.disabled_label.setVisible(getattr(self, "place", False) and not self.allowed)
        if hasattr(self, "toolbar"):
            self._place_toolbar()
        self._update_actions()

    # -- items follow the page ----------------------------------------------------
    def _probe_layout(self) -> None:
        """Ask the tablet how it shows pages (tablet_layout), in a worker; a failure leaves
        it unknown (the send asks again anyway)."""
        dev = getattr(self.conn, "device", None)
        if not self.probe_layout_on_connect or dev is None:
            return
        run_in_thread(lambda: tablet_layout(dev), self._alive(self.set_layout),
                      lambda _e: None)

    def set_layout(self, layout: str | None) -> None:
        """The tablet's layout as last seen. A change re-shades the pictures: the keyboard
        layout shades their parts past the frame's ends too (shade_job `wide`)."""
        if layout == self.layout:
            return
        self.layout = layout
        self._tracker.keyboard = layout == frame.KEYBOARD
        self._sync_watch()
        for it in self.placed_items():
            if isinstance(it, ImageSceneItem):
                it.shaded_key = it.pending_key = None
        self._reshade.start()
        self._refresh_offscreen()

    def _wide(self) -> bool:
        return self.layout == frame.KEYBOARD

    def _sync_watch(self) -> None:
        """Read the tablet's touch events while there are items, the tablet is connected and
        shows the keyboard layout (where a drag scrolls the page); else close the channel."""
        dev = getattr(self.conn, "device", None)
        want = (self._online and self.layout == frame.KEYBOARD and can_watch(dev)
                and bool(self.placed_items()))
        self.touch_watch.watch(dev if want else None)

    def _on_touch(self, ev) -> None:
        """A one-finger gesture step from the tablet (TouchWatcher): a drag predicts the
        scroll; the items move only on the next frame (one answer per frame)."""
        if shiboken6.isValid(self) and self.placed_items():
            self._tracker.touch(ev)

    def _on_frame(self) -> None:
        """A new stream frame: while there are items, follow the page (scroll_fuse: the
        picture, with the finger's drags in the keyboard layout)."""
        if not shiboken6.isValid(self):
            return
        f = getattr(self.screen, "frame", None)
        if f is None or f is self._tracked_frame:
            return
        self._tracked_frame = f
        if not self.placed_items():
            self._tracker.reset()
            self._show_page_warning(False)
            return
        t0 = time.perf_counter()
        step = self._tracker.step(f.gray)
        self.track_ms = (time.perf_counter() - t0) * 1000.0
        if step.dx:
            log.debug("page scrolled: items moved %+d px (%.1f ms)", step.dx, self.track_ms)
            self.shift_items(step.dx)
        self._show_page_warning(self._tracker.warn)

    def shift_items(self, dx: float) -> None:
        """Move every placed item `dx` frame px along frame x (the page scrolled). An item
        held by the mouse keeps still: its shift waits for the release (_settle_held)."""
        held = self._held_item()
        for it in self.placed_items():
            if it is held:
                if self._held is None or self._held["item"] is not it:
                    busy = bool(getattr(it, "_resizing", False) or getattr(it, "_rotating", False))
                    start = next((p for i, p in self._press_pos if i is it), it.pos())
                    self._held = {"item": it, "transform": busy, "pos": start, "dx": 0.0}
                self._held["dx"] += dx
                continue
            it.setPos(it.pos().x() + dx, it.pos().y())
        self._refresh_offscreen()

    def _held_item(self) -> PlacedItem | None:
        g = self.scene.mouseGrabberItem()
        return g if isinstance(g, PlacedItem) else None

    def _settle_held(self) -> None:
        """The held item was let go. Resized or turned: its waiting shift is applied (the
        rest of it stayed on the old spot of the page). Dragged to a new place: none is (it
        was dropped where the user saw it on the current picture). Only clicked: applied."""
        held = self._held
        if held is None or not shiboken6.isValid(self):
            return
        it = held["item"]
        if self._held_item() is it:
            return  # still held
        self._held, self._press_pos = None, []
        if not shiboken6.isValid(it) or it.scene() is not self.scene:
            return
        if held["transform"] or it.pos() == held["pos"]:
            it.setPos(it.pos().x() + held["dx"], it.pos().y())
        self._refresh_offscreen()

    def _show_page_warning(self, on: bool) -> None:
        on = bool(on) and not self.busy and bool(self.placed_items())
        if self.page_box.isVisible() != on:
            self.page_box.setVisible(on)
            if on:
                self._page_pin.update()
                self.page_box.raise_()

    def offscreen_counts(self) -> dict[str, int]:
        """How many placed items are wholly off the picture, by side (above/below/beside)."""
        counts: dict[str, int] = {}
        for it in self.placed_items():
            side = offscreen_side(item_extent(it), self.layout)
            if side is not None:
                counts[side] = counts.get(side, 0) + 1
        return counts

    def _refresh_offscreen(self) -> None:
        """Fade the items wholly off the picture and mark how many are past each edge."""
        if not (shiboken6.isValid(self) and shiboken6.isValid(self.scene)):
            return
        counts: dict[str, int] = {}
        for it in self.placed_items():
            side = offscreen_side(item_extent(it), self.layout)
            want = 1.0 if side is None else OFFSCREEN_OPACITY
            if it.opacity() != want:
                it.setOpacity(want)
            if side is not None:
                counts[side] = counts.get(side, 0) + 1
        marks = [(_edge_point(side, self.layout), msg.offscreen_label(n, side))
                 for side, n in sorted(counts.items())]
        if marks != self.view.edge_marks:
            self.view.edge_marks = marks
            self.view.viewport().update()

    def _undo_ok(self, last: dict | None) -> bool:
        """In testing mode only a send to the acceptance notebook may be undone (the tablet-side
        guard checks it again on the safety backup)."""
        if self.mode != "test-only":
            return True
        return firmware.is_acceptance_name(str((last or {}).get("name") or ""))

    def _last_send_name(self) -> str:
        name = (self.cfg.last_send or {}).get("name")
        return f"“{name}”" if name else msg.UNKNOWN_NOTEBOOK

    def _can_send(self) -> bool:
        items = self.placed_items()
        return bool(items) and all(i.ready() for i in items)

    def _update_actions(self) -> None:
        idle = self.allowed and not self.busy
        for w in (self.add_image_btn, self.add_ink_btn, *self.shape_btns.values()):
            w.setEnabled(idle)
        self.remove_btn.setEnabled(idle and bool(self._selected()))
        self.send_btn.setEnabled(idle and self._can_send())
        n = len(self.placed_items())
        self.badge.setText(str(n))
        self.badge.setVisible(n > 0)
        self._place_badge()
        blank = any(isinstance(i, ImageSceneItem) and i.shaded is not None and i.shaded.blank
                    for i in self.placed_items())
        self.send_btn.setToolTip(msg.BLANK_TIP if blank else "")
        has_undo = bool((self.cfg.last_send or {}).get("backup_dir"))
        self.undo_btn.setEnabled(idle and has_undo and self._undo_ok(self.cfg.last_send))
        self.undo_btn.setToolTip(f"{UNDO_LABEL}: " + msg.UNDO_TIP.format(name=self._last_send_name())
                                 if has_undo else f"{UNDO_LABEL} (nothing to undo)")
        for it in self.placed_items():
            it.setFlag(it.GraphicsItemFlag.ItemIsMovable, not self.busy)
            if isinstance(it, InkSceneItem):
                it.editable = not self.busy
                if self.busy:
                    it.end_editing()
        self.format_bar.setEnabled(not self.busy)
        self._refresh_offscreen()
        if not self.placed_items():
            self._tracker.reset()
        self._sync_watch()
        self._show_page_warning(self._tracker.warn)
        self.actions_changed.emit()

    def has_undo(self) -> bool:
        return bool((self.cfg.last_send or {}).get("backup_dir"))

    # -- items ----------------------------------------------------------------
    def placed_items(self) -> list[PlacedItem]:
        return [i for i in self.scene.items() if isinstance(i, PlacedItem)]

    def _selected(self) -> list[PlacedItem]:
        return [i for i in self.scene.selectedItems() if isinstance(i, PlacedItem)]

    def _place(self, item: PlacedItem, centre=None) -> PlacedItem:
        cx, cy = (FRAME_W / 2, FRAME_H / 2) if centre is None else (centre.x(), centre.y())
        item.setPos(cx - item.w / 2, cy - item.h / 2)
        # the view shows the frame rotated (Rotate in Screen, e.g. to match a landscape page):
        # start the item upright as the user sees it, not upright in the portrait frame
        turn = getattr(self.screen, "rotation", 0) % 360
        if turn:
            item.set_rotation(-turn)
        self.scene.addItem(item)
        if self._tracker.warn:  # placed on the page shown now: follow this one from here
            self._tracker.reset()
        self.scene.clearSelection()
        item.setSelected(True)
        self._update_actions()
        return item

    def _pick_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Add image", str(Path.home()), IMAGE_FILTER)
        if path:
            self.add_image(Path(path), style=AUTO)

    def add_image(self, path: Path, centre=None, style: str = SHADED) -> ImageSceneItem:
        """A picture, centred on `centre` (frame pixels; default the page's middle), drawn in
        `style`: SHADED, LINEART, or AUTO (what the user adds: the first shading job guesses
        from the content, `shade_job.resolve`, and the user can switch)."""
        path = Path(path)
        size = picture_size(path)  # header only; an unreadable file fails in the worker
        w = h = NEW_IMAGE
        if size is not None:
            k = NEW_IMAGE / max(size)
            w, h = max(size[0] * k, 1.0), max(size[1] * k, 1.0)
        pen = PenStyle(STYLE_PEN[LINEART if style == LINEART else SHADED])
        item = ImageSceneItem(path, w, h, pen, ShadeSettings(style=style))
        self._place(item, centre)
        item.geometry_changed.connect(self._geometry_changed)
        self._shade(item)
        return item

    def _shade(self, item: ImageSceneItem) -> None:
        """Shade in a worker for the item's geometry and settings now; only the newest
        shading of an item is used (older results are dropped)."""
        item.shade_gen += 1
        gen, key = item.shade_gen, item.key()
        item.pending_key = key
        spec = item.send_data()
        source, raster = item.source, item.raster
        tmp = Path(self._tmp.name) / f"{id(item)}-{gen}.png"

        def done(shaded: shade_job.Shaded):
            if gen != item.shade_gen or item.scene() is not self.scene:
                return  # a newer shading was started, or the item was removed
            item.pending_key = None
            drawn = shaded.spec or spec
            done_key = key
            if item.settings.style == AUTO and drawn.settings.style != AUTO:
                # the content guess: the item takes the style (and pen) it was drawn in
                item.settings, item.pen = drawn.settings, drawn.model.pen
                done_key = key[:-2] + (drawn.model.pen.tool, drawn.settings)
                if item.isSelected():
                    self._selection_changed()
            item.set_shaded(shaded, done_key)
            res = shaded.result
            if shaded.blank:
                self.show_message(msg.PICTURE_BLANK, error=False)
            elif res.capped:
                self.show_message(msg.SHADE_PARTIAL, error=False)
            elif drawn.settings.style == SHADED and res.spacing_used > spec.settings.spacing:
                self.show_message(msg.SHADE_WIDER.format(n=res.spacing_used), error=False)
            if item.key() != done_key:
                self._reshade.start()  # it moved or changed while being shaded
            self._update_actions()

        def failed(e):
            if gen != item.shade_gen or item.scene() is not self.scene:
                return
            self.scene.removeItem(item)
            self._update_actions()
            self.show_message(msg.PICTURE_FAILED, str(e))

        wide = self._wide()
        run_in_thread(lambda: shade_job.run(source, raster, tmp, spec, wide),
                      self._alive(done), self._alive(failed))

    def _geometry_changed(self) -> None:
        self._reshade.start()  # debounced: restarts on every drag/resize/rotate step

    def _settings_changed(self) -> None:
        settings = ShadeSettings(self.spacing_spin.value(), round(self.contrast_spin.value(), 1),
                                 self.cross_check.isChecked(), self.invert_check.isChecked(),
                                 STYLES[max(self.style_seg.index(), 0)],
                                 self.detail_spin.value(), round(self.threshold_spin.value(), 1),
                                 self.sharp_check.isChecked())
        for it in self._selected():
            if isinstance(it, ImageSceneItem):
                it.settings = settings
        self._reshade.start()  # debounced: restarts on every step

    def _style_changed(self) -> None:
        """Shaded <-> Line art for the selected pictures: their controls swap, and a picture
        still on the other style's own pen (pencil / fineliner) takes this style's."""
        style = STYLES[max(self.style_seg.index(), 0)]
        other = STYLES[1 - STYLES.index(style)]
        for it in self._selected():
            if isinstance(it, ImageSceneItem) and it.pen.tool == STYLE_PEN[other]:
                it.pen = replace(it.pen, tool=STYLE_PEN[style])
                if len(self._selected()) == 1:
                    self.tool_combo.blockSignals(True)
                    self.tool_combo.setCurrentText(it.pen.tool)
                    self.tool_combo.blockSignals(False)
        self._show_picture_controls(True, style)
        self._settings_changed()

    def _show_picture_controls(self, pictures: bool, style: str) -> None:
        self.style_controls.setVisible(pictures)
        self.shade_controls.setVisible(pictures and style != LINEART)
        self.lineart_controls.setVisible(pictures and style == LINEART)
        self.shade_options.setVisible(pictures)
        self.cross_check.setVisible(style != LINEART)  # Invert is there for both

    def _reshade_stale(self) -> None:
        if not shiboken6.isValid(self):
            return
        for it in self.placed_items():
            if isinstance(it, ImageSceneItem) and it.key() not in (it.shaded_key, it.pending_key):
                self._shade(it)

    def new_ink_text(self) -> InkSceneItem | None:
        """An empty ink text box in the middle of the visible picture, ready to type into."""
        if not self.allowed or self.busy:
            return None
        for it in self.placed_items():
            it.end_editing()
        item = InkSceneItem("", INK_SIZE, self._pen, self._ink_style)
        self._wire_ink(item)
        vp = self.view.viewport().rect()
        self._place(item, self.view.mapToScene(vp.center()))
        self.view.setFocus()
        item.start_editing()
        return item

    def add_shape(self, kind: str) -> PlacedItem | None:
        """A default-sized rectangle, line, arrow or sticky note in the middle of the visible
        picture, selected (a sticky note ready to type into)."""
        if not self.allowed or self.busy:
            return None
        for it in self.placed_items():
            it.end_editing()
        centre = self.view.mapToScene(self.view.viewport().rect().center())
        if kind in ("note", "rectangle"):
            note = NoteSceneItem("", NOTE_SIZE, self._pen, self._ink_style, kind=kind)
            self._wire_ink(note)
            self._place(note, centre)
            if kind == "note":  # a rectangle is typed into on a double-click or Enter
                self.view.setFocus()
                note.start_editing()
            return note
        return self._place(ShapeSceneItem(kind, self._pen), centre)

    def _shape_options_changed(self) -> None:
        for it in self._selected():
            if isinstance(it, (ShapeSceneItem, NoteSceneItem)) and it.kind == "rectangle":
                it.set_options(replace(it.options, fill=self.fill_check.isChecked()))
            elif isinstance(it, ShapeSceneItem) and it.kind == "arrow":
                it.set_options(replace(it.options, double=self.double_check.isChecked()))

    def add_ink_text(self, text: str, size: float = INK_SIZE,
                     style: InkStyle | None = None) -> InkSceneItem | None:
        item = InkSceneItem(text, size, self._pen, style or self._ink_style)  # a few ms
        if item.skipped:
            self.show_message(msg.INK_SKIPPED.format(chars=msg.char_list(item.skipped)),
                              error=False)
        if not item.ready():
            return None
        item.shown_skipped = list(item.skipped)
        self._wire_ink(item)
        return self._place(item)

    def _wire_ink(self, item: InkSceneItem) -> None:
        item.changed.connect(lambda: self._ink_changed(item))
        item.style_edited.connect(self._remember_style)
        item.rendered.connect(lambda: self._ink_rendered(item))
        item.editing_changed.connect(lambda on: self._ink_editing(item, on))

    def _remember_style(self, style: InkStyle) -> None:
        self._ink_style = style

    def _ink_changed(self, item: InkSceneItem) -> None:
        if self.format_bar.item is item:
            self.format_bar.sync()
            self.format_bar.place()

    def _ink_rendered(self, item: InkSceneItem) -> None:
        if not shiboken6.isValid(self):
            return
        if item.skipped and item.skipped != item.shown_skipped:
            self.show_message(msg.INK_SKIPPED.format(chars=msg.char_list(item.skipped)),
                              error=False)
        item.shown_skipped = list(item.skipped)
        self._update_actions()

    def _ink_editing(self, item: InkSceneItem, on: bool) -> None:
        if isinstance(item, NoteSceneItem) and item.kind == "rectangle" and item.isSelected():
            self._selection_changed()  # its format bar comes and goes with the typing
        if not on:  # an emptied box goes once its deselection (if any) has finished
            QTimer.singleShot(0, lambda: self._drop_empty() if shiboken6.isValid(self) else None)
        self._update_actions()

    def _drop_empty(self) -> None:
        """Remove ink text boxes that ended up with nothing to draw (no longer being edited)."""
        gone = [i for i in self.placed_items()
                if isinstance(i, InkSceneItem) and not i.editing and not i.ready()]
        for it in gone:
            self.scene.removeItem(it)
        if gone:
            self._update_actions()

    def _refocus_text(self) -> None:
        """After a format bar button: keys go back to the box being edited."""
        it = self.format_bar.item
        if it is not None and it.editing:
            self.view.setFocus()
            it.setFocus()

    def _format_style(self, style: InkStyle) -> None:
        it = self.format_bar.item
        if it is None:
            return
        self._ink_style = style
        it.set_style(style)

    def _format_size(self, size: float) -> None:
        it = self.format_bar.item
        if it is not None:
            it.set_font_size(size)

    def _format_pen(self, pen: PenStyle) -> None:
        it = self.format_bar.item
        if it is None:
            return
        self._pen = pen
        it.pen = pen
        it.update()
        for w, v in ((self.tool_combo, pen.tool), (self.color_combo, pen.color)):
            w.blockSignals(True)
            w.setCurrentText(v)
            w.blockSignals(False)
        self.thick_spin.blockSignals(True)
        self.thick_spin.setValue(pen.thickness)
        self.thick_spin.blockSignals(False)

    def remove_selected(self) -> None:
        for it in self._selected():
            self.scene.removeItem(it)
        self._update_actions()

    def _selection_changed(self) -> None:
        if not (shiboken6.isValid(self) and shiboken6.isValid(self.scene)):
            return  # the scene emits this while it is being destroyed
        sel = self._selected()
        if len(sel) == 1:
            it = sel[0]
            for w, v in ((self.tool_combo, it.pen.tool), (self.color_combo, it.pen.color)):
                w.blockSignals(True)
                w.setCurrentText(v)
                w.blockSignals(False)
            self.thick_spin.blockSignals(True)
            self.thick_spin.setValue(it.pen.thickness)
            self.thick_spin.blockSignals(False)
            if isinstance(it, ImageSceneItem):
                for w, v in ((self.spacing_spin, it.settings.spacing),
                             (self.contrast_spin, it.settings.contrast)):
                    w.blockSignals(True)
                    w.setValue(v)
                    w.blockSignals(False)
                self.cross_check.blockSignals(True)
                self.cross_check.setChecked(it.settings.crosshatch)
                self.cross_check.blockSignals(False)
                for w, v in ((self.invert_check, it.settings.invert),
                             (self.sharp_check, it.settings.sharp_text)):
                    w.blockSignals(True)
                    w.setChecked(v)
                    w.blockSignals(False)
                for w, v in ((self.detail_spin, it.settings.detail),
                             (self.threshold_spin, it.settings.threshold)):
                    w.blockSignals(True)
                    w.setValue(v)
                    w.blockSignals(False)
                if it.settings.style in STYLES:  # AUTO: shown once the first job decides
                    self.style_seg.set_index(STYLES.index(it.settings.style))
        pics = [i for i in sel if isinstance(i, ImageSceneItem)]
        style = pics[0].settings.style if pics else SHADED
        self._show_picture_controls(bool(pics), style)
        kinds = {i.kind: i for i in sel if isinstance(i, (ShapeSceneItem, NoteSceneItem))}
        for w, kind, flag in ((self.fill_check, "rectangle", "fill"),
                              (self.double_check, "arrow", "double")):
            w.setVisible(kind in kinds)
            if kind in kinds:
                w.blockSignals(True)
                w.setChecked(getattr(kinds[kind].options, flag))
                w.blockSignals(False)
        self.shape_controls.setVisible("rectangle" in kinds or "arrow" in kinds)
        ink = sel[0] if len(sel) == 1 and isinstance(sel[0], InkSceneItem) \
            and sel[0].bar_wanted else None
        self.pen_controls.setVisible(ink is None)  # an ink text box has them on its format bar
        self.format_bar.show_item(ink)
        self._update_actions()

    def _pen_changed(self) -> None:
        self._pen = PenStyle(self.tool_combo.currentText(),  # type: ignore[arg-type]
                             self.color_combo.currentText(),  # type: ignore[arg-type]
                             self.thick_spin.value())
        for it in self._selected():
            it.pen = self._pen
            it.update()
        self._reshade.start()  # a picture's preview widths follow the pen thickness

    # -- send / undo ----------------------------------------------------------
    def _warnings_ok(self, items: list[PlacedItem]) -> bool:
        warnings = []
        if any(isinstance(i, ImageSceneItem) and i.shaded and i.shaded.result.capped
               for i in items):
            warnings.append(msg.SHADE_PARTIAL)
        skipped: list[str] = []
        for i in items:
            if isinstance(i, InkSceneItem):
                skipped += [c for c in i.skipped if c not in skipped]
        if skipped:
            warnings.append(msg.INK_SKIPPED.format(chars=msg.char_list(skipped)))
        return not warnings or self.ask("\n\n".join(warnings + ["Send anyway?"]))

    def send(self) -> None:
        if self.busy or not self.allowed:
            return
        for it in self.placed_items():
            it.end_editing()  # renders what was typed: the send uses those strokes
        self._drop_empty()
        if not self._can_send():
            return
        if not self._warnings_ok(self.placed_items()):
            return
        # the dialog may have run while a shading finished or the tablet disconnected
        if self.busy or not self.allowed or not self._can_send():
            self.status.setText(msg.DIALOG_STALE)
            return
        # the snapshot: current geometry, pen and settings (a later re-shade can't change it)
        items = self.placed_items()
        specs = [i.send_data() for i in items]
        if not any(self._may_draw(i, s) for i, s in zip(items, specs)):
            self.show_message(msg.NOTHING_TO_SEND)
            return
        n, root, bridge = len(specs), self._root, self.bridge
        listing = [self._describe(i) for i in items]  # what the confirm sheet lists
        test_only, version = self.mode == "test-only", self._version()
        guard = firmware.acceptance_guard(version) if test_only else None

        ask = bool(self.cfg.confirm_sends)  # read on the GUI thread, at Send
        layer_hidden = [False]

        def confirm(opened: OpenedPage) -> bool:  # worker, xochitl stopped
            if opened.orientation == "landscape":  # temporary: landscape not calibrated
                raise NotEditable(msg.LANDSCAPE_OFF)  # before any build/write; xochitl restarts
            if layout[0] == frame.KEYBOARD:
                # xochitl doesn't save the scroll when it stops: find it from the live picture
                # the items were placed on, by laying the page's own strokes over it. Strokes
                # anchored to typed text move with its lines: each anchor is fitted on its own
                if picture is None:
                    raise NotEditable(msg.SCROLL_UNKNOWN)
                fits = scroll_fit.fit_references(picture, opened.data)
                summary = _fit_summary(fits)
                log.warning("keyboard placement fit: %s", summary)
                chosen = scroll_fit.choose_reference(fits, items_fx)
                if chosen is None and (measured[0] is not None or touch is not None
                                       or not any(f.get("points") for f in fits.values())):
                    # nothing usable to line up with: the scroll measured by swiping to the top
                    if measured[0] is not None:
                        scroll[0] = float(measured[0])
                        log.warning("placing by the measured scroll %s", measured[0])
                    elif touch is not None:
                        raise NeedsMeasuring(msg.SCROLL_NO_STROKES)  # measure, then again
                    else:
                        raise NotEditable(msg.SCROLL_NO_STROKES)
                elif chosen is None:
                    _save_fit_debug(picture, opened.data, summary)
                    raise NotEditable(msg.SCROLL_UNKNOWN)
                else:
                    key, fit = chosen
                    if key is None:  # unanchored strokes line up: absolute lines, as before
                        scroll[0] = fit["scroll"]
                    else:  # in a new group anchored like the nearest confident anchor's groups
                        anchor[0] = (key, fit["const"])
                        log.warning("placing by text anchor %s at frame x %s", key,
                                    fit["const"])
                    # every keyboard-layout placement is kept locally (newest 3) to diagnose
                    _save_fit_debug(picture, opened.data,
                                    {"placed": True, "reference": str(key),
                                     "fit": {k: v for k, v in fit.items()
                                             if isinstance(v, (int, float, str, type(None)))},
                                     "items_fx": [float(x) for x in items_fx]
                                     if items_fx is not None else None,
                                     "fits": summary})
            if test_only and not firmware.is_acceptance_name(opened.name):
                raise firmware.NotTestNotebook(firmware.testing_banner(version))
            hidden = pagemod.load(opened.data).rmdesk_layer_hidden()
            layer_hidden[0] = hidden
            if not ask:  # clicking Send was the confirmation (the setting asks first)
                return True
            text = msg.confirm_text(n, opened, hidden)
            if measured[0] is not None:
                text += "\n\n" + msg.MEASURED_NOTE
            return bridge.confirm(text, listing)

        shaded: list = []  # one ShadeResult per picture spec, made before xochitl stops
        layout = [frame.PORTRAIT_UI]  # set by op() from the tablet, before xochitl stops
        seen_layout = [None]  # what op() read, for the panel's cached layout
        scroll = [0.0]  # keyboard layout: set by confirm() from the live picture
        anchor = [None]  # keyboard layout on text-anchored strokes: (anchor_id, frame-x C)
        measured = [None]  # a typed-only page: its scroll, measured by swiping it to the top
        items_fx = _frame_x_range(specs)  # where the items are, to pick the nearest anchor
        live = getattr(self.screen, "frame", None)  # what the items were placed on
        picture = None if live is None else live.gray.copy()

        def build_page(opened: OpenedPage) -> tuple[bytes, int]:  # worker, after Yes
            # no picture I/O or shading here: the tablet screen is dark until this returns
            lines = []
            results = iter(shaded)
            p = pagemod.load(opened.data)
            how = {"layout": layout[0], "scroll": scroll[0]}
            if anchor[0] is not None:
                anchor_id, const = anchor[0]
                template = p.anchor_template(anchor_id)
                if template is None:  # fitted from this page's groups: cannot happen
                    raise NotEditable(msg.SCROLL_UNKNOWN)
                how = {"layout": layout[0],
                       "anchored": frame.Anchored(const, template.anchor_origin_x.value)}
            for s in specs:
                if isinstance(s, ShadeSpec):
                    lines += build.frame_shade_to_lines(s.model, next(results), s.model.pen,
                                                        opened.orientation, **how)
                else:
                    lines += build.frame_to_lines(*s, opened.orientation, **how)
            if not lines:
                raise msg.NothingToSend()
            if anchor[0] is not None:
                p.append_anchored_group(lines, anchor[0][0])
            else:
                p.append_to_rmdesk_layer(lines)
            return p.to_bytes(), len(lines)

        def op(dev):
            # the Type Folio switches the tablet to its keyboard layout (the page turned,
            # 1:1, scrolled): the frame maps to the page differently there
            layout[0] = tablet_layout(dev)  # what the live picture shows: before anything stops
            seen_layout[0] = layout[0]
            # pictures are shaded again for the snapshot's geometry (never a cached preview),
            # before anything stops: a missing or unreadable picture fails with the tablet as is
            try:
                wide = layout[0] == frame.KEYBOARD  # parts past the frame's ends are kept
                shaded[:] = [shade_job.shade(s, wide) for s in specs if isinstance(s, ShadeSpec)]
            except Exception as e:
                raise msg.PictureFailed(str(e)) from e
            return safety.send_to_open_page(dev, build_page, confirm, root=root, guard=guard)

        def done(res: tuple[safety.SendResult, OpenedPage]):
            result, opened = res
            self._end()
            self.set_layout(seen_layout[0])
            self.conn.clear_pending()  # send_to_open_page restarted xochitl
            log.warning("sent to %s/%s; backup at %s", opened.notebook_id, opened.page_id,
                        result.backup_dir)
            # page-scoped undo needs the page id and the sha256 of the bytes written
            self.cfg.last_send = {"notebook_id": opened.notebook_id, "name": opened.name,
                                  "page_id": opened.page_id,
                                  "page_sha256": result.page_sha256,
                                  "backup_dir": str(result.backup_dir)}
            self._save()
            for it in self.placed_items():
                self.scene.removeItem(it)
            sent = msg.SENT_TO.format(name=opened.name or "the notebook",
                                      page=opened.page_index + 1, total=opened.page_count)
            if layer_hidden[0]:
                sent += msg.SENT_HIDDEN
            self.show_message(sent, error=False)
            self._update_actions()

        def failed(e: Exception):
            if isinstance(e, NeedsMeasuring) and measured[0] is None and touch is not None:
                # no handwriting after all (the hint was stale): nothing was written and
                # xochitl is back, so measure the scroll now and send again
                self._measure(picture, touch, go)
                return
            self._end()
            if seen_layout[0] is not None:
                self.set_layout(seen_layout[0])
            if safety.start_error_of(e) is not None:  # the screen didn't come back: say so
                self.show_message(*msg.describe_send_error(e))
                return
            if isinstance(e, safety.Cancelled):  # No, or no answer in time: not an error
                self.status.setText(msg.NOT_SENT)
                return
            if isinstance(e, NotEditable):  # the open page can't be written: say why
                self.show_message(e.reason)
                return
            if isinstance(e, safety.SendFailed):
                log.warning("send failed (restored=%s); backup at %s: %s",
                            e.restored, e.backup_dir, e)
            self.show_message(*msg.describe_send_error(e))

        def go(scroll_measured: int | None = None) -> None:
            measured[0] = scroll_measured
            scroll[0], anchor[0] = 0.0, None
            if not self._claim_job(SEND_JOB):
                self._end()
                return
            self.show_message("")
            self._begin(msg.SENDING)
            self._on_device(op, done, failed, release=self.conn.end_job)

        # keyboard layout, a page with typed text but no handwriting: nothing to line up, so
        # rmdesk measures the scroll by swiping the page to its top (input only, before the
        # job slot and before xochitl stops), when it may send touch (Control)
        touch = self._probe_touch() if picture is not None else None
        if touch is None:
            go()
            return

        def checked(need: bool) -> None:
            if need:
                self._measure(picture, touch, go)
            else:
                go()
        self.show_message("")
        self._begin(msg.CHECKING_PAGE)
        self._on_device(lambda dev: needs_measuring(dev, picture, items_fx), checked,
                        lambda e: go(), release=lambda: None)

    def _probe_touch(self):
        """Control's touch (RemoteControl.borrow_touch) if rmdesk may send touch, else None."""
        if not getattr(self.screen, "control_enabled", False):
            return None
        return getattr(self.screen, "control", None)

    def _measure(self, picture, touch, then: Callable[[int], None]) -> None:
        """Swipe the page to its top and measure how far `picture` was scrolled (ScrollProbe),
        then then(scroll); a refusal is said in the message line and nothing is sent."""
        probe = self.probe_factory(picture, touch, self.screen, lambda: self.conn.current_job,
                                   parent=self)
        self._probe = probe
        self._begin(msg.MEASURING)
        probe.progress.connect(self.status.setText)

        def finished(scroll: int) -> None:
            self._probe = None
            probe.deleteLater()
            then(scroll)

        def failed(text: str, details: str) -> None:
            self._probe = None
            probe.deleteLater()
            self._end()
            self.show_message(text, details)
        probe.finished.connect(finished)
        probe.failed.connect(failed)
        probe.start()

    @staticmethod
    def _describe(item: PlacedItem) -> str:
        if isinstance(item, ImageSceneItem):
            style = STYLE_LABELS.get(item.settings.style, STYLE_LABELS[SHADED])
            return f"Picture ({style}): {item.source.name}"
        if isinstance(item, ShapeSceneItem):
            if item.kind == "arrow" and item.options.double:
                return "Double arrow"
            return SHAPE_LABELS[item.kind]
        if isinstance(item, InkSceneItem):
            text = " ".join(item.text.split())
            quoted = "\u201c" + (text if len(text) <= 40 else text[:39] + "\u2026") + "\u201d"
            if isinstance(item, NoteSceneItem):
                label = SHAPE_LABELS[item.kind]
                return f"{label}: {quoted}" if text else label
            return "Ink text: " + quoted
        return "Item"

    def _may_draw(self, item: PlacedItem, spec) -> bool:
        """Whether the item may put something on the page (a picture whose preview is out
        of date is shaded again by the send, which then decides)."""
        if isinstance(spec, ShadeSpec):
            return not item.current() or bool(item.preview_strokes())
        return build.has_lines_on_page(*spec, layout=self.layout or frame.PORTRAIT_UI)

    def undo(self) -> None:
        last = self.cfg.last_send
        if self.busy or not self.allowed or not (last or {}).get("backup_dir"):
            return
        if not self._undo_ok(last):
            self.show_message(firmware.testing_banner(self._version()))
            return
        name = self._last_send_name()
        if not self.ask(msg.UNDO_CONFIRM.format(name=name)):
            return
        if self.busy or not self.allowed or self.cfg.last_send is not last \
                or not self._undo_ok(last):
            self.status.setText(msg.DIALOG_STALE)
            return
        snapshot = Config(last_send=dict(last))  # undo_last clears it; keep the GUI's copy
        root = self._root
        guard = (firmware.undo_acceptance_guard(str(last.get("notebook_id", "")), self._version())
                 if self.mode == "test-only" else None)

        def finished():
            self.cfg.last_send = None
            self._save()
            self._update_actions()

        def done(res: safety.RestoreResult):
            self._end()
            self.conn.clear_pending()  # undo_last restarted xochitl
            log.warning("undid the last send to %s; backup before the undo at %s",
                        last.get("notebook_id"), res.safety_backup)
            self.show_message(msg.UNDONE.format(name=name, backup=res.safety_backup), error=False)
            finished()

        def failed(e: Exception):
            self._end()
            if isinstance(e, safety.UndoFailed):
                log.warning("undo of %s failed; backup before the undo at %s: %s",
                            last.get("notebook_id"), e.safety_backup, e)
            self.show_message(*msg.describe_undo_error(e))
            if isinstance(e, safety.UndoFailed) and msg.undo_completed(e):
                finished()  # every file was restored: the send is undone

        if not self._claim_job(UNDO_JOB):
            return
        self.show_message("")
        self._begin(msg.UNDOING)
        self._on_device(lambda dev: safety.undo_last(dev, snapshot, root=root, guard=guard),
                        done, failed, release=self.conn.end_job)
