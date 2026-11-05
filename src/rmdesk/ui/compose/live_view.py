"""The Screen view's one picture: the live tablet frame with the placed items (scene = frame
pixels) on top. The same widget serves every pointer (Control, Place); only Place makes
the items interactive, and only Control sends input (rmdesk.ui.remote_control)."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter, QPen, QTransform
from PySide6.QtWidgets import QFrame, QGraphicsScene, QGraphicsView

from rmdesk.compose.frame import FRAME_H, FRAME_W
from rmdesk.compose.place import SNAP_DEG
from rmdesk.ui.compose.scene import PlacedItem
from rmdesk.ui.theme import TOKENS

BG = "#202020"
OUTLINE_PX = 2  # the Control focus outline: keys go to the tablet


def paint_frame(p: QPainter, view: QRectF, img: QImage, region: QRect, rotation: int) -> QRectF:
    """Draw `region` of `img` fit to `view` (aspect kept, centred), rotated clockwise; returns
    where it landed. Shared by the Screen view's picture and the Present window."""
    from rmdesk.ui.screen_view import Geometry  # (screen_view imports this module)
    g = Geometry(view, region, rotation)
    p.save()
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    p.translate(g.target.center())
    p.rotate(g.rotation)
    w, h = region.width() * g.scale, region.height() * g.scale
    p.drawImage(QRectF(-w / 2, -h / 2, w, h), img, QRectF(region))
    p.restore()
    return g.target


