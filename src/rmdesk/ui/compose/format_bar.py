"""The floating format bar over a selected ink text box: size, font, bold/italic, alignment,
line and letter spacing and the pen. It sits above the box (below it when there is no room), always inside the view,
and wraps onto more rows when the view is narrow."""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QFont, QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout,
                               QLabel, QSpinBox, QToolButton, QWidget)

from rmdesk.compose.inktext import (ALIGNS, FONTS, LETTER_SPACING_MAX, LETTER_SPACING_MIN,
                                    LINE_SPACING_MAX, LINE_SPACING_MIN)
from rmdesk.compose.items import PenStyle
from rmdesk.ui.compose.scene import (ROT_OFFSET, ROT_RADIUS, SIZE_MAX, SIZE_MIN, SIZE_STEP,
                                     InkSceneItem, InkStyle)
from rmdesk.ui.theme import TOKENS
from rmdesk.ui.widgets.flow import FlowLayout

FONT_LABELS = {"sans": "Sans", "serif": "Serif", "script": "Script", "gothic": "Gothic",
               "cursive": "Cursive", "gothicita": "Italian Gothic",
               "greek": "Greek (symbols)", "cyrillic": "Cyrillic (symbols)"}
_SYMBOLS_TIP = ("{name}: the Latin keys type {script} letters (the Hershey font's own mapping, "
                "e.g. a \u2192 {example}), so it is not a {script} keyboard layout")
FONT_TIPS = {
    "greek": _SYMBOLS_TIP.format(name="Greek (symbols)", script="Greek", example="\u03b1")
    + "; y and z have no glyph",
    "cyrillic": _SYMBOLS_TIP.format(name="Cyrillic (symbols)", script="Cyrillic",
                                    example="\u0430"),
}
ALIGN_KEYS = {"left": "Ctrl+L", "center": "Ctrl+E", "right": "Ctrl+R"}
ALIGN_LABELS = {"left": "Align left", "center": "Centre", "right": "Align right"}
TOOLS = ["fineliner", "ballpoint", "marker", "pencil"]
COLORS = ["black", "grey", "white"]
MARGIN = 8  # from the view's edges
GAP = 8  # between the box (and its rotate handle) and the bar

# 24-grid outline drawings (stroke 1.8, round caps) like the bundled icons
_ALIGN_SVG = {
    "left": "M4 6h16M4 10h10M4 14h16M4 18h10",
    "center": "M4 6h16M7 10h10M4 14h16M7 18h10",
    "right": "M4 6h16M10 10h10M4 14h16M10 18h10",
}
_LINE_SPACING_SVG = "M11 6h10M11 12h10M11 18h10M5 4v16M3 6l2-2 2 2M3 18l2 2 2-2"
_LETTER_SPACING_SVG = "M4 4v16M20 4v16M8 12h8M10 10l-2 2 2 2M14 10l2 2-2 2"

# Local styles (theme.py could absorb these as QFrame#inkFormatBar / QToolButton[fmt] rules)
_QSS = f"""
QFrame#inkFormatBar {{ background: rgba(20,20,20,235); border: 1px solid {TOKENS['line']};
    border-radius: 10px; }}
QFrame#inkFormatBar QLabel {{ background: transparent; border: none; color: {TOKENS['text-3']};
    font-size: 12px; }}
QFrame#inkFormatBar QComboBox, QFrame#inkFormatBar QSpinBox,
QFrame#inkFormatBar QDoubleSpinBox {{ min-height: 30px; max-height: 30px; padding: 0 8px; }}
QFrame#inkFormatBar QToolButton {{ min-height: 30px; max-height: 30px; min-width: 30px;
    padding: 0; border-radius: 7px; border: 1px solid {TOKENS['line']};
    background: {TOKENS['surface-2']}; color: {TOKENS['text-2']}; }}
QFrame#inkFormatBar QToolButton:hover {{ background: {TOKENS['surface-3']}; }}
QFrame#inkFormatBar QToolButton:checked {{ border-color: {TOKENS['accent']}; color: #FFFFFF;
    background: rgba(208,31,47,46); }}
QFrame#inkFormatBar QToolButton:focus {{ border: 2px solid {TOKENS['accent']}; }}
"""


