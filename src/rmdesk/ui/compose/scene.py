"""Graphics items for composing on the live view: frame-pixel scene, movable/resizable items."""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QGuiApplication, QImageReader, QPainter, QPainterPath,
                           QPen, QPolygonF)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject

from rmdesk.compose.inktext import (LETTER_SPACING_DEFAULT, LINE_SPACING_DEFAULT, line_pitch,
                                    text_to_polylines)
from rmdesk.compose.items import Box, ImageItem, InkTextItem, PenStyle, ShapeItem
from rmdesk.compose.place import Polyline, normalise_deg, rotate_polylines, snap_angle
from rmdesk.compose.shade import WIDTH_UNITS_PER_PX
from rmdesk.compose.shapes import (DEFAULT_SIZE, LINE_H, NOTE_PAD, ShapeOptions, note_text_rect,
                                   shape_polylines, text_gaps)
from rmdesk.ui.compose.shade_job import PreviewStroke, Shaded, ShadeSettings, ShadeSpec

ACCENT = QColor("#d01f2f")
SELECTION = QColor(208, 31, 47, 64)  # the accent, translucent: behind selected characters
HANDLE = 28.0  # frame pixels
MIN_SIZE = 20.0
ROT_OFFSET = 24.0  # rotate handle centre above the top edge, frame pixels
ROT_RADIUS = 10.0
ROT_HIT_PX = 12.0  # the rotate handle's hit radius on screen is never smaller than this
SHADING = "Shading…"
BLANK = "Nothing to draw"
PLACEHOLDER = "Type…"
RENDER_MS = 150  # ink text: strokes follow typing after this pause
EMPTY_W, EMPTY_H = 3.0, 1.3  # an empty ink text box, in font sizes
MIN_FONT = 4.0
SIZE_MIN, SIZE_MAX, SIZE_STEP = 8, 400, 2  # the format bar's Size control; Ctrl+Plus / Ctrl+Minus
UNDO_STEPS = 100  # per ink text box: text + caret + selection snapshots
NEWLINE_W = 0.3  # a selected line break is shown this wide, in font sizes


def picture_size(path: Path) -> tuple[int, int] | None:
    """(width, height) of a picture from its header only (cheap on the GUI thread), or None."""
    if path.suffix.lower() == ".svg":
        r = QSvgRenderer(str(path))
        size = r.defaultSize() if r.isValid() else None
    else:
        size = QImageReader(str(path)).size()
    if size is None or size.width() < 1 or size.height() < 1:
        return None
    return size.width(), size.height()


