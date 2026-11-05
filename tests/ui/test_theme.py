from unittest.mock import patch

import pytest
from PySide6.QtCore import QtMsgType, qInstallMessageHandler
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from rmdesk.ui import theme
from rmdesk.ui.widgets import Banner, Card, Chip, DevicePill, EmptyState, Segmented, ToggleSwitch, repolish

EXPECTED = {
    "bg": "#0B0B0B", "surface-1": "#141414", "surface-2": "#1C1C1C", "surface-3": "#262626",
    "line": "#2B2B2B", "accent": "#D01F2F", "accent-hover": "#E23443", "accent-text": "#FF6B73",
    "text": "#EDEDED", "text-2": "#B0B0B0", "text-3": "#8F8F8F",
    "ok": "#4CC764", "warn": "#E8AB3C", "paper": "#ECEBE6",
}
ICONS = ["files", "screen", "device", "usb", "wifi", "offline", "upload", "download", "trash", "refresh",
         "rotate", "camera", "present", "image", "text", "pen", "undo", "send", "check", "x", "alert",
         "info", "plus", "folder", "file-pdf", "file-epub", "notebook", "search", "maximize",
         "minimize", "sidebar", "rectangle", "line", "arrow", "note", "eye", "pointer", "move",
         "keep", "battery", "back"]


@pytest.fixture(autouse=True)
def _app(qapp):
    return qapp


def _lum(h):
    c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _ratio(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_tokens_exact():
    assert theme.TOKENS == EXPECTED


def test_token_contrast():
    assert _ratio(theme.TOKENS["text-3"], theme.TOKENS["surface-1"]) >= 4.5


@pytest.mark.parametrize("name", ICONS)
def test_icon_loads(name):
    ic = theme.icon(name)
    assert isinstance(ic, QIcon) and not ic.isNull()
    assert not ic.pixmap(16, 16).isNull()
    assert not theme.icon(name, 20, "#FF0000").pixmap(20, 20).toImage().isNull()


def test_all_bundled_icons_listed():
    assert set(theme.icon_names()) == set(ICONS)


def test_qss_parses_without_warnings(qapp):
    msgs = []
    old = qInstallMessageHandler(lambda t, c, m: msgs.append((t, m)))
    try:
        qapp.setStyleSheet(theme.qss())
        w = ToggleSwitch()
        w.show()
        qapp.processEvents()
    finally:
        qInstallMessageHandler(old)
        qapp.setStyleSheet("")
    bad = [m for t, m in msgs if t in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg)]
    assert not bad, bad


def test_chip_state_repolish(qtbot):
    chip = Chip("ok", "Running")
    qtbot.addWidget(chip)
    with patch.object(chip.style(), "polish", wraps=chip.style().polish) as pol, \
            patch.object(chip.style(), "unpolish", wraps=chip.style().unpolish) as unpol:
        chip.set_state("err", "Failed")
        assert unpol.called and pol.called
    assert chip.property("chip") == "err" and chip.text() == "Failed"


def test_toggle_toggles_and_paints(qtbot):
    t = ToggleSwitch()
    qtbot.addWidget(t)
    assert (t.width(), t.height()) == (36, 20)
    off = t.grab().toImage()
    t.click()
    assert t.isChecked()
    assert t.grab().toImage() != off
    t.click()
    assert not t.isChecked()


def test_segmented_banner_empty_pill(qtbot):
    seg = Segmented(["View", "Compose"])
    qtbot.addWidget(seg)
    with qtbot.waitSignal(seg.changed) as sig:
        seg.buttons[1].click()
    assert sig.args == [1] and seg.index() == 1

    b = Banner("warn", "Restart needed", "Restart")
    qtbot.addWidget(b)
    with qtbot.waitSignal(b.action):
        b.button.click()
    b.set_kind("err")
    assert b.property("banner") == "err"

    e = EmptyState("files", "Nothing here", "Add")
    qtbot.addWidget(e)
    with qtbot.waitSignal(e.action):
        e.button.click()

    p = DevicePill("usb")
    qtbot.addWidget(p)
    assert p.label.text() == "USB"
    p.set_state("connecting")
    assert p.dot.property("dot") == "connecting"

    c = Card()
    qtbot.addWidget(c)
    assert c.property("card") is True
    repolish(c)


@pytest.mark.parametrize("state,color", [("usb", "ok"), ("wifi", "ok"), ("connecting", "warn"), ("offline", "text-3")])
def test_pill_dot_colour(qapp, qtbot, state, color):
    qapp.setStyleSheet(theme.qss())
    try:
        p = DevicePill(state)
        qtbot.addWidget(p)
        p.show()
        img = p.dot.grab().toImage()
        got = img.pixelColor(img.width() // 2, img.height() // 2)
        assert got.name().upper() == theme.TOKENS[color]
    finally:
        qapp.setStyleSheet("")
