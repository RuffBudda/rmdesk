"""Design tokens, generated stylesheet and recolourable SVG icons."""
from __future__ import annotations

from importlib import resources

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

TOKENS = {
    "bg": "#0B0B0B", "surface-1": "#141414", "surface-2": "#1C1C1C", "surface-3": "#262626",
    "line": "#2B2B2B", "accent": "#D01F2F", "accent-hover": "#E23443", "accent-text": "#FF6B73",
    "text": "#EDEDED", "text-2": "#B0B0B0", "text-3": "#8F8F8F",
    "ok": "#4CC764", "warn": "#E8AB3C", "paper": "#ECEBE6",
}
FONT = "Inter"
MONO = "JetBrains Mono"


def qss() -> str:
    t = TOKENS
    return f"""
* {{ font-family: "{FONT}", sans-serif; font-size: 13px; color: {t['text']}; }}
QMainWindow, QDialog, QMessageBox, #stage, #stack {{ background: {t['bg']}; }}
QWidget#page {{ background: {t['bg']}; }}
QLabel {{ background: transparent; }}
QLabel#title {{ font-size: 18px; font-weight: 600; }}
QLabel#heading {{ font-size: 15px; font-weight: 600; }}
QLabel#display {{ font-size: 22px; font-weight: 600; }}
QLabel#overline {{ font-size: 11px; font-weight: 600; color: {t['text-3']}; }}
QLabel#hint, QLabel#muted, QLabel[role="hint"] {{ font-size: 12px; color: {t['text-3']}; }}
QLabel#error {{ color: {t['accent-text']}; }}
QLabel#mono, QLabel[role="mono"] {{ font-family: "{MONO}", monospace; font-size: 12px; }}
#statusBar {{ background: {t['surface-1']}; border: none; border-top: 1px solid {t['line']}; }}
QLabel#statusMessage {{ font-size: 12px; color: {t['text-2']}; padding: 2px 8px; }}
QFrame[card="true"] {{ background: {t['surface-1']}; border: 1px solid {t['line']}; border-radius: 14px; }}
QFrame[card="true"] QLabel {{ background: transparent; }}
QFrame[card="true"] QLabel[chip] {{ border-radius: 12px; padding: 2px 10px; background: {t['surface-3']}; color: {t['text-2']}; }}
QFrame[card="true"] QLabel[chip="ok"] {{ background: rgba(76,199,100,33); color: #86DF97; }}
QFrame[card="true"] QLabel[chip="warn"] {{ background: rgba(232,171,60,36); color: #F2C46F; }}
QFrame[card="true"] QLabel[chip="err"] {{ background: rgba(208,31,47,51); color: #FF9AA0; }}
QFrame[card="true"] QLabel[chip="neutral"] {{ background: {t['surface-3']}; color: {t['text-2']}; }}
#devicePill {{ background: transparent; border: 2px solid transparent; border-radius: 8px; }}
#devicePill:hover {{ background: {t['surface-3']}; }}
#devicePill[current="true"] {{ background: {t['surface-3']}; }}
#devicePill:focus {{ border-color: {t['accent']}; }}
#devicePill QLabel {{ background: transparent; border: none; padding: 0; }}
QLabel#pillState {{ font-size: 13px; font-weight: 500; color: {t['text-2']}; }}
#devicePill:hover QLabel#pillState {{ color: {t['text']}; }}
QLabel#onboardStatus {{ font-size: 18px; font-weight: 600; color: {t['text']}; }}
QLabel#onboardAdvice {{ font-size: 13px; color: {t['text-2']}; }}
QPushButton#linkButton {{ color: {t['accent-text']}; padding: 0 8px; }}
QPushButton#linkButton:hover {{ color: {t['text']}; }}
QPushButton#backButton {{ padding: 0 12px 0 8px; color: {t['text-2']}; }}
QPushButton#backButton:hover {{ color: {t['text']}; }}
QFrame[banner] {{ border-radius: 10px; border: 1px solid {t['line']}; background: {t['surface-2']}; }}
QFrame[banner] QPushButton {{ min-height: 28px; }}
QFrame[banner] QLabel {{ background: transparent; border: none; }}
QFrame[banner="warn"] {{ background: rgba(232,171,60,36); border-color: rgba(232,171,60,90); }}
QFrame[banner="err"] {{ background: rgba(208,31,47,51); border-color: rgba(208,31,47,120); }}
QFrame[banner="ok"] {{ background: rgba(76,199,100,33); border-color: rgba(76,199,100,90); }}
QFrame[segmented="true"] {{ background: {t['surface-1']}; border: 1px solid {t['line']}; border-radius: 8px; }}
QFrame[segmented="true"] QPushButton {{ min-height: 28px; border: none; border-radius: 6px; background: transparent; color: {t['text-2']}; }}
QFrame[segmented="true"] QPushButton:checked {{ background: {t['surface-3']}; color: {t['text']}; }}
QPushButton, QToolButton {{ min-height: 36px; padding: 0 13px; border-radius: 8px; border: 1px solid {t['line']}; background: {t['surface-2']}; }}
QToolButton {{ min-height: 32px; padding: 0 10px; }}
QPushButton:hover, QToolButton:hover {{ background: {t['surface-3']}; }}
QPushButton:pressed, QToolButton:pressed, QPushButton:checked, QToolButton:checked {{ background: {t['surface-3']}; }}
QPushButton:focus, QToolButton:focus {{ border: 2px solid {t['accent']}; }}
QPushButton:disabled, QToolButton:disabled {{ background: {t['surface-1']}; color: {t['text-3']}; border-color: {t['line']}; }}
QPushButton[variant="primary"] {{ background: {t['accent']}; border-color: {t['accent']}; color: #FFFFFF; font-weight: 600; }}
QPushButton[variant="primary"]:hover {{ background: {t['accent-hover']}; border-color: {t['accent-hover']}; }}
QPushButton[variant="primary"]:disabled {{ background: {t['surface-3']}; border-color: {t['surface-3']}; color: {t['text-3']}; }}
QPushButton[variant="ghost"], QToolButton[variant="ghost"] {{ background: transparent; border-color: transparent; color: {t['text-2']}; }}
QPushButton[variant="ghost"]:hover, QToolButton[variant="ghost"]:hover {{ background: {t['surface-2']}; color: {t['text']}; }}
QPushButton[variant="danger"] {{ color: {t['accent-text']}; }}
QPushButton[variant="danger"]:disabled, QPushButton[variant="ghost"]:disabled {{ color: {t['text-3']}; }}
QPushButton[hasBadge="true"] {{ padding-right: 40px; }}
QPushButton[dense="true"] {{ min-height: 32px; }}
QPushButton[iconOnly="true"] {{ padding: 0; min-width: 36px; max-width: 36px; }}
QPushButton[iconOnly="true"][dense="true"] {{ min-width: 32px; max-width: 32px; }}
QPushButton[iconOnly="true"]:checked {{ background: rgba(208,31,47,46); border-color: {t['accent']}; }}
QPushButton[iconOnly="true"][variant="ghost"]:checked {{ background: rgba(208,31,47,46); border-color: rgba(208,31,47,120); color: {t['text']}; }}
QFrame[segmented="true"] QPushButton {{ padding: 0 10px; }}
QFrame[cluster="true"] {{ background: {t['surface-2']}; border: none; border-radius: 10px; }}
QFrame[cluster="true"] QLabel {{ background: transparent; color: {t['text-2']}; }}
QFrame[cluster="true"] QPushButton {{ border-width: 2px; border-style: solid; min-height: 32px; max-height: 32px; }}
QFrame[cluster="true"] QPushButton[iconOnly="true"] {{ padding: 0; min-width: 32px; max-width: 32px; }}
QFrame[cluster="true"] QPushButton[variant="ghost"]:hover {{ background: {t['surface-3']}; color: {t['text']}; }}
QFrame[cluster="true"] QPushButton:focus {{ border-color: {t['accent']}; }}
QFrame[cluster="true"] QPushButton[iconOnly="true"][variant="ghost"]:checked {{ background: rgba(208,31,47,46); border-color: rgba(208,31,47,128); }}
QFrame[cluster="true"] QPushButton[iconOnly="true"][variant="ghost"]:checked:focus {{ border-color: {t['accent']}; }}
QFrame[cluster="true"] QPushButton[variant="danger"] {{ background: transparent; border-color: transparent; }}
QFrame[cluster="true"] QPushButton[variant="danger"]:hover {{ background: {t['surface-3']}; }}
QFrame[cluster="true"] QPushButton[variant="danger"]:focus, QFrame[cluster="true"] QPushButton[variant="secondary"]:focus {{ border-color: {t['accent']}; }}
QPushButton[variant="secondary"] {{ background: {t['surface-3']}; border-color: transparent; }}
QPushButton[variant="secondary"]:hover {{ background: #333333; }}
QFrame[cluster="true"] QCheckBox {{ min-height: 36px; padding: 0 6px; }}
QFrame[cluster="true"] QLabel {{ padding-left: 6px; }}
QFrame[cluster="true"] QLabel[badge="true"] {{ background: #FFFFFF; color: {t['accent']}; padding: 0 6px; }}
QFrame[cluster="true"] QFrame[segmented="true"] {{ background: transparent; border: none; border-radius: 0; }}
QFrame[cluster="true"] QFrame[segmented="true"] QPushButton {{ border: 2px solid transparent; min-height: 32px; max-height: 32px; padding: 0 12px; font-weight: 500; }}
QFrame[cluster="true"] QFrame[segmented="true"] QPushButton:hover {{ color: {t['text']}; }}
QFrame[cluster="true"] QFrame[segmented="true"] QPushButton:checked {{ background: {t['surface-3']}; color: {t['text']}; }}
QFrame[cluster="true"] QFrame[segmented="true"] QPushButton:focus {{ border-color: {t['accent']}; }}
QFrame[cluster="true"] QComboBox, QFrame[cluster="true"] QSpinBox, QFrame[cluster="true"] QDoubleSpinBox {{ background: {t['surface-1']}; min-height: 34px; max-height: 34px; }}
QFrame[cluster="true"] QComboBox:hover, QFrame[cluster="true"] QSpinBox:hover, QFrame[cluster="true"] QDoubleSpinBox:hover {{ background: {t['surface-3']}; }}
QFrame[cluster="true"] QComboBox:focus, QFrame[cluster="true"] QSpinBox:focus, QFrame[cluster="true"] QDoubleSpinBox:focus {{ border-width: 1px; border-color: {t['accent']}; }}
QPushButton[compact="true"] {{ padding: 0 12px; }}
QLabel#hoverHint {{ background: {t['surface-2']}; color: {t['text-2']}; font-size: 12px; border: 1px solid {t['line']}; border-radius: 6px; padding: 3px 8px; }}
QLineEdit, QTextEdit, QPlainTextEdit {{ min-height: 36px; padding: 0 12px; border-radius: 8px; border: 1px solid {t['line']};
    background: {t['bg']}; selection-background-color: {t['accent']}; selection-color: #FFFFFF; }}
QTextEdit, QPlainTextEdit {{ padding: 8px 12px; }}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{ border: 2px solid {t['accent']}; }}
QLineEdit:disabled {{ color: {t['text-3']}; background: {t['surface-1']}; }}
QComboBox, QSpinBox, QDoubleSpinBox {{ min-height: 36px; padding: 0 12px; border-radius: 8px; border: 1px solid {t['line']}; background: {t['surface-2']}; }}
QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{ background: {t['surface-3']}; }}
QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border: 2px solid {t['accent']}; }}
QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{ color: {t['text-3']}; background: {t['surface-1']}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{ background: {t['surface-2']}; border: 1px solid {t['line']}; border-radius: 8px; outline: 0;
    selection-background-color: {t['surface-3']}; selection-color: {t['text']}; }}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 18px; border: none; background: transparent; }}
QCheckBox {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {t['text-3']}; background: {t['bg']}; }}
QCheckBox::indicator:hover {{ border-color: {t['text-2']}; }}
QCheckBox::indicator:checked {{ background: {t['accent']}; border-color: {t['accent']}; }}
QCheckBox::indicator:focus {{ border: 2px solid {t['accent']}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {t['surface-3']}; border-radius: 5px; min-height: 28px; }}
QScrollBar::handle:horizontal {{ background: {t['surface-3']}; border-radius: 5px; min-width: 28px; }}
QScrollBar::handle:hover {{ background: {t['line']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; background: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QToolTip {{ background: {t['surface-3']}; color: {t['text']}; border: 1px solid {t['line']}; border-radius: 6px; padding: 4px 8px; }}
QMenu {{ background: {t['surface-2']}; border: 1px solid {t['line']}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 8px 14px; border-radius: 6px; }}
QMenu::item:selected {{ background: {t['surface-3']}; }}
QMenu::item:disabled {{ color: {t['text-3']}; }}
QMenu::separator {{ height: 1px; background: {t['line']}; margin: 4px 8px; }}
QListView, QTreeView, QTableView, QListWidget, QTreeWidget {{ background: {t['surface-1']}; border: 1px solid {t['line']}; border-radius: 10px;
    outline: 0; alternate-background-color: {t['surface-1']}; }}
QListView::item, QTreeView::item {{ padding: 6px; border-radius: 6px; }}
QListView::item:hover, QTreeView::item:hover {{ background: {t['surface-2']}; }}
QListView::item:selected, QTreeView::item:selected {{ background: {t['surface-3']}; color: {t['text']}; }}
QHeaderView::section {{ background: {t['surface-1']}; color: {t['text-3']}; border: none; border-bottom: 1px solid {t['line']}; padding: 6px 8px; font-size: 11px; font-weight: 600; }}
QListWidget#docGrid {{ background: transparent; border: none; }}
QListWidget#docGrid::item, QListWidget#docGrid::item:hover, QListWidget#docGrid::item:selected {{ background: transparent; padding: 0; }}
QFrame#selBar {{ background: {t['surface-2']}; border: 1px solid {t['line']}; border-radius: 10px; }}
QFrame#selBar QLabel {{ background: transparent; border: none; }}
QLabel#selCount {{ font-weight: 600; }}
QSplitter::handle {{ background: transparent; width: 14px; }}
QTreeView {{ padding: 6px; selection-background-color: transparent; }}
QTreeView::branch, QTreeView::branch:selected, QTreeView::branch:hover {{ background: transparent; border: none; }}
QProgressBar {{ min-height: 6px; max-height: 6px; border: none; border-radius: 3px; background: {t['surface-3']}; text-align: center; color: transparent; }}
QProgressBar::chunk {{ border-radius: 3px; background: {t['text-2']}; }}
QProgressBar#refreshBar {{ min-height: 3px; max-height: 3px; border-radius: 0; background: transparent; }}
QProgressBar#refreshBar::chunk {{ border-radius: 0; background: {t['accent']}; }}
QProgressBar#batteryBar[low="true"]::chunk {{ background: {t['warn']}; }}
QLabel#batteryBadgeText {{ font-size: 12px; color: {t['text-2']}; }}
#batteryBadge[low="true"] QLabel#batteryBadgeText {{ color: {t['warn']}; }}
QFrame#placeBar {{ background: rgba(20,20,20,235); border: none; border-radius: 12px; }}
QFrame#placeBar QLabel {{ background: transparent; }}
QWidget#batteryOverlay {{ background: rgba(20,20,20,170); border-radius: 8px; }}
QLabel#batteryOverlayText {{ font-size: 11px; color: {t['text-2']}; }}
#batteryOverlay[low="true"] QLabel#batteryOverlayText {{ color: {t['warn']}; }}
QSlider::groove:horizontal {{ height: 4px; border-radius: 2px; background: {t['surface-3']}; }}
QSlider::sub-page:horizontal {{ height: 4px; border-radius: 2px; background: {t['accent']}; }}
QSlider::handle:horizontal {{ width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; background: {t['text']}; }}
QSlider::handle:horizontal:focus {{ border: 2px solid {t['accent']}; }}
QGroupBox {{ border: 1px solid {t['line']}; border-radius: 10px; margin-top: 12px; padding-top: 12px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; color: {t['text-3']}; }}
QScrollArea {{ border: none; background: transparent; }}
QWidget#sheet {{ background: transparent; }}
QFrame[sheetCard="true"] {{ background: {t['surface-1']}; border: 1px solid {t['line']}; border-radius: 16px; }}
QFrame[sheetCard="true"] QLabel {{ background: transparent; border: none; }}
QLabel#sheetTitle {{ font-size: 17px; font-weight: 600; }}
QLabel#sheetItems {{ background: {t['surface-2']}; border-radius: 8px; padding: 10px 12px; color: {t['text-2']}; }}
QLabel[kbd="true"] {{ font-family: "{MONO}", monospace; font-size: 11px; background: {t['surface-3']}; border: 1px solid {t['line']}; border-radius: 5px; padding: 1px 6px; color: {t['text']}; }}
QLabel[badge="true"] {{ background: #FFFFFF; color: {t['accent']}; font-size: 11px; font-weight: 700; border-radius: 9px; padding: 0 6px; }}
QWidget#overlay {{ background: transparent; }}
QWidget[overlayChip="true"] {{ background: transparent; }}
QLabel[chip] {{ border-radius: 12px; padding: 2px 10px; font-size: 12px; font-weight: 500; background: {t['surface-3']}; color: {t['text-2']}; }}
QLabel[chip="ok"] {{ background: rgba(76,199,100,33); color: #86DF97; }}
QLabel[chip="warn"] {{ background: rgba(232,171,60,36); color: #F2C46F; }}
QLabel[chip="err"] {{ background: rgba(208,31,47,51); color: #FF9AA0; }}
QLabel[chip="neutral"] {{ background: {t['surface-3']}; color: {t['text-2']}; }}
#devicePill QLabel[dot] {{ min-width: 8px; max-width: 8px; min-height: 8px; max-height: 8px; border-radius: 4px; background: {t['text-3']}; }}
#devicePill QLabel[dot="usb"], #devicePill QLabel[dot="wifi"] {{ background: {t['ok']}; }}
#devicePill QLabel[dot="connecting"] {{ background: {t['warn']}; }}
#devicePill QLabel[dot="offline"] {{ background: {t['text-3']}; }}
"""


def icon_names() -> list[str]:
    root = resources.files("rmdesk.ui").joinpath("icons")
    return sorted(p.name[:-4] for p in root.iterdir() if p.name.endswith(".svg"))


def _svg(name: str, color: str) -> bytes:
    data = resources.files("rmdesk.ui").joinpath("icons", f"{name}.svg").read_bytes()
    return data.replace(b"currentColor", color.encode())


def icon(name: str, size: int = 16, color: str | None = None) -> QIcon:
    """Render a bundled SVG recoloured (default: text colour) at `size` px (and 2x for HiDPI)."""
    renderer = QSvgRenderer(QByteArray(_svg(name, color or TOKENS["text"])))
    out = QIcon()
    for scale in (1, 2):
        px = size * scale
        img = QImage(px, px, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        renderer.render(p)
        p.end()
        pm = QPixmap.fromImage(img)
        pm.setDevicePixelRatio(scale)
        out.addPixmap(pm)
    return out
