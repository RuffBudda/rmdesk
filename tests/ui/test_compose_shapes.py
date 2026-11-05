"""Rectangle, line, arrow and sticky note in live Compose."""
import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtWidgets import QApplication

from rmdesk.compose import build, scroll_fit, shapes
from rmdesk.compose import frame as fr
from rmdesk.compose.items import ShapeItem
from rmdesk.notebook.opened import OpenedPage
from rmdesk.ui.compose.scene import InkSceneItem, NoteSceneItem
from tests.ui.test_live_compose import (NB, ORIG, PG2, FakeClient, _new_lines,  # noqa: F401
                                        drain_workers, folio_handler, frame, fw, make_screen,
                                        pruned, record_send, reset_clients)

K = Qt.Key
CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
NOMOD = Qt.KeyboardModifier.NoModifier
KINDS = ("rectangle", "line", "arrow", "note")


def _btn(panel, kind):
    return {"rectangle": panel.add_rect_btn, "line": panel.add_line_btn,
            "arrow": panel.add_arrow_btn, "note": panel.add_note_btn}[kind]


def _sent(call):
    return [(pt.x, pt.y) for ln in _new_lines(call["new_page"]) for pt in ln.points]


def _preview(item, layout=fr.PORTRAIT_UI, scroll=0.0):
    p = item.pos()
    return [fr.frame_to_page(p.x() + x, p.y() + y, "portrait", layout=layout, scroll=scroll)
            for pl in item.rotated_polylines() for x, y in pl]


def _close(a, b):
    assert len(a) == len(b)
    for u, v in zip(a, b):
        assert u == pytest.approx(v, abs=0.01)


# -- toolbar and placing -----------------------------------------------------------