class PlacedItem(QGraphicsObject):
    """Something placed over the live view; shows its strokes in red until sent.

    Position (scene pos) + size are the item's Box in frame pixels (1404x1872,
    unrotated), which are also the page's box coordinates. Drag the
    body to move, the bottom-right corner to resize, the circle above the top
    edge to rotate (around the centre; 15 degree steps, Shift for free). The strokes
    are rotated with compose.place.rotate_polylines, the same function the send uses;
    the outline and handles are drawn in the item's rotated frame.
    """

    editing = False  # ink text being typed into (keys go to the text)

    def __init__(self, w: float, h: float, pen: PenStyle):
        super().__init__()
        self.w, self.h = float(w), float(h)
        self.pen = pen
        self._resizing = False
        self._rotating = False
        self.rotation_deg = 0.0
        self.user_resized = False
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)

    # -- geometry --
    def box(self) -> Box:
        p = self.pos()
        return Box(p.x(), p.y(), self.w, self.h)

    def set_size(self, w: float, h: float) -> None:
        self.prepareGeometryChange()
        self.w, self.h = max(MIN_SIZE, float(w)), max(MIN_SIZE, float(h))
        self.update()

    def centre(self) -> QPointF:
        """Item centre in local coordinates (the rotation pivot)."""
        return QPointF(self.w / 2, self.h / 2)

    def set_rotation(self, deg: float) -> None:
        self.prepareGeometryChange()
        self.rotation_deg = normalise_deg(deg)
        self.update()

    def rotate_by(self, deg: float) -> None:
        self.set_rotation(self.rotation_deg + deg)

    def _rot_point(self, x: float, y: float, deg: float) -> QPointF:
        """(x, y) rotated by `deg` around the centre (same convention as rotate_polylines)."""
        pl = rotate_polylines([[(x, y)]], self.w / 2, self.h / 2, deg)[0][0]
        return QPointF(*pl)

    def to_local_frame(self, p: QPointF) -> QPointF:
        """A point in item coordinates -> the item's own unrotated frame."""
        return self._rot_point(p.x(), p.y(), -self.rotation_deg)

    def _px(self, n: float) -> float:
        """`n` screen pixels in frame pixels (the scene is scaled to fit the view)."""
        sc = self.scene()
        views = sc.views() if sc is not None else []
        if not views:
            return n
        t = views[0].transform()
        k = math.hypot(t.m11(), t.m12())
        return n / k if k > 1e-6 else n

    def hit_radius(self) -> float:
        """Rotate handle hit radius, frame pixels: >= ROT_HIT_PX on screen."""
        return max(ROT_RADIUS + 4, self._px(ROT_HIT_PX))

    def rotate_handle_centre(self) -> QPointF:
        """Handle centre in the local frame before rotation. Normally above the top edge; moved
        below the bottom edge when that would leave the scene, and clamped into the scene as a
        last resort, so it can always be reached."""
        up = QPointF(self.w / 2, -ROT_OFFSET)
        sc = self.scene()
        if sc is None:
            return up
        r = sc.sceneRect().adjusted(self.hit_radius(), self.hit_radius(),
                                    -self.hit_radius(), -self.hit_radius())
        if r.width() <= 0 or r.height() <= 0:
            return up

        def inside(p: QPointF) -> bool:
            c = self.mapToScene(self._rot_point(p.x(), p.y(), self.rotation_deg))
            return r.contains(c)

        if inside(up):
            return up
        down = QPointF(self.w / 2, self.h + ROT_OFFSET)
        if inside(down):
            return down
        c = self.mapToScene(self._rot_point(up.x(), up.y(), self.rotation_deg))
        c = QPointF(min(max(c.x(), r.left()), r.right()), min(max(c.y(), r.top()), r.bottom()))
        return self._rot_point_inv(self.mapFromScene(c))

    def _rot_point_inv(self, p: QPointF) -> QPointF:
        """An item-coordinate point -> local frame before rotation."""
        return self._rot_point(p.x(), p.y(), -self.rotation_deg)

    def boundingRect(self) -> QRectF:
        r = math.hypot(self.w, self.h) / 2 + ROT_OFFSET + self.hit_radius() + 4
        return QRectF(self.w / 2 - r, self.h / 2 - r, 2 * r, 2 * r)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        corners = [self._rot_point(x, y, self.rotation_deg)
                   for x, y in ((-2, -2), (self.w + 2, -2), (self.w + 2, self.h + 2),
                                (-2, self.h + 2))]
        path.addPolygon(QPolygonF(corners))
        path.closeSubpath()
        if self.isSelected():
            hc = self.rotate_handle_centre()
            c = self._rot_point(hc.x(), hc.y(), self.rotation_deg)
            path.addEllipse(c, self.hit_radius(), self.hit_radius())
        return path

    def handle_rect(self) -> QRectF:
        return QRectF(self.w - HANDLE, self.h - HANDLE, HANDLE, HANDLE)  # local frame

    # -- strokes, in item-local frame pixels --
    def local_polylines(self) -> list[Polyline]:
        raise NotImplementedError

    def rotated_polylines(self) -> list[Polyline]:
        """local_polylines() turned by the item's rotation: exactly what is sent."""
        return rotate_polylines(self.local_polylines(), self.w / 2, self.h / 2, self.rotation_deg)

    def ready(self) -> bool:
        return True

    def placeholder(self) -> str:
        """Text shown in the box instead of strokes (none by default)."""
        return ""

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        for pl in self.rotated_polylines():
            if len(pl) < 2:
                continue
            path.moveTo(QPointF(*pl[0]))
            for pt in pl[1:]:
                path.lineTo(QPointF(*pt))
        pen = QPen(ACCENT, max(1.0, 3.0 * self.pen.thickness))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawPath(path)
        outline = QPen(ACCENT if self.isSelected() else QColor("#9a9a9a"), 0, Qt.PenStyle.DashLine)
        painter.setPen(outline)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.save()  # outline and handles live in the rotated frame
        painter.translate(self.w / 2, self.h / 2)
        painter.rotate(self.rotation_deg)
        painter.translate(-self.w / 2, -self.h / 2)
        painter.drawRect(QRectF(0, 0, self.w, self.h))
        if self.placeholder():
            painter.drawText(QRectF(0, 0, self.w, self.h), Qt.AlignmentFlag.AlignCenter,
                             self.placeholder())
        if self.isSelected():
            painter.fillRect(self.handle_rect(), ACCENT)
            hc = self.rotate_handle_centre()
            painter.setPen(QPen(ACCENT, 0))
            rad = max(ROT_RADIUS, self._px(7))
            edge_y = self.h if hc.y() > self.h / 2 else 0.0
            painter.drawLine(QPointF(self.w / 2, edge_y), hc)
            painter.setBrush(ACCENT)
            painter.drawEllipse(hc, rad, rad)
        painter.restore()

    # -- mouse: corner handle resizes, top circle rotates, the rest moves --
    def _on_rotate_handle(self, pos: QPointF) -> bool:
        h = self.rotate_handle_centre()
        hc = self._rot_point(h.x(), h.y(), self.rotation_deg)
        return math.hypot(pos.x() - hc.x(), pos.y() - hc.y()) <= self.hit_radius()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            if self.isSelected() and self._on_rotate_handle(e.pos()):
                self._rotating = True
                e.accept()
                return
            if self.handle_rect().contains(self.to_local_frame(e.pos())):
                self._resizing = True
                self.setSelected(True)
                e.accept()
                return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._rotating:
            c = self.mapToScene(self.centre())
            d = e.scenePos() - c
            if d.x() or d.y():
                free = bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                self.set_rotation(snap_angle(math.degrees(math.atan2(d.x(), -d.y())), free))
            e.accept()
            return
        if self._resizing:
            q = self.to_local_frame(e.pos())
            tl = self.mapToScene(self._rot_point(0, 0, self.rotation_deg))  # fixed corner
            self.resize_to(q.x(), q.y())
            self.setPos(self.pos() + tl - self.mapToScene(self._rot_point(0, 0, self.rotation_deg)))
            self.user_resized = True
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._rotating or self._resizing:
            self._rotating = self._resizing = False
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def resize_to(self, w: float, h: float) -> None:
        self.set_size(w, h)

    def end_editing(self) -> None:
        """Stop typing into the item (only ink text edits)."""

    def wants_key(self, e) -> bool:
        """While editing: the key belongs to the text, not to a shortcut."""
        return False