def _svg_icon(path: str, color: str, size: int = 16) -> QIcon:
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
           f'stroke="{color}" stroke-width="1.8" stroke-linecap="round" '
           f'stroke-linejoin="round"><path d="{path}"/></svg>')
    r = QSvgRenderer(QByteArray(svg.encode()))
    out = QIcon()
    for scale in (1, 2):
        img = QImage(size * scale, size * scale, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        r.render(p)
        p.end()
        pm = QPixmap.fromImage(img)
        pm.setDevicePixelRatio(scale)
        out.addPixmap(pm)
    return out


def _group(*widgets: QWidget, spacing: int = 4) -> QWidget:
    g = QWidget()
    lay = QHBoxLayout(g)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(spacing)
    for w in widgets:
        lay.addWidget(w)
    return g


class _CompactSpin(QDoubleSpinBox):
    """As wide as its longest value needs, no wider (the bar stays one row at 1280 px)."""

    def sizeHint(self) -> QSize:
        return self.minimumSizeHint()


class InkFormatBar(QFrame):
    """Edits one InkSceneItem. `style_changed(InkStyle)`, `size_changed(float)` and
    `pen_changed(PenStyle)` fire on user changes only (never while showing an item)."""

    style_changed = Signal(object)
    size_changed = Signal(float)
    pen_changed = Signal(object)
    done = Signal()  # a button or list was used: give the keys back to the text

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("inkFormatBar")
        self.setAccessibleName("Ink text format")
        self.setStyleSheet(_QSS)
        self.setAutoFillBackground(False)
        self.item: InkSceneItem | None = None
        self.avoid: QWidget | None = None  # a widget at the top of the view to stay below
        self._syncing = False

        self.size_spin = QSpinBox()
        self.size_spin.setRange(SIZE_MIN, SIZE_MAX)
        self.size_spin.setSingleStep(SIZE_STEP)
        self.size_spin.setToolTip("Size (Ctrl++ larger, Ctrl+\u2212 smaller)")
        self.size_spin.setAccessibleName("Size")
        self.font_combo = QComboBox()
        for f in FONTS:
            self.font_combo.addItem(FONT_LABELS[f], f)
        for f, tip in FONT_TIPS.items():
            self.font_combo.setItemData(self.font_combo.findData(f), tip,
                                        Qt.ItemDataRole.ToolTipRole)
        self.font_combo.setToolTip("Font")
        self.font_combo.setAccessibleName("Font")
        self.bold_btn = self._toggle("B", "Bold", "Ctrl+B")
        f = self.bold_btn.font()
        f.setBold(True)
        self.bold_btn.setFont(f)
        self.italic_btn = self._toggle("I", "Italic", "Ctrl+I")
        f = QFont(self.italic_btn.font())
        f.setItalic(True)
        self.italic_btn.setFont(f)
        self.align_btns: dict[str, QToolButton] = {}
        self.align_group = QButtonGroup(self)
        self.align_group.setExclusive(True)
        for a in ALIGNS:
            b = self._toggle("", ALIGN_LABELS[a], ALIGN_KEYS[a])
            b.setIcon(_svg_icon(_ALIGN_SVG[a], TOKENS["text"]))
            b.setIconSize(QSize(16, 16))
            self.align_group.addButton(b)
            self.align_btns[a] = b
        # 1.0x is the original line pitch (inktext.LINE_HEIGHT, 1.4 font sizes)
        self.line_spacing_spin = self._spin(LINE_SPACING_MIN, LINE_SPACING_MAX, 0.1, 1, "\u00d7",
                                            "Line spacing",
                                            "Line spacing (1.0\u00d7 = the normal distance "
                                            "between lines)")
        self.letter_spacing_spin = self._spin(LETTER_SPACING_MIN, LETTER_SPACING_MAX, 0.05, 2,
                                              " em", "Letter spacing",
                                              "Letter spacing: extra space after each "
                                              "character, in font sizes")
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
        self.thick_spin.setToolTip("Thickness")
        self.thick_spin.setAccessibleName("Thickness")

        flow = FlowLayout(self, spacing=6)
        flow.setContentsMargins(8, 6, 8, 6)
        size_label = QLabel("Size")
        for g in (_group(size_label, self.size_spin), self.font_combo,
                  _group(self.bold_btn, self.italic_btn),
                  _group(*self.align_btns.values()),
                  _group(self._icon_label(_LINE_SPACING_SVG, self.line_spacing_spin),
                         self.line_spacing_spin, spacing=2),
                  _group(self._icon_label(_LETTER_SPACING_SVG, self.letter_spacing_spin),
                         self.letter_spacing_spin, spacing=2),
                  _group(self.tool_combo, self.color_combo, self.thick_spin)):
            flow.addWidget(g)

        self.size_spin.valueChanged.connect(self._size_edited)
        self.size_spin.editingFinished.connect(self.done)
        self.font_combo.activated.connect(lambda _i: self._style_edited())
        for b in (self.bold_btn, self.italic_btn, *self.align_btns.values()):
            b.clicked.connect(lambda _c=False: self._style_edited())
        for sp in (self.line_spacing_spin, self.letter_spacing_spin):
            sp.valueChanged.connect(lambda _v: self._style_edited(refocus=False))
            sp.editingFinished.connect(self.done)
        self.tool_combo.activated.connect(lambda _i: self._pen_edited())
        self.color_combo.activated.connect(lambda _i: self._pen_edited())
        self.thick_spin.valueChanged.connect(lambda _v: self._pen_edited(refocus=False))
        self.thick_spin.editingFinished.connect(self.done)
        self.hide()

    @staticmethod
    def _spin(lo: float, hi: float, step: float, decimals: int, suffix: str, name: str,
              tip: str) -> QDoubleSpinBox:
        sp = _CompactSpin()
        sp.setDecimals(decimals)
        sp.setRange(lo, hi)
        sp.setSingleStep(step)
        sp.setSuffix(suffix)
        sp.setToolTip(tip)
        sp.setAccessibleName(name)
        return sp

    @staticmethod
    def _icon_label(path: str, buddy: QWidget) -> QLabel:
        lab = QLabel()
        lab.setPixmap(_svg_icon(path, TOKENS["text-3"]).pixmap(16, 16))
        lab.setToolTip(buddy.toolTip())
        lab.setBuddy(buddy)
        return lab

    @staticmethod
    def _toggle(text: str, name: str, keys: str = "") -> QToolButton:
        b = QToolButton()
        b.setText(text)
        b.setCheckable(True)
        b.setToolTip(f"{name} ({keys})" if keys else name)
        b.setAccessibleName(name)
        b.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        return b

    # -- the item's values -> the controls --
    def show_item(self, item: InkSceneItem | None) -> None:
        self.item = item
        if item is None:
            self.hide()
            return
        self.sync()
        self.show()
        self.raise_()
        self.place()

    def sync(self) -> None:
        it = self.item
        if it is None:
            return
        self._syncing = True
        try:
            if not self.size_spin.hasFocus():
                self.size_spin.setValue(int(round(it.size)))
            self.font_combo.setCurrentIndex(max(0, self.font_combo.findData(it.style.font)))
            self.font_combo.setToolTip(FONT_TIPS.get(it.style.font, "Font"))
            for sp, v in ((self.line_spacing_spin, it.style.line_spacing),
                          (self.letter_spacing_spin, it.style.letter_spacing)):
                if not sp.hasFocus():
                    sp.setValue(v)
            self.bold_btn.setChecked(it.style.bold)
            self.italic_btn.setChecked(it.style.italic)
            self.align_btns.get(it.style.align, self.align_btns["left"]).setChecked(True)
            self.tool_combo.setCurrentText(it.pen.tool)
            self.color_combo.setCurrentText(it.pen.color)
            self.thick_spin.setValue(it.pen.thickness)
        finally:
            self._syncing = False

    def ink_style(self) -> InkStyle:
        align = next((a for a, b in self.align_btns.items() if b.isChecked()), "left")
        return InkStyle(self.font_combo.currentData() or "sans", self.bold_btn.isChecked(),
                        self.italic_btn.isChecked(), align,
                        round(self.line_spacing_spin.value(), 2),
                        round(self.letter_spacing_spin.value(), 2))

    def pen(self) -> PenStyle:
        return PenStyle(self.tool_combo.currentText(),  # type: ignore[arg-type]
                        self.color_combo.currentText(),  # type: ignore[arg-type]
                        self.thick_spin.value())

    # -- user changes --
    def _style_edited(self, refocus: bool = True) -> None:
        if not self._syncing:
            self.style_changed.emit(self.ink_style())
            if refocus:
                self.done.emit()

    def _size_edited(self, v: int) -> None:
        if not self._syncing:
            self.size_changed.emit(float(v))

    def _pen_edited(self, refocus: bool = True) -> None:
        if not self._syncing:
            self.pen_changed.emit(self.pen())
            if refocus:
                self.done.emit()

    # -- placement --
    def place(self) -> None:
        """Above the item's box and rotate handle, else below it, always inside the view."""
        it, view = self.item, self.parentWidget()
        if it is None or view is None or it.scene() is None or not it.scene().views():
            return
        gv = it.scene().views()[0]
        avail = max(view.width() - 2 * MARGIN, 60)
        lay = self.layout()
        m = lay.contentsMargins()
        one_row = sum(lay.itemAt(i).sizeHint().width() for i in range(lay.count())) \
            + lay.spacing() * (lay.count() - 1) + m.left() + m.right() + 2
        w = min(one_row, avail)
        if one_row > avail:  # wrapped: as narrow as it can be without needing another row
            rows_h = lay.totalHeightForWidth(avail)
            lo, hi = 60, avail
            while lo < hi:
                mid = (lo + hi) // 2
                if lay.totalHeightForWidth(mid) <= rows_h:
                    hi = mid
                else:
                    lo = mid + 1
            w = lo
        self.setFixedWidth(w)
        self.setFixedHeight(max(lay.totalHeightForWidth(w), 1))
        pts = [it.mapToScene(it._rot_point(x, y, it.rotation_deg))
               for x, y in ((0, 0), (it.w, 0), (it.w, it.h), (0, it.h),
                            (it.w / 2, -ROT_OFFSET - ROT_RADIUS),
                            (it.w / 2, it.h + ROT_OFFSET + ROT_RADIUS))]
        vp = [gv.viewport().mapTo(view, gv.mapFromScene(p)) if view is not gv.viewport()
              else gv.mapFromScene(p) for p in pts]
        xs, ys = [q.x() for q in vp], [q.y() for q in vp]
        box = QRect(QPoint(min(xs), min(ys)), QPoint(max(xs), max(ys)))
        h = self.height()
        # never over the floating Place tools (`avoid`, a sibling at the top of the view)
        av = self.avoid
        top = MARGIN
        if av is not None and av.isVisible() and av.parentWidget() is view:
            top = av.geometry().bottom() + 1 + GAP
        y = box.top() - GAP - h
        if y < top:
            y = box.bottom() + GAP
            if y + h > view.height() - MARGIN:
                y = top
        x = box.center().x() - w // 2
        x = max(MARGIN, min(x, view.width() - w - MARGIN))
        y = max(min(top, max(view.height() - h, 0)), min(y, view.height() - h))
        self.move(x, y)
        self.raise_()