class LiveComposeView(QGraphicsView):
    """The scene (frame pixels) over the source's latest frame, rotated like the Screen view
    and fit to the widget. `fitted` fires after every fit (resize, rotation).

    The frame is drawn exactly as the plain canvas drew it (Geometry: fit, rotation), so
    Control's mapping (ScreenView._canvas_geometry) matches what is on screen."""

    fitted = Signal()

    def __init__(self, source, scene: QGraphicsScene):
        super().__init__(scene)
        self.source = source
        self.paste_handler = None  # Ctrl+V in Place (not editing): set by the Screen view
        # placed items scrolled off the picture: ((frame x, frame y), "2 items above") marks,
        # drawn upright just inside the picture's edge (set by LiveCompose)
        self.edge_marks: list[tuple[tuple[float, float], str]] = []
        self.setFrameShape(QFrame.Shape.NoFrame)  # viewport == widget: one coordinate system
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setMinimumSize(200, 260)
        self.setAccessibleName("Tablet screen")
        self._key = None
        self._arr: np.ndarray | None = None
        self._img: QImage | None = None
        source.frame_changed.connect(self.viewport().update)
        source.redraw.connect(self.fit)

    @property
    def frame(self):
        return self.source.frame

    def fit(self) -> None:
        rot = self.source.rotation % 360
        w, h = (FRAME_H, FRAME_W) if rot in (90, 270) else (FRAME_W, FRAME_H)
        vp = self.viewport().rect()
        s = max(min(vp.width() / w, vp.height() / h), 1e-3)
        self.setTransform(QTransform().rotate(rot).scale(s, s))
        self.centerOn(self.sceneRect().center())
        self.viewport().update()
        self.fitted.emit()

    def editing_item(self) -> PlacedItem | None:
        """The item being typed into, if any (only while the items are interactive: Place)."""
        if not self.isInteractive():
            return None
        for it in self.scene().items():
            if isinstance(it, PlacedItem) and it.editing:
                return it
        return None

    def event(self, e) -> bool:
        if e.type() == QEvent.Type.ShortcutOverride:
            editing = self.editing_item()
            if editing is not None and editing.wants_key(e):
                e.accept()  # Ctrl+Z, Ctrl+C... edit the text, never a window or app shortcut
                return True
        return super().event(e)

    def keyPressEvent(self, e) -> None:
        if not self.isInteractive():  # Control: the items take no keys
            e.ignore()
            return
        editing = self.editing_item()
        if editing is not None:  # keys go to the text: [ ] type, Delete/Backspace edit
            editing.handle_key(e)
            return
        mods = Qt.KeyboardModifier
        if e.matches(QKeySequence.StandardKey.Paste) and self.paste_handler is not None:
            if self.paste_handler():  # Ctrl+V in Place: a picture, files or text from the clipboard
                e.accept()
                return
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not e.modifiers():
            one = [i for i in self.scene().selectedItems() if isinstance(i, PlacedItem)]
            if len(one) == 1 and hasattr(one[0], "start_editing"):  # Enter: type into it
                one[0].start_editing()
                e.accept()
                return
        chord = bool(e.modifiers() & (mods.ControlModifier | mods.AltModifier | mods.MetaModifier))
        step = None if chord else {Qt.Key.Key_BracketLeft: -SNAP_DEG,
                                   Qt.Key.Key_BracketRight: SNAP_DEG}.get(e.key())
        sel = [i for i in self.scene().selectedItems() if isinstance(i, PlacedItem)]
        if step is None or not sel:
            super().keyPressEvent(e)
            return
        for it in sel:  # plain `[` / `]` (never Ctrl+[ / Ctrl+]) turn the selected items 15 degrees
            it.rotate_by(step)
        e.accept()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self.fit()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.fit()

    def focusInEvent(self, e) -> None:
        super().focusInEvent(e)
        self.viewport().update()

    def focusOutEvent(self, e) -> None:
        super().focusOutEvent(e)
        self.viewport().update()

    def _image(self) -> QImage | None:
        f = self.source.frame
        if f is None:
            return None
        if self._key is not f:  # holds the Frame: compared with `is`, never a reusable id()
            self._arr = np.ascontiguousarray(f.gray)  # QImage doesn't own its buffer
            self._img = QImage(self._arr.data, f.width, f.height, self._arr.strides[0],
                               QImage.Format.Format_Grayscale8)
            self._key = f
        return self._img

    def page_rect(self) -> QRectF:
        """Where the page is drawn, in viewport (= widget) pixels."""
        return self.mapFromScene(self.sceneRect()).boundingRect().toRectF()

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        painter.save()
        painter.resetTransform()  # paint in viewport pixels, like the plain canvas did
        vp = QRectF(self.viewport().rect())
        painter.fillRect(vp, QColor(BG))
        img = self._image()
        if img is not None:
            paint_frame(painter, vp, img, QRect(0, 0, img.width(), img.height()),
                        self.source.rotation)
        elif getattr(self.source, "pointer", "") == "place":
            painter.fillRect(self.page_rect(), QColor("white"))  # a page to place on
        painter.restore()

    def outline_visible(self) -> bool:
        """The thin accent outline: Control is on and the picture has the keys."""
        s = self.source
        control = getattr(s, "control", None)
        return (getattr(s, "pointer", "") == "control" and control is not None
                and control.active and self.hasFocus())

    def mark_rects(self) -> list[tuple[QRectF, str]]:
        """Where each edge mark is drawn, in viewport pixels: a pill centred on its frame
        point, kept inside the picture."""
        out = []
        if not self.edge_marks:
            return out
        fm = self.fontMetrics()
        page = self.page_rect().adjusted(4, 4, -4, -4)
        for (fx, fy), text in self.edge_marks:
            c = self.mapFromScene(QPointF(fx, fy))
            w, h = fm.horizontalAdvance(text) + 16, fm.height() + 6
            x = min(max(c.x() - w / 2, page.left()), page.right() - w)
            y = min(max(c.y() - h / 2, page.top()), page.bottom() - h)
            out.append((QRectF(x, y, w, h), text))
        return out

    def _paint_marks(self) -> None:
        rects = self.mark_rects()
        if not rects:
            return
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for r, text in rects:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(TOKENS["accent"]))
            p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
            p.setPen(QColor("#FFFFFF"))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
        p.end()

    def paintEvent(self, e) -> None:
        super().paintEvent(e)
        self._paint_marks()
        if not self.outline_visible():
            return
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(TOKENS["accent"]), OUTLINE_PX)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        img = self._image()
        if img is not None:
            from rmdesk.ui.screen_view import Geometry
            r = Geometry(QRectF(self.viewport().rect()), QRect(0, 0, img.width(), img.height()),
                         self.source.rotation).target
        else:
            r = self.page_rect()
        half = OUTLINE_PX / 2
        r = r.adjusted(half, half, -half, -half)
        p.drawRect(r)
        p.end()