class ImageSceneItem(PlacedItem):
    """A picture, shaded or as line art (settings.style). `source` is the user's file;
    `raster` the file drawn from (an SVG's PNG).

    `shaded` is the newest shading result and `shaded_key` the geometry, settings and
    pen thickness it was made for (the preview carries the sent widths) (a ShadeResult only covers that box and rotation); the panel
    re-shades whenever `key()` moves away from it. `geometry_changed` fires on every
    move, resize and rotation.
    """

    geometry_changed = Signal()

    def __init__(self, source: Path, w: float, h: float, pen: PenStyle,
                 settings: ShadeSettings = ShadeSettings()):
        super().__init__(w, h, pen)
        self.source = source
        self.raster: Path | None = None
        self.settings = settings
        self.shaded: Shaded | None = None
        self.shaded_key: tuple | None = None
        self.pending_key: tuple | None = None
        self.shade_gen = 0
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        # thousands of segments: paint once, reuse until update()
        self.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)

    def key(self) -> tuple:
        b = self.box()
        # the pen's tool too: line-art widths are the tool's (shading ignores it)
        return (b.x, b.y, b.w, b.h, self.rotation_deg, self.pen.thickness, self.pen.tool,
                self.settings)

    def current(self) -> bool:
        """The preview was shaded for the item as it is now."""
        return self.shaded is not None and self.shaded_key == self.key()

    def ready(self) -> bool:
        return self.shaded is not None and not self.shaded.blank

    def placeholder(self) -> str:
        if self.shaded is None:
            return SHADING
        return BLANK if self.shaded.blank else ""

    def set_shaded(self, shaded: Shaded, key: tuple) -> None:
        self.shaded, self.shaded_key = shaded, key
        self.raster = shaded.raster
        self.update()

    def set_size(self, w: float, h: float) -> None:
        super().set_size(w, h)
        self.geometry_changed.emit()

    def set_rotation(self, deg: float) -> None:
        super().set_rotation(deg)
        self.geometry_changed.emit()

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.geometry_changed.emit()
        return super().itemChange(change, value)

    def model(self) -> ImageItem:
        return ImageItem(path=self.raster or self.source, box=self.box(), pen=self.pen,
                         rotation=self.rotation_deg)

    def send_data(self) -> ShadeSpec:
        """The snapshot the send shades from: current box, rotation, pen and settings."""
        return ShadeSpec(self.model(), self.settings)

    def local_polylines(self) -> list[Polyline]:
        return []  # drawn from the shading preview instead

    def preview_strokes(self) -> list[PreviewStroke]:
        """Item-local (x, y, width) of the strokes shown (and sent, while current())."""
        return self.shaded.preview if self.shaded is not None else []

    def paint(self, painter: QPainter, option, widget=None) -> None:
        if self.shaded is not None and self.shaded.paths:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            for units, path in self.shaded.paths:
                # units are the sent widths (thickness applied, clamped): no extra scaling
                pen = QPen(ACCENT, units / WIDTH_UNITS_PER_PX)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(pen)
                painter.drawPath(path)
        super().paint(painter, option, widget)


@dataclass(frozen=True)
class InkStyle:
    """One style for a whole ink text box (see compose.inktext)."""
    font: str = "sans"
    bold: bool = False
    italic: bool = False
    align: str = "left"
    line_spacing: float = LINE_SPACING_DEFAULT  # x the default line pitch (1.0 = 1.4 em)
    letter_spacing: float = LETTER_SPACING_DEFAULT  # extra em after each character


def _char_class(ch: str) -> int:
    if ch.isalnum() or ch == "_":
        return 0
    return 1 if ch.isspace() else 2