def test_toolbar_has_the_shape_buttons_after_ink_text(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    order = [panel.add_image_btn, panel.add_ink_btn, panel.add_rect_btn, panel.add_line_btn,
             panel.add_arrow_btn, panel.add_note_btn, panel.remove_btn]
    # left to right in the tools row: the add cluster, then the remove cluster
    xs = [w.mapTo(panel.tools_row, w.rect().topLeft()).x() for w in order]
    assert xs == sorted(xs)
    assert all(panel.tools_row.isAncestorOf(w) for w in order)
    # icon-only buttons: the names are their accessible names and tooltips
    assert [b.accessibleName() for b in order[2:6]] == ["Rectangle", "Line", "Arrow",
                                                          "Sticky note"]
    for b in order[2:6]:
        assert not b.icon().isNull() and b.isVisible() and b.isEnabled()
        assert b.text() == "" and b.toolTip()


@pytest.mark.parametrize("kind", KINDS)
def test_button_places_a_selected_default_shape_in_the_visible_middle(qtbot, tmp_path, fw, kind):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    panel.view.fit()
    _btn(panel, kind).click()
    (item,) = panel.placed_items()
    assert item.isSelected() and item.kind == kind
    assert (item.w, item.h) == shapes.DEFAULT_SIZE[kind]
    centre = panel.view.mapToScene(panel.view.viewport().rect().center())
    c = item.mapToScene(item.centre())
    assert abs(c.x() - centre.x()) < 2 and abs(c.y() - centre.y()) < 2
    assert item.rotation_deg == 0
    assert panel.badge.text() == "1"
    if kind == "note":
        assert isinstance(item, InkSceneItem) and item.editing and item.ready()
        assert panel.format_bar.isVisible()


def test_shapes_start_upright_in_a_rotated_view(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    view.rotate()  # 90
    QApplication.processEvents()
    for kind in KINDS:
        item = panel.add_shape(kind)
        assert item.rotation_deg == 270


def test_shape_controls_follow_the_selection(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    rect = panel.add_shape("rectangle")
    assert panel.shape_controls.isVisible()
    assert panel.fill_check.isVisible() and not panel.double_check.isVisible()
    assert not panel.fill_check.isChecked()  # outline only by default
    n = len(rect.local_polylines())
    panel.fill_check.setChecked(True)
    assert rect.options.fill and len(rect.local_polylines()) > n
    arrow = panel.add_shape("arrow")
    assert panel.double_check.isVisible() and not panel.fill_check.isVisible()
    panel.double_check.setChecked(True)
    assert arrow.options.double and len(arrow.local_polylines()) == 3
    panel.add_shape("line")
    assert not panel.shape_controls.isVisible()
    rect.setSelected(True)
    for it in panel.placed_items():
        it.setSelected(it is rect)
    assert panel.fill_check.isChecked()
    for w in (panel.tool_combo, panel.color_combo, panel.thick_spin):
        assert w.isVisible()  # shapes have their pen in the main toolbar


def test_pen_controls_apply_to_shapes(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    arrow = panel.add_shape("arrow")
    head = arrow.local_polylines()[1]
    panel.tool_combo.setCurrentText("marker")
    panel.color_combo.setCurrentText("grey")
    panel.thick_spin.setValue(2.0)
    assert (arrow.pen.tool, arrow.pen.color, arrow.pen.thickness) == ("marker", "grey", 2.0)
    assert arrow.local_polylines()[1] != head  # the head follows the thickness
    assert arrow.model().options() == shapes.ShapeOptions(thickness=2.0)


def test_note_pen_lives_on_the_format_bar(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    note = panel.add_shape("note")
    assert panel.format_bar.isVisible() and panel.format_bar.item is note
    assert not panel.tool_combo.isVisible() and not panel.shape_controls.isVisible()
    panel.format_bar.thick_spin.setValue(2.0)
    assert note.pen.thickness == 2.0


def test_line_resize_changes_only_the_length(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    for kind in ("line", "arrow"):
        item = panel.add_shape(kind)
        h = item.h
        item.resize_to(650, 300)
        assert (item.w, item.h) == (650, h)
    rect = panel.add_shape("rectangle")
    rect.resize_to(120, 90)
    assert (rect.w, rect.h) == (120, 90)


# -- preview == sent -----------------------------------------------------------------

# item centres, all inside frame y 234..1638 (the page's width in the keyboard layout)
CENTRES = {"rectangle": (400, 560), "line": (900, 850), "arrow": (500, 1100), "note": (950, 1300)}


def _one_of_each(panel, rotate=0):
    out = []
    for i, kind in enumerate(KINDS):
        it = panel.add_shape(kind)
        if kind == "rectangle":
            panel.fill_check.setChecked(True)
        if kind == "arrow":
            panel.double_check.setChecked(True)
        if kind == "note":
            type_keys(panel, "Buy milk and some bread")
            it.end_editing()
        cx, cy = CENTRES[kind]
        it.setPos(cx - it.w / 2, cy - it.h / 2)
        it.set_rotation(rotate * (i + 1))
        out.append(it)
    return out


def type_keys(panel, text):
    from PySide6.QtTest import QTest
    QTest.keyClicks(panel.view, text)


@pytest.mark.parametrize("rotate", [0, 25])
def test_preview_equals_sent_for_each_kind(qtbot, tmp_path, fw, monkeypatch, rotate):
    calls = record_send(monkeypatch, tmp_path)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    panel.view.setFocus()
    items = _one_of_each(panel, rotate)
    want = sorted(p for it in items for p in _preview(it))
    panel.send()
    qtbot.waitUntil(lambda: bool(calls) and not panel.busy, timeout=10000)
    _close(sorted(_sent(calls[0])), want)
    assert panel.placed_items() == []  # a successful send removes the items


def test_preview_equals_sent_in_the_keyboard_layout_with_scroll(qtbot, tmp_path, fw,
                                                               monkeypatch):
    op = OpenedPage(NB, "Physics notes", PG2, 1, 2, "portrait", ORIG, vertical_scroll=300.0)
    calls = record_send(monkeypatch, tmp_path, opened=op)
    monkeypatch.setattr(scroll_fit, "fit_details", lambda picture, points: {"scroll": 300.0})
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path)
    view.frame = frame(255)
    dev.run_handler = folio_handler("Landscape")
    panel.view.setFocus()
    items = _one_of_each(panel, 10)
    want = [p for it in items for p in _preview(it, fr.KEYBOARD, 300.0)]
    panel.send()
    qtbot.waitUntil(lambda: bool(calls) and not panel.busy, timeout=10000)
    _close(sorted(_sent(calls[0])), sorted(want))


def test_send_data_is_a_shape_item(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    for kind in KINDS:
        it = panel.add_shape(kind)
        model, pls, size = it.send_data()
        assert isinstance(model, ShapeItem) and model.kind == kind and size is None
        assert pls == it.local_polylines()
        assert build.has_lines_on_page(model, pls)


# -- confirm, badge, undo ----------------------------------------------------------

def test_confirm_lists_shapes_and_badge_counts_them(qtbot, tmp_path, fw, monkeypatch):
    calls = record_send(monkeypatch, tmp_path)
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path)
    listings = []
    real = panel.bridge.confirm
    panel.bridge.confirm = lambda text, listing: listings.append(listing) or real(text, listing)
    panel.view.setFocus()
    panel.add_shape("rectangle")
    panel.add_shape("line")
    panel.add_shape("arrow")
    panel.double_check.setChecked(True)
    panel.add_shape("note")
    type_keys(panel, "Call the plumber tomorrow about the leaking kitchen tap")
    panel.add_ink_text("Hi")
    assert panel.badge.text() == "5"
    panel.send()
    qtbot.waitUntil(lambda: bool(calls) and not panel.busy, timeout=10000)
    (listing,) = listings
    assert "Rectangle" in listing and "Line" in listing and "Double arrow" in listing
    note = next(s for s in listing if s.startswith("Sticky note: “"))
    assert note.endswith("…”") and "Call the plumber" in note
    assert cfg.last_send and cfg.last_send.get("backup_dir")
    assert panel.undo_btn.isEnabled()  # undone like any send


def test_empty_note_is_kept_and_listed(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    note = panel.add_shape("note")
    note.end_editing()
    QApplication.processEvents()
    assert panel.placed_items() == [note]
    assert panel._describe(note) == "Sticky note"
    assert panel._describe(panel.add_shape("arrow")) == "Arrow"


# -- the sticky note's text ----------------------------------------------------------

def _note(qtbot, tmp_path, text):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    panel.view.fit()
    panel.add_note_btn.click()
    note = panel.placed_items()[0]
    panel.view.setFocus()
    for i, line in enumerate(text.split("\n")):
        if i:
            qtbot.keyClick(panel.view, K.Key_Return)
        qtbot.keyClicks(panel.view, line)
    return view, panel, note


def _text_inside(note):
    x, y, iw, ih = shapes.note_text_rect(note.w, note.h)
    pts = [p for pl in note.text_polylines() for p in pl]
    assert pts
    for px, py in pts:
        assert x - 1e-6 <= px <= x + iw + 1e-6 and y - 1e-6 <= py <= y + ih + 1e-6


def test_note_text_wraps_and_stays_inside_after_resize(qtbot, tmp_path, fw):
    view, panel, note = _note(qtbot, tmp_path, "the quick brown fox jumps over the lazy dog")
    assert isinstance(note, NoteSceneItem)
    assert len(note.stops) > 1  # wrapped onto more rows
    _text_inside(note)
    rows = len(note.stops)
    note.resize_to(note.w * 0.6, note.h)
    assert len(note.stops) > rows  # re-wrapped narrower
    _text_inside(note)
    note.resize_to(180, 60)  # too short for the text: the note grows to hold it
    _text_inside(note)
    note.resize_to(1300, 400)
    assert len(note.stops) == 1
    _text_inside(note)
    panel.format_bar.size_spin.setValue(80)  # a larger font re-wraps too
    assert note.size == 80
    _text_inside(note)


def test_note_outline_follows_resize(qtbot, tmp_path, fw):
    view, panel, note = _note(qtbot, tmp_path, "hi")
    note.resize_to(500, 420)
    outline = note.local_polylines()[0]
    assert (500.0, 420.0) in outline


def test_note_preview_is_outline_plus_text(qtbot, tmp_path, fw):
    from rmdesk.compose.inktext import text_to_polylines
    view, panel, note = _note(qtbot, tmp_path, "Hello note")
    x, y, iw, _ih = shapes.note_text_rect(note.w, note.h)
    want_text = text_to_polylines("Hello note", note.size, max_width=iw).lines
    want = shapes.shape_polylines("note", note.w, note.h) + \
        [[(x + a, y + b) for a, b in pl] for pl in want_text]
    assert note.local_polylines() == want


def test_editing_shortcuts_work_in_a_note(qtbot, tmp_path, fw):
    view, panel, note = _note(qtbot, tmp_path, "one  two three")
    k = lambda key, m=NOMOD: qtbot.keyClick(panel.view, key, m)  # noqa: E731
    assert note.text == "one  two three" and note.editing
    k(K.Key_Left, CTRL)
    assert note.cursor == 9
    k(K.Key_Left, CTRL | SHIFT)
    assert note.selection() == (5, 9)
    k(K.Key_Home, CTRL)
    assert note.cursor == 0 and not note.has_selection()
    k(K.Key_End, CTRL | SHIFT)
    assert note.selected_text() == "one  two three"
    k(K.Key_C, CTRL)
    assert QApplication.clipboard().text() == "one  two three"
    k(K.Key_End, CTRL)
    k(K.Key_Backspace, CTRL)
    assert note.text == "one  two "
    k(K.Key_Z, CTRL)
    assert note.text == "one  two three"
    k(K.Key_Z, CTRL | SHIFT)
    assert note.text == "one  two "
    k(K.Key_V, CTRL)
    assert note.text == "one  two one  two three"
    k(K.Key_A, CTRL)
    assert note.selection() == (0, len(note.text))
    k(K.Key_B, CTRL)
    assert note.style.bold and panel.format_bar.bold_btn.isChecked()
    k(K.Key_E, CTRL)
    assert note.style.align == "center"
    size = note.size
    k(K.Key_Plus, CTRL)
    assert note.size == size + 2
    _text_inside(note)
    k(K.Key_Escape)
    assert not note.editing and panel.placed_items() == [note]


def test_note_home_end_up_down_follow_the_wrapped_rows(qtbot, tmp_path, fw):
    view, panel, note = _note(qtbot, tmp_path, "aaaa bbbb cccc dddd eeee ffff gggg hhhh")
    note.resize_to(240, note.h)
    starts = note.row_starts
    assert len(starts) >= 3
    k = lambda key, m=NOMOD: qtbot.keyClick(panel.view, key, m)  # noqa: E731
    k(K.Key_Home, CTRL)
    k(K.Key_End)
    assert note.cursor == starts[1] - 1  # before the space the row was broken at
    k(K.Key_Down)
    assert starts[1] <= note.cursor < starts[2]
    k(K.Key_Home)
    assert note.cursor == starts[1]
    k(K.Key_Up)
    assert note.cursor == 0
    # the caret is drawn on the row it is in, inside the note
    note.cursor = starts[1]
    a, b = note.caret_line()
    x, y, _iw, _ih = shapes.note_text_rect(note.w, note.h)
    assert a.x() == pytest.approx(x) and a.y() > y


def test_click_in_a_note_places_the_caret(qtbot, tmp_path, fw):
    view, panel, note = _note(qtbot, tmp_path, "hello world")
    x, y, _iw, _ih = shapes.note_text_rect(note.w, note.h)
    assert note.cursor_at(QPointF(x - 5, y + note.size / 2)) == 0
    assert note.cursor_at(QPointF(note.w - 2, y + note.size / 2)) == len("hello world")


def test_ink_text_is_not_wrapped(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    item = panel.add_ink_text("the quick brown fox jumps over the lazy dog")
    assert len(item.stops) == 1 and not isinstance(item, NoteSceneItem)


# -- narrow window -----------------------------------------------------------------

def test_toolbar_wraps_at_700_px_with_the_shape_buttons(qtbot, tmp_path, fw):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    view.window().resize(700, 700)
    view.resize(700 - 208, 600)
    panel.add_shape("rectangle")
    QApplication.processEvents()
    bar = [panel.add_image_btn, panel.add_ink_btn, panel.add_rect_btn, panel.add_line_btn,
           panel.add_arrow_btn, panel.add_note_btn, panel.remove_btn, panel.tool_combo,
           panel.color_combo, panel.thick_spin, panel.undo_btn, panel.send_btn,
           panel.fill_check]
    assert view.minimumSizeHint().width() <= view.width()
    for w in bar:
        assert w.isVisible(), w
        r = w.mapTo(view, w.rect().topLeft())
        assert r.x() >= 0 and r.x() + w.width() <= view.width(), w
        assert w.width() >= w.minimumSizeHint().width(), w
    assert len({w.mapTo(view, w.rect().topLeft()).y() for w in bar}) > 2
    assert panel.view.height() >= 200
    tb, vp = panel.toolbar, panel.view.viewport()  # the floating tools stay on the picture
    assert tb.parentWidget() is vp
    assert tb.geometry().left() >= 0 and tb.geometry().right() < vp.width()


# -- text inside a rectangle -----------------------------------------------------------

def _rect(qtbot, tmp_path, text=None):
    view, panel, *_ = make_screen(qtbot, tmp_path)
    panel.view.fit()
    panel.view.setFocus()
    rect = panel.add_shape("rectangle")
    if text is not None:
        qtbot.mouseDClick(panel.view.viewport(), Qt.MouseButton.LeftButton,
                          pos=panel.view.mapFromScene(rect.mapToScene(rect.centre())))
        for i, line in enumerate(text.split("\n")):
            if i:
                qtbot.keyClick(panel.view, K.Key_Return)
            qtbot.keyClicks(panel.view, line)
    return view, panel, rect


def test_rectangle_is_a_plain_rectangle_until_it_has_text(qtbot, tmp_path, fw):
    view, panel, rect = _rect(qtbot, tmp_path)
    assert isinstance(rect, NoteSceneItem) and rect.kind == "rectangle" and not rect.editing
    assert (rect.w, rect.h) == shapes.DEFAULT_SIZE["rectangle"]
    want = shapes.shape_polylines("rectangle", rect.w, rect.h, shapes.ShapeOptions())
    assert rect.local_polylines() == want  # byte-identical to a plain rectangle
    panel.fill_check.setChecked(True)
    assert rect.local_polylines() == shapes.shape_polylines(
        "rectangle", rect.w, rect.h, shapes.ShapeOptions(fill=True))
    rect.resize_to(120, 90)
    assert (rect.w, rect.h) == (120, 90)
    assert panel.tool_combo.isVisible()  # its pen is in the main toolbar; no format bar yet
    assert not panel.format_bar.isVisible()
    model, pls, size = rect.send_data()
    assert model == ShapeItem("rectangle", rect.box(), rect.pen, 0.0, fill=True) and size is None
    assert panel._describe(rect) == "Rectangle"
    rect.end_editing()
    assert panel.placed_items() == [rect]  # an untouched rectangle is kept


def test_double_click_starts_typing_in_a_rectangle(qtbot, tmp_path, fw):
    view, panel, rect = _rect(qtbot, tmp_path, "Hello rectangle")
    assert rect.editing and rect.text == "Hello rectangle"
    assert panel.format_bar.isVisible() and panel.format_bar.item is rect
    assert not panel.tool_combo.isVisible()
    assert rect.local_polylines()[0] == [(0.0, 0.0), (rect.w, 0.0), (rect.w, rect.h),
                                         (0.0, rect.h), (0.0, 0.0)]  # no fold
    assert len(rect.local_polylines()) > 1
    rect.end_editing()
    assert panel.format_bar.isVisible()  # it has text: styled from the format bar


def test_enter_starts_typing_in_the_selected_rectangle(qtbot, tmp_path, fw):
    view, panel, rect = _rect(qtbot, tmp_path)
    assert rect.isSelected() and not rect.editing
    qtbot.keyClick(panel.view, K.Key_Return)
    assert rect.editing and rect.text == ""  # the Enter only opens the editor
    qtbot.keyClicks(panel.view, "ab")
    assert rect.text == "ab"
    rect.end_editing()
    rect.setSelected(False)
    rect.setSelected(True)
    qtbot.keyClick(panel.view, K.Key_Enter)
    assert rect.editing and rect.cursor == 2


def test_rectangle_text_wraps_with_the_note_padding_and_the_box_grows(qtbot, tmp_path, fw):
    view, panel, rect = _rect(qtbot, tmp_path,
                              "the quick brown fox jumps over the lazy dog " * 3)
    assert len(rect.stops) > 1
    x, y, iw, ih = shapes.note_text_rect(rect.w, rect.h, fold=False)
    assert (x, y) == (shapes.NOTE_PAD, shapes.NOTE_PAD) and iw == rect.w - 2 * shapes.NOTE_PAD
    assert rect.h > shapes.DEFAULT_SIZE["rectangle"][1]  # grew taller to hold the text
    for px, py in (p for pl in rect.text_polylines() for p in pl):
        assert x - 1e-6 <= px <= x + iw + 1e-6 and y - 1e-6 <= py <= y + ih + 1e-6
    h = rect.h
    rect.resize_to(rect.w * 0.6, 50)
    assert rect.h > 50 and len(rect.stops) > 3 and rect.h >= h  # narrower: re-wrapped, taller
    for px, py in (p for pl in rect.text_polylines() for p in pl):
        assert 0 <= px <= rect.w and 0 <= py <= rect.h


def test_editing_shortcuts_work_in_a_rectangle(qtbot, tmp_path, fw):
    view, panel, rect = _rect(qtbot, tmp_path, "one two")
    qtbot.keyClick(panel.view, K.Key_Backspace, CTRL)
    assert rect.text == "one "
    qtbot.keyClick(panel.view, K.Key_Z, CTRL)
    assert rect.text == "one two"
    qtbot.keyClick(panel.view, K.Key_B, CTRL)
    assert rect.style.bold


def test_deleting_the_text_makes_it_plain_again(qtbot, tmp_path, fw):
    view, panel, rect = _rect(qtbot, tmp_path, "x")
    qtbot.keyClick(panel.view, K.Key_Backspace)
    assert rect.text == "" and rect.plain
    rect.end_editing()
    QApplication.processEvents()
    assert panel.placed_items() == [rect]
    assert rect.local_polylines() == shapes.shape_polylines(
        "rectangle", rect.w, rect.h, shapes.ShapeOptions())


def test_fill_hatching_skips_a_band_around_each_text_row(qtbot, tmp_path, fw):
    view, panel, rect = _rect(qtbot, tmp_path, "Fill me in\nsecond row")
    plain = shapes.shape_polylines("rectangle", rect.w, rect.h,
                                   shapes.ShapeOptions(fill=True))[1:]
    panel.fill_check.setChecked(True)
    assert rect.options.fill
    pls = rect.local_polylines()
    text = rect.text_polylines()
    hatch = pls[1:len(pls) - len(text)]
    assert hatch and pls[len(pls) - len(text):] == text  # the text lines are drawn on top
    assert len(hatch) > len(plain) // 2
    gaps = shapes.text_gaps(text)
    assert len(gaps) >= 1
    for pl in hatch:
        for _x, y in pl:
            assert not any(a + 1e-6 < y < b - 1e-6 for a, b in gaps)
    assert any(pl[0][1] > b and pl[-1][1] < a for pl in plain for a, b in gaps[:1])  # was crossed
    panel.fill_check.setChecked(False)
    assert rect.local_polylines() == shapes.shape_polylines(
        "rectangle", rect.w, rect.h, shapes.ShapeOptions()) + text


def test_confirm_lists_a_rectangle_with_its_text(qtbot, tmp_path, fw):
    view, panel, rect = _rect(qtbot, tmp_path, "Call the plumber tomorrow about the leaking tap")
    label = panel._describe(rect)
    assert label.startswith("Rectangle: “Call the plumber") and label.endswith("…”")
    rect.set_text("Short", now=True)
    assert panel._describe(rect) == "Rectangle: “Short”"


def _same_points(got, want):
    """The same multiset of points (a hatch has many points sharing one coordinate, so a
    sort-and-compare can pair them up differently by float noise)."""
    assert len(got) == len(want)
    left = list(want)
    for g in got:
        i = next((i for i, w in enumerate(left)
                  if abs(w[0] - g[0]) < 0.02 and abs(w[1] - g[1]) < 0.02), None)
        assert i is not None, g
        left.pop(i)


def _rect_items(panel, rotate, text="Boxed words that wrap around a few rows"):
    rect = panel.add_shape("rectangle")
    rect.start_editing()
    type_keys(panel, text)
    rect.end_editing()
    panel.fill_check.setChecked(True)
    plain = panel.add_shape("rectangle")  # an empty one next to it
    for it, (cx, cy) in ((rect, (500, 700)), (plain, (900, 1200))):
        it.setPos(cx - it.w / 2, cy - it.h / 2)
        it.set_rotation(rotate)
    return [rect, plain]


@pytest.mark.parametrize("rotate", [0, 20])
def test_rectangle_text_preview_equals_sent(qtbot, tmp_path, fw, monkeypatch, rotate):
    calls = record_send(monkeypatch, tmp_path)
    view, panel, *_ = make_screen(qtbot, tmp_path)
    panel.view.setFocus()
    items = _rect_items(panel, rotate)
    want = sorted(p for it in items for p in _preview(it))
    panel.send()
    qtbot.waitUntil(lambda: bool(calls) and not panel.busy, timeout=10000)
    _same_points(_sent(calls[0]), want)


def test_rectangle_text_preview_equals_sent_in_the_keyboard_layout_with_scroll(
        qtbot, tmp_path, fw, monkeypatch):
    op = OpenedPage(NB, "Physics notes", PG2, 1, 2, "portrait", ORIG, vertical_scroll=300.0)
    calls = record_send(monkeypatch, tmp_path, opened=op)
    monkeypatch.setattr(scroll_fit, "fit_details", lambda picture, points: {"scroll": 300.0})
    view, panel, dev, cfg, _ = make_screen(qtbot, tmp_path)
    view.frame = frame(255)
    dev.run_handler = folio_handler("Landscape")
    panel.view.setFocus()
    items = _rect_items(panel, 10)
    want = [p for it in items for p in _preview(it, fr.KEYBOARD, 300.0)]
    panel.send()
    qtbot.waitUntil(lambda: bool(calls) and not panel.busy, timeout=10000)
    _same_points(_sent(calls[0]), want)


def test_rectangle_text_anchored_placement_matches_the_preview(qtbot, tmp_path, fw):
    view, panel, rect = _rect(qtbot, tmp_path, "Anchored words in a box")
    rect.end_editing()
    panel.fill_check.setChecked(True)
    rect.setPos(300, 500)
    rect.set_rotation(15)
    a = fr.Anchored(const=900.0, origin_x=-300.0)
    model, pls, _ = rect.send_data()
    got = [(p.x, p.y) for ln in build.frame_to_lines(model, pls, None, "portrait",
                                                      layout=fr.KEYBOARD, anchored=a)
           for p in ln.points]
    p0 = rect.pos()
    ref = [fr.frame_to_anchored(p0.x() + x, p0.y() + y, a)
           for pl in rect.rotated_polylines() for x, y in pl]
    _same_points(got, ref)


def test_rectangle_with_text_verifies_on_the_fixtures(qtbot, tmp_path, fw):
    from pathlib import Path

    from rmdesk.notebook import page
    view, panel, rect = _rect(qtbot, tmp_path, "Text inside\na filled box")
    rect.end_editing()
    panel.fill_check.setChecked(True)
    rect.setPos(150, 400)
    model, pls, _ = rect.send_data()
    lines = build.frame_to_lines(model, pls, None, "portrait")
    assert lines
    for ln in lines:
        page._check_line(ln)
    rm = Path(__file__).parents[1] / "fixtures" / "rm"
    names = sorted(p.name for p in rm.glob("*.rm"))
    assert names
    for name in names:
        orig = (rm / name).read_bytes()
        pg = page.load(orig)
        pg.append_to_rmdesk_layer(lines)
        page.verify(orig, pg.to_bytes(), len(lines), None)