class InkSceneItem(PlacedItem):
    """Hand-lettered text, edited in place on the canvas.

    The strokes come from compose.inktext.text_to_polylines (the same call the send uses)
    rendered at `base_size`; they scale linearly with the font size, so resizing the box
    (aspect locked) changes `size` and scales them. While `editing`, keys typed into the
    box change `text` at once and the strokes follow after RENDER_MS (debounced); the
    send flushes a pending render first. `changed` fires on every text, style, size and
    geometry change, `rendered` when new strokes are ready, `editing_changed` when editing
    starts or ends.

    While editing there is a selection from `anchor` to `cursor` (Shift+keys, Ctrl+A, a mouse
    drag, a double-clicked word), the clipboard (Ctrl+C/X/V, plain text only) and an undo
    stack of (text, cursor, anchor) snapshots (Ctrl+Z, Ctrl+Shift+Z / Ctrl+Y). A drag inside
    a box being edited selects text instead of moving the box.
    """

    changed = Signal()
    rendered = Signal()
    editing_changed = Signal(bool)
    bar_wanted = True  # the format bar is shown while this item is the one selected
    hint_idle = True  # an empty box shows its placeholder also when not being edited
    style_edited = Signal(object)  # a keyboard shortcut changed the style (an InkStyle)

    def __init__(self, text: str, size: float, pen: PenStyle, style: InkStyle = InkStyle(),
                 box: tuple[float, float] = (1.0, 1.0)):
        super().__init__(*box, pen)
        self.text = text
        self.style = style
        self.size = float(size)
        self.base_size = float(size)
        self.base_lines: list[Polyline] = []
        self.stops: list[list[float]] = [[0.0]]
        self.row_starts: list[int] = [0]  # where each drawn row starts in the rendered text
        self._rendered_text = text
        self.skipped: list[str] = []
        self.shown_skipped: list[str] | None = None  # the skipped characters last reported
        self.editing = False
        self.editable = True
        self.cursor = len(text)
        self.anchor: int | None = None  # the other end of the selection, None: no selection
        self._undo: list[tuple[str, int, int | None]] = []
        self._redo: list[tuple[str, int, int | None]] = []
        self._last_edit: str | None = None  # "type" while a typed word goes into one step
        self._selecting = False
        self._dirty = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(RENDER_MS)
        self._timer.timeout.connect(self.render)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self._render(anchor=False)

    # -- rendering --
    def _render(self, anchor: bool = True) -> None:
        st = self.style
        extra = {} if self._wrap_width() is None else {"max_width": self._wrap_width()}
        res = text_to_polylines(self.text, self.size, st.font, st.bold, st.italic, st.align,
                                line_spacing=st.line_spacing, letter_spacing=st.letter_spacing,
                                **extra)
        self.base_size = self.size
        self.base_lines, self.skipped, self.stops = res.lines, res.skipped, res.stops
        self.row_starts, self._rendered_text = res.row_starts, self.text
        self._dirty = False
        self._set_box(*self._text_box(res.lines), anchor)

    def _wrap_width(self) -> float | None:
        """Rows are wrapped to this width (pixels); None: only at line breaks."""
        return None

    def _text_origin(self) -> QPointF:
        """Where the text's (0, 0) is in the item's local frame."""
        return QPointF(0.0, 0.0)

    def _text_box(self, lines: list[Polyline]) -> tuple[float, float]:
        """The box size for freshly rendered strokes: the text's extent."""
        xs = [x for pl in lines for x, _ in pl]
        ys = [y for pl in lines for _, y in pl]
        if xs:
            return max(max(xs), 1.0), max(max(ys), 1.0)
        return EMPTY_W * self.size, EMPTY_H * self.size  # nothing drawn yet: a box to type into

    def _anchor_x(self) -> float:
        return {"center": 0.5, "right": 1.0}.get(self.style.align, 0.0)

    def _set_box(self, w: float, h: float, anchor: bool = True) -> None:
        """New box size; the top edge point under the alignment (left/centre/right) stays put
        on the page, also when the box is rotated."""
        ax = self._anchor_x()
        before = self.mapToScene(self._rot_point(ax * self.w, 0, self.rotation_deg))
        self.prepareGeometryChange()
        self.w, self.h = float(w), float(h)
        if anchor:
            after = self.mapToScene(self._rot_point(ax * self.w, 0, self.rotation_deg))
            self.setPos(self.pos() + before - after)
        self.update()

    def render(self) -> None:
        """Strokes for the text and style now (the debounced render, or at once)."""
        self._timer.stop()
        self._render()
        self.rendered.emit()
        self.changed.emit()

    def flush(self) -> None:
        """Render now if typing left the strokes behind the text."""
        if self._dirty:
            self.render()

    def _schedule(self) -> None:
        self._dirty = True
        self._timer.start()
        self.update()
        self.changed.emit()

    def set_text(self, text: str, now: bool = False) -> None:
        self.text = text
        self.cursor = min(self.cursor, len(text))
        if self.anchor is not None:
            self.anchor = min(self.anchor, len(text))
        if now:
            self.render()
        else:
            self._schedule()

    def set_style(self, style: InkStyle) -> None:
        if style != self.style:
            self.style = style
            self.render()

    def set_font_size(self, size: float) -> None:
        """Change the font size, keeping the top edge point under the alignment in place."""
        size = max(float(size), MIN_FONT)
        k = size / self.base_size
        self.size = size
        bw, bh = self._base_box()
        self._set_box(bw * k, bh * k)
        self.changed.emit()

    def _base_box(self) -> tuple[float, float]:
        xs = [x for pl in self.base_lines for x, _ in pl]
        ys = [y for pl in self.base_lines for _, y in pl]
        if xs:
            return max(max(xs), 1.0), max(max(ys), 1.0)
        return EMPTY_W * self.base_size, EMPTY_H * self.base_size

    def resize_to(self, w: float, h: float) -> None:
        bw, bh = self._base_box()
        k = max(h / bh, w / bw, MIN_SIZE / bh)
        self.size = self.base_size * k
        self.prepareGeometryChange()
        self.w, self.h = bw * k, bh * k
        self.update()
        self.changed.emit()

    def set_size(self, w: float, h: float) -> None:
        self.resize_to(w, h)

    def set_rotation(self, deg: float) -> None:
        super().set_rotation(deg)
        self.changed.emit()

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.changed.emit()
        elif change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged and not value:
            self.end_editing()
        return super().itemChange(change, value)

    def _scaled(self) -> list[Polyline]:
        k = self.size / self.base_size
        return [[(x * k, y * k) for x, y in pl] for pl in self.base_lines]

    def ready(self) -> bool:
        return bool(self.base_lines)

    def model(self) -> InkTextItem:
        st = self.style
        return InkTextItem(text=self.text, box=self.box(), size=self.size, pen=self.pen,
                           rotation=self.rotation_deg, font=st.font, bold=st.bold,
                           italic=st.italic, align=st.align, line_spacing=st.line_spacing,
                           letter_spacing=st.letter_spacing)

    def send_data(self) -> tuple[InkTextItem, list[Polyline], None]:
        self.flush()
        return self.model(), self._scaled(), None

    def local_polylines(self) -> list[Polyline]:
        return self._scaled()

    # -- editing --
    def start_editing(self, cursor: int | None = None) -> None:
        if not self.editable:
            return
        self.cursor = len(self.text) if cursor is None else max(0, min(cursor, len(self.text)))
        self.anchor = None
        self._last_edit = None
        self.setSelected(True)
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        if not self.editing:
            self.editing = True
            self.editing_changed.emit(True)
        self.update()

    def end_editing(self) -> None:
        if not self.editing:
            return
        self.editing = False
        self.anchor = None
        self._selecting = False
        self.flush()
        self.clearFocus()
        self.update()
        self.editing_changed.emit(False)

    def _rows(self) -> list[tuple[int, int, bool]]:
        """(start, end, soft) of every drawn row: text[start:end] is its characters (without
        a line break). `soft`: the row was wrapped (it goes on in the next row). Without
        wrapping, the rows are the text's lines."""
        t = self.text
        if self._wrap_width() is not None and t == self._rendered_text and self.row_starts:
            out = []
            starts = self.row_starts
            for i, a in enumerate(starts):
                if i + 1 == len(starts):
                    out.append((a, len(t), False))
                elif t[starts[i + 1] - 1] == "\n":
                    out.append((a, starts[i + 1] - 1, False))
                else:
                    out.append((a, starts[i + 1], True))
            return out
        out, a = [], 0
        for line in t.split("\n"):
            out.append((a, a + len(line), False))
            a += len(line) + 1
        return out

    def _row_len(self, row: int) -> int:
        """Columns the caret can reach in a row by Up/Down/End (a wrapped row: before the
        character it was broken after, so the caret stays on the row)."""
        a, b, soft = self._rows()[row]
        return b - a - (1 if soft and b > a else 0)

    def _row_col(self, pos: int | None = None) -> tuple[int, int]:
        pos = self.cursor if pos is None else pos
        rows = self._rows()
        row = max((i for i, (a, _b, _s) in enumerate(rows) if a <= pos), default=0)
        return row, pos - rows[row][0]

    def _index(self, row: int, col: int) -> int:
        rows = self._rows()
        row = max(0, min(row, len(rows) - 1))
        return rows[row][0] + max(0, min(col, self._row_len(row)))

    def _pitch(self) -> float:
        return line_pitch(self.size, self.style.line_spacing)

    def _stop_x(self, row: int, col: int) -> float:
        """Caret x before column `col` of `row` (from the last render, scaled)."""
        k = self.size / self.base_size
        stops = self.stops[min(row, len(self.stops) - 1)] if self.stops else [0.0]
        return stops[max(0, min(col, len(stops) - 1))] * k

    def caret_line(self) -> tuple[QPointF, QPointF]:
        """The caret, item-local frame before rotation (from the last render, scaled)."""
        row, col = self._row_col()
        x = self._stop_x(row, col)
        top = row * self._pitch()
        o = self._text_origin()
        return (QPointF(x, top - 0.05 * self.size) + o, QPointF(x, top + 1.1 * self.size) + o)

    def cursor_at(self, p: QPointF) -> int:
        """The text position nearest a point in the item's local frame."""
        k = self.size / self.base_size
        pitch = self._pitch()
        p = p - self._text_origin()
        row = int(max(0.0, p.y()) // pitch) if pitch > 0 else 0
        row = min(row, len(self.stops) - 1)
        stops = self.stops[row]
        col = min(range(len(stops)), key=lambda i: abs(stops[i] * k - p.x()))
        return self._index(row, col)

    # -- selection --
    def selection(self) -> tuple[int, int]:
        """(start, end) of the selected text; start == end when nothing is selected."""
        a = self.cursor if self.anchor is None else self.anchor
        return min(a, self.cursor), max(a, self.cursor)

    def has_selection(self) -> bool:
        a, b = self.selection()
        return a != b

    def selected_text(self) -> str:
        a, b = self.selection()
        return self.text[a:b]

    def set_selection(self, anchor: int, cursor: int) -> None:
        n = len(self.text)
        self.anchor = max(0, min(anchor, n))
        self.cursor = max(0, min(cursor, n))
        self.update()

    def selection_rects(self) -> list[QRectF]:
        """The highlight behind the selection, one rectangle per text line, item-local frame
        before rotation (the caret's height; a selected line break shows NEWLINE_W wide)."""
        a, b = self.selection()
        if a == b:
            return []
        out = []
        r0, c0 = self._row_col(a)
        r1, c1 = self._row_col(b)
        rows = self._rows()
        pitch = self._pitch()
        o = self._text_origin()
        for row in range(r0, r1 + 1):
            first = c0 if row == r0 else 0
            start, end, soft = rows[row]
            last = c1 if row == r1 else end - start
            x0, x1 = self._stop_x(row, first), self._stop_x(row, last)
            if row < r1 and not soft:
                x1 += NEWLINE_W * self.size
            if x1 <= x0:
                continue
            top = row * pitch - 0.05 * self.size
            out.append(QRectF(x0 + o.x(), top + o.y(), x1 - x0, 1.15 * self.size))
        return out

    def word_at(self, pos: int) -> tuple[int, int]:
        """(start, end) of the word (or run of spaces, or of other characters) at `pos`."""
        t = self.text
        if not t:
            return 0, 0
        i = pos if pos < len(t) and t[pos] != "\n" else pos - 1
        if i < 0 or t[i] == "\n":
            return pos, pos
        cls = _char_class(t[i])
        a, b = i, i + 1
        while a > 0 and t[a - 1] != "\n" and _char_class(t[a - 1]) == cls:
            a -= 1
        while b < len(t) and t[b] != "\n" and _char_class(t[b]) == cls:
            b += 1
        return a, b

    # -- undo --
    def _snapshot(self) -> tuple[str, int, int | None]:
        return self.text, self.cursor, self.anchor

    def _push_undo(self, kind: str | None = None) -> None:
        """Remember the state before an edit. Consecutive typing is one step per word (the
        spaces before a word join it), consecutive Backspace/Delete one step."""
        last, self._last_edit = self._last_edit, kind
        if kind is not None and (kind == last or (kind == "type" and last == "space")):
            return
        self._undo.append(self._snapshot())
        del self._undo[:-UNDO_STEPS]
        self._redo.clear()

    def _restore(self, src: list, dst: list) -> None:
        if not src:
            return
        dst.append(self._snapshot())
        del dst[:-UNDO_STEPS]
        text, cursor, anchor = src.pop()
        self._last_edit = None
        self.cursor, self.anchor = cursor, anchor
        self.set_text(text)

    def undo(self) -> None:
        self._restore(self._undo, self._redo)

    def redo(self) -> None:
        self._restore(self._redo, self._undo)

    # -- keys --
    _NAV = (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down,
            Qt.Key.Key_Home, Qt.Key.Key_End)
    _EDIT = (Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Backspace,
             Qt.Key.Key_Delete)
    _CTRL = (Qt.Key.Key_A, Qt.Key.Key_C, Qt.Key.Key_X, Qt.Key.Key_V, Qt.Key.Key_Z,
             Qt.Key.Key_Y, Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Home, Qt.Key.Key_End,
             Qt.Key.Key_Backspace, Qt.Key.Key_Delete, Qt.Key.Key_B, Qt.Key.Key_I, Qt.Key.Key_L,
             Qt.Key.Key_E, Qt.Key.Key_R, Qt.Key.Key_Plus, Qt.Key.Key_Equal, Qt.Key.Key_Minus)
    _ALIGN_KEYS = {Qt.Key.Key_L: "left", Qt.Key.Key_E: "center", Qt.Key.Key_R: "right"}

    def wants_key(self, e) -> bool:
        """The key edits the text (so no window or app shortcut may take it while editing):
        typing, navigation (with Shift), Ctrl+A/C/X/V/Z/Y (Ctrl+Shift+Z), word and text-wide
        moves and deletes (Ctrl+arrows/Home/End/Backspace/Delete) and the formatting keys
        (Ctrl+B/I/L/E/R/Plus/=/Minus)."""
        M = Qt.KeyboardModifier
        mods = e.modifiers() & (M.ControlModifier | M.AltModifier | M.MetaModifier)
        key = e.key()
        if mods == M.ControlModifier:
            return key in self._CTRL
        if mods:
            return False
        if key in self._NAV or key in self._EDIT:
            return True
        return bool(e.text()) and e.text().isprintable()

    def handle_key(self, e) -> None:
        """A key typed while editing: it edits the text (never rotates or removes the box)."""
        e.accept()
        K, M = Qt.Key, Qt.KeyboardModifier
        key, text, t = e.key(), e.text(), self.text
        allmods = e.modifiers()
        mods = allmods & (M.ControlModifier | M.AltModifier | M.MetaModifier)
        shift = bool(allmods & M.ShiftModifier)
        if key == K.Key_Escape:
            self.end_editing()
            return
        if mods == M.ControlModifier and self._ctrl_edit(key, shift):
            self.update()
            return
        if mods == M.ControlModifier:
            if key == K.Key_A:
                self.set_selection(0, len(t))
            elif key == K.Key_C:
                self.copy()
            elif key == K.Key_X:
                self.cut()
            elif key == K.Key_V:
                self.paste()
            elif key == K.Key_Z:
                self.redo() if shift else self.undo()
            elif key == K.Key_Y:
                self.redo()
            self.update()
            return
        c = self.cursor
        a, b = self.selection()
        if key in (K.Key_Return, K.Key_Enter):
            self._push_undo()
            self._insert("\n")
        elif key in (K.Key_Backspace, K.Key_Delete):
            if a != b:
                self._push_undo()
                self._replace("")
            elif key == K.Key_Backspace and c > 0:
                self._push_undo("delete")
                self.cursor = c - 1
                self.set_text(t[:c - 1] + t[c:])
            elif key == K.Key_Delete and c < len(t):
                self._push_undo("delete")
                self.set_text(t[:c] + t[c + 1:])
        elif key in self._NAV:
            self._last_edit = None
            if shift and self.anchor is None:
                self.anchor = c
            if not shift and a != b and key in (K.Key_Left, K.Key_Right):
                self.cursor = a if key == K.Key_Left else b  # collapse the selection
            elif key == K.Key_Left:
                self.cursor = max(0, c - 1)
            elif key == K.Key_Right:
                self.cursor = min(len(t), c + 1)
            else:
                row, col = self._row_col()
                if key == K.Key_Up and row == 0:  # as a desktop text field: to the start
                    self.cursor = 0
                elif key == K.Key_Down and row == len(self._rows()) - 1:
                    self.cursor = len(t)
                else:
                    if key == K.Key_Home:
                        col = 0
                    elif key == K.Key_End:
                        col = self._row_len(row)
                    else:
                        row += -1 if key == K.Key_Up else 1
                    self.cursor = self._index(row, col)
            if not shift:
                self.anchor = None
        elif text and not mods and text.isprintable():
            self._push_undo("space" if text.isspace() else "type")
            self._insert(text)
        self.update()

    def _word_left(self, pos: int) -> int:
        """The start of the word before `pos` (a word is a run of non-space characters)."""
        t = self.text
        while pos > 0 and t[pos - 1].isspace():
            pos -= 1
        while pos > 0 and not t[pos - 1].isspace():
            pos -= 1
        return pos

    def _word_right(self, pos: int) -> int:
        """The start of the next word after `pos` (the end of the text after the last)."""
        t, n = self.text, len(self.text)
        while pos < n and not t[pos].isspace():
            pos += 1
        while pos < n and t[pos].isspace():
            pos += 1
        return pos

    def _ctrl_edit(self, key, shift: bool) -> bool:
        """Ctrl+key: word moves, text start/end, word deletes and the formatting keys.
        True if the key was one of those."""
        K = Qt.Key
        t, c = self.text, self.cursor
        if key in (K.Key_Left, K.Key_Right, K.Key_Home, K.Key_End):
            self._last_edit = None
            if shift and self.anchor is None:
                self.anchor = c
            self.cursor = {K.Key_Left: self._word_left, K.Key_Right: self._word_right,
                           K.Key_Home: lambda _p: 0, K.Key_End: lambda _p: len(t)}[key](c)
            if not shift:
                self.anchor = None
            return True
        if key in (K.Key_Backspace, K.Key_Delete):
            if not self.has_selection():
                other = self._word_left(c) if key == K.Key_Backspace else self._word_right(c)
                if other == c:
                    return True
                self.set_selection(c, other)
            self._push_undo()
            self._replace("")
            return True
        if shift and key not in (K.Key_Plus, K.Key_Equal):
            return False
        st = self.style
        if key == K.Key_B:
            self._set_style(replace(st, bold=not st.bold))
        elif key == K.Key_I:
            self._set_style(replace(st, italic=not st.italic))
        elif key in self._ALIGN_KEYS:
            self._set_style(replace(st, align=self._ALIGN_KEYS[key]))
        elif key in (K.Key_Plus, K.Key_Equal, K.Key_Minus):
            step = -SIZE_STEP if key == K.Key_Minus else SIZE_STEP
            self.set_font_size(max(SIZE_MIN, min(SIZE_MAX, self.size + step)))
        else:
            return False
        return True

    def _set_style(self, style: InkStyle) -> None:
        self.set_style(style)
        self.style_edited.emit(style)

    def _replace(self, s: str) -> None:
        """Put `s` in place of the selection (at the caret when nothing is selected)."""
        a, b = self.selection()
        self.anchor = None
        self.cursor = a + len(s)
        self.set_text(self.text[:a] + s + self.text[b:])

    def _insert(self, s: str) -> None:
        self._replace(s)

    def copy(self) -> None:
        """Ctrl+C: the selected text to the clipboard (nothing selected: nothing copied)."""
        if self.has_selection():
            QGuiApplication.clipboard().setText(self.selected_text())

    def cut(self) -> None:
        if self.has_selection():
            self.copy()
            self._push_undo()
            self._replace("")

    def paste(self) -> None:
        """Ctrl+V: the clipboard's plain text (line breaks made \\n) replaces the selection.
        Characters the font can't draw are reported after the render, as when typed."""
        md = QGuiApplication.clipboard().mimeData()
        if md is None or not md.hasText():
            return
        s = md.text().replace("\r\n", "\n").replace("\r", "\n")
        if not s:
            return
        self._push_undo()
        self._replace(s)

    def keyPressEvent(self, e):
        if self.editing:
            self.handle_key(e)
        else:
            super().keyPressEvent(e)

    def mousePressEvent(self, e):
        if self.editing and e.button() == Qt.MouseButton.LeftButton \
                and not self._on_rotate_handle(e.pos()) \
                and not self.handle_rect().contains(self.to_local_frame(e.pos())):
            # place the caret (Shift: extend the selection); a drag then selects
            pos = self.cursor_at(self.to_local_frame(e.pos()))
            shift = bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            if shift:
                self.set_selection(self.cursor if self.anchor is None else self.anchor, pos)
            else:
                self.set_selection(pos, pos)
            self._last_edit = None
            self._selecting = True
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._selecting:
            self.cursor = self.cursor_at(self.to_local_frame(e.pos()))
            self.update()
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._selecting:
            self._selecting = False
            if self.anchor == self.cursor:
                self.anchor = None
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.editable:
            pos = self.cursor_at(self.to_local_frame(e.pos()))
            if self.editing:  # a double-click in the box being edited selects a word
                a, b = self.word_at(pos)
                self.set_selection(a, b)
                self._selecting = False
            else:
                self.start_editing(pos)
            e.accept()
            return
        super().mouseDoubleClickEvent(e)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        rects = self.selection_rects() if self.editing else []
        if rects:  # behind the strokes
            painter.save()
            painter.translate(self.w / 2, self.h / 2)
            painter.rotate(self.rotation_deg)
            painter.translate(-self.w / 2, -self.h / 2)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(SELECTION)
            for r in rects:
                painter.drawRect(r)
            painter.restore()
        super().paint(painter, option, widget)
        if not (self.editing or (not self.text and self.hint_idle)):
            return
        painter.save()
        painter.translate(self.w / 2, self.h / 2)
        painter.rotate(self.rotation_deg)
        painter.translate(-self.w / 2, -self.h / 2)
        if not self.text:
            f = QFont(painter.font())
            f.setPixelSize(max(1, int(self.size * 0.6)))
            painter.setFont(f)
            painter.setPen(QPen(QColor("#9a9a9a"), 0))
            painter.drawText(QRectF(0, 0, self.w, self.h), Qt.AlignmentFlag.AlignCenter,
                             PLACEHOLDER)
        if self.editing:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(ACCENT, max(1.0, self._px(1.5))))
            painter.drawRect(QRectF(-self._px(4), -self._px(4),
                                    self.w + self._px(8), self.h + self._px(8)))
            a, b = self.caret_line()
            pen = QPen(ACCENT, max(1.5, self._px(2)))
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            painter.setPen(pen)
            painter.drawLine(a, b)
        painter.restore()

    def boundingRect(self) -> QRectF:
        r = super().boundingRect()
        cx, cy = self.w / 2, self.h / 2  # room for a caret past the box (trailing spaces)
        a, b = self.caret_line() if self.editing else (QPointF(cx, cy), QPointF(cx, cy))
        pts = [a, b]
        for sr in (self.selection_rects() if self.editing else []):
            pts += [sr.topLeft(), sr.bottomRight(), sr.topRight(), sr.bottomLeft()]
        extra = max(math.hypot(p.x() - cx, p.y() - cy) for p in pts)
        extra += self._px(12)
        if extra > r.width() / 2:
            r = QRectF(cx - extra, cy - extra, 2 * extra, 2 * extra)
        return r


class ShapeSceneItem(PlacedItem):
    """A rectangle, line or arrow drawn from the PC (compose.shapes.shape_polylines, the same
    call the send uses, for the box as it is now).

    A line or arrow is a thin box (shapes.LINE_H high) that it runs along the middle of,
    left to right: the corner handle changes only its length and the rotate handle its
    direction, so it moves, resizes, rotates and is sent exactly like any other item.
    `options` holds the shape's own settings (a rectangle's fill, an arrow's second head);
    the arrowhead's size follows the pen thickness."""

    def __init__(self, kind: str, pen: PenStyle, options: ShapeOptions = ShapeOptions(),
                 w: float | None = None, h: float | None = None):
        dw, dh = DEFAULT_SIZE[kind]
        super().__init__(dw if w is None else w, dh if h is None else h, pen)
        self.kind = kind
        self.options = options

    def is_line(self) -> bool:
        return self.kind in ("line", "arrow")

    def set_options(self, options: ShapeOptions) -> None:
        self.options = options
        self.update()

    def shape_options(self) -> ShapeOptions:
        return replace(self.options, thickness=self.pen.thickness)

    def local_polylines(self) -> list[Polyline]:
        return shape_polylines(self.kind, self.w, self.h, self.shape_options())

    def resize_to(self, w: float, h: float) -> None:
        self.set_size(w, LINE_H if self.is_line() else h)

    def model(self) -> ShapeItem:
        return ShapeItem(self.kind, self.box(), self.pen, self.rotation_deg,
                         fill=self.options.fill, double=self.options.double)

    def send_data(self) -> tuple[ShapeItem, list[Polyline], None]:
        return self.model(), self.local_polylines(), None


NOTE_MIN_INNER = 2.0  # a sticky note's text area is never narrower than this many font sizes


class NoteSceneItem(InkSceneItem):
    """A sticky note (kind "note"), or a rectangle (kind "rectangle", no fold) that can hold
    text: the outline with a folded corner (compose.shapes) and an ink text
    inside, typed and formatted exactly like an ink text box (this is one, with its text
    wrapped to the note's inner width, shapes.note_text_rect).

    The note keeps the size the user gives it; resizing it, or changing the text or its
    style, re-wraps the text at once (no debounce: the caret moves along the wrapped rows),
    and the note grows taller (and, for a large font, wider) when the text would not fit,
    so the text always stays inside. One pen draws the outline and the text: its controls
    are on the format bar, as for ink text.

    A rectangle with no text is only its outline (and fill): the box keeps the size it was
    given, it shows no placeholder, and its strokes are those of a plain shape; its pen is in
    the main toolbar until it is typed into. With text, the same layout as a note's (without
    the fold, text_rect(fold=False)), and a Fill's hatching skips a band around each text row."""

    def __init__(self, text: str, size: float, pen: PenStyle, style: InkStyle = InkStyle(),
                 w: float | None = None, h: float | None = None, kind: str = "note",
                 options: ShapeOptions = ShapeOptions()):
        dw, dh = DEFAULT_SIZE[kind]
        self.kind = kind
        self.folded = kind == "note"
        self.options = options
        self.hint_idle = self.folded
        super().__init__(text, size, pen, style, box=(dw if w is None else w,
                                                      dh if h is None else h))

    @property
    def plain(self) -> bool:
        """A rectangle with no text is just the outline (and its fill): the box is the one
        the user gave it, exactly as for a shape that cannot hold text."""
        return not self.folded and not self.text

    @property
    def bar_wanted(self) -> bool:
        return self.folded or self.editing or bool(self.text)

    def is_line(self) -> bool:
        return False

    def set_options(self, options: ShapeOptions) -> None:
        self.options = options
        self.update()

    def shape_options(self) -> ShapeOptions:
        return replace(self.options, thickness=self.pen.thickness)

    # -- layout --
    def _min_w(self) -> float:
        if self.plain:
            return MIN_SIZE
        return 2 * NOTE_PAD + NOTE_MIN_INNER * self.size

    def _rect(self, w: float, h: float) -> tuple[float, float, float, float]:
        return note_text_rect(w, h, self.folded)

    def _wrap_width(self) -> float | None:
        return self._rect(self.w, self.h)[2]

    def _text_origin(self) -> QPointF:
        x, y, _w, _h = self._rect(self.w, self.h)
        return QPointF(x, y + getattr(self, "_rise", 0.0))

    def _anchor_x(self) -> float:
        return 0.0  # the note's top-left corner stays put

    def _text_box(self, lines: list[Polyline]) -> tuple[float, float]:
        if self.plain:
            return self.w, self.h
        w = max(self.w, self._min_w())
        ys = [y for pl in lines for _, y in pl]
        # some glyphs (ascenders, accents) reach above the row's top: the text moves down
        self._rise = max(0.0, -min(ys, default=0.0))
        th = max(ys, default=EMPTY_H * self.size) + self._rise
        _x, top, _iw, _ih = self._rect(w, self.h)
        h = self.h
        while top + th + NOTE_PAD > h:  # the fold (and so `top`) can grow with h: settle
            h = top + th + NOTE_PAD + 1.0
            top = self._rect(w, h)[1]
        return w, h

    def _schedule(self) -> None:
        self._dirty = True
        self.render()

    def resize_to(self, w: float, h: float) -> None:
        self.prepareGeometryChange()
        self.w = max(float(w), self._min_w(), MIN_SIZE)
        self.h = max(float(h), MIN_SIZE)
        self.render()

    def set_font_size(self, size: float) -> None:
        self.size = max(float(size), MIN_FONT)
        self.render()

    # -- strokes --
    def text_polylines(self) -> list[Polyline]:
        o = self._text_origin()
        return [[(x + o.x(), y + o.y()) for x, y in pl] for pl in self._scaled()]

    def local_polylines(self) -> list[Polyline]:
        text = self.text_polylines()
        if self.folded:
            return shape_polylines("note", self.w, self.h) + text
        return shape_polylines(self.kind, self.w, self.h, self.shape_options(),
                               text_gaps(text)) + text

    def ready(self) -> bool:
        return True  # the outline is drawn even with no text

    def model(self) -> ShapeItem:
        return ShapeItem(self.kind, self.box(), self.pen, self.rotation_deg,
                         fill=self.options.fill, text=self.text)

    def send_data(self) -> tuple[ShapeItem, list[Polyline], None]:
        self.flush()
        return self.model(), self.local_polylines(), None
