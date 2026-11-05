import io
import struct
from pathlib import Path

import pytest
import rmscene
from rmscene import scene_items as si
from rmscene.crdt_sequence import CrdtSequenceItem
from rmscene.scene_stream import (
    AuthorIdsBlock,
    RootTextBlock,
    SceneGroupItemBlock,
    SceneInfo,
    SceneLineItemBlock,
    SceneTombstoneItemBlock,
    SceneTreeBlock,
    TreeNodeBlock,
)
from rmscene.tagged_block_common import CrdtId, LwwValue

from rmdesk.notebook import page, render

FIX = Path(__file__).parent / "fixtures" / "rm"
FIXTURES = sorted(p.name for p in FIX.glob("*.rm"))
MULTILAYER = ["Normal_A_stroke_2_layers.rm", "Normal_A_stroke_2_layers_v3.3.2.rm"]
HEADER_LEN = 43
ROOT = CrdtId(0, 1)


def _data(name: str) -> bytes:
    return (FIX / name).read_bytes()


def _tree(data: bytes):
    return rmscene.read_tree(io.BytesIO(data))


def _raw_blocks(data: bytes) -> list[bytes]:
    out, i = [], HEADER_LEN
    while i < len(data):
        n = struct.unpack_from("<I", data, i)[0]
        out.append(data[i : i + 8 + n])
        i += 8 + n
    return out


def _items(tree) -> dict:
    """(parent node id, item id) -> value, for every item in the tree."""
    out = {}

    def walk(group):
        for it in group.children.sequence_items():
            v = it.value
            if isinstance(v, si.Group):
                out[(group.node_id, it.item_id)] = ("group", v.node_id, v.label, v.visible)
                walk(v)
            else:
                out[(group.node_id, it.item_id)] = it
    walk(tree.root)
    return out


def _lines(n: int = 3) -> list[si.Line]:
    return [
        si.Line(
            color=si.PenColor.RED,
            tool=si.Pen.FINELINER_2,
            points=[si.Point(-100.0 + 10 * i + k, 200.5 + k, 0, 0, 4, 255) for k in range(5)],
            thickness_scale=2.0,
            starting_length=0.0,
        )
        for i in range(n)
    ]


def _root_labels(tree) -> list[str]:
    return [g.label.value for g in tree.root.children.values()]


def _appended(name: str, n: int = 3) -> tuple[bytes, bytes]:
    orig = _data(name)
    p = page.load(orig)
    p.append_layer("rmdesk", _lines(n))
    return orig, p.to_bytes()


def test_constants():
    assert (page.PAGE_W, page.PAGE_H, page.X_MIN) == (1404, 1872, -702)


@pytest.mark.parametrize("name", FIXTURES)
def test_load_roundtrip_identical_tree(name):
    orig = _data(name)
    new = page.load(orig).to_bytes()
    a, b = _tree(orig), _tree(new)
    assert _items(a) == _items(b)
    assert a.root_text == b.root_text
    assert a.scene_info == b.scene_info


@pytest.mark.parametrize("name", FIXTURES)
def test_unmodified_page_is_byte_identical(name):
    orig = _data(name)
    p = page.load(orig)
    assert p.raw == orig
    assert p.to_bytes() == orig


@pytest.mark.parametrize("name", FIXTURES)
def test_append_layer_adds_lines(name):
    orig, new = _appended(name, 3)
    page.verify(orig, new, 3, None)
    tree = _tree(new)
    layer = tree.root.children.values()[-1]
    assert layer.label.value == "rmdesk"
    assert layer.visible.value is True
    assert layer.children.values() == _lines(3)


@pytest.mark.parametrize("name", FIXTURES)
def test_append_preserves_original_block_bytes(name):
    orig, new = _appended(name)
    old_raw, new_raw = _raw_blocks(orig), _raw_blocks(new)
    assert len(new_raw) == len(old_raw) + 3 + 3
    it = iter(new_raw)
    assert all(any(r == n for n in it) for r in old_raw)  # ordered subsequence
    assert new[:HEADER_LEN] == orig[:HEADER_LEN]


@pytest.mark.parametrize("name", MULTILAYER)
def test_append_keeps_multilayer_page(name):
    orig, new = _appended(name)
    before = _root_labels(_tree(orig))
    assert len(before) == 2
    assert _root_labels(_tree(new)) == before + ["rmdesk"]


@pytest.mark.parametrize("name", FIXTURES)
def test_ids_do_not_collide(name):
    orig, new = _appended(name)
    p = page.load(new)
    ids = []
    for b in p.blocks:
        if isinstance(b, SceneTreeBlock):
            ids.append(b.tree_id)
        elif hasattr(b, "item") and b.item is not None:
            ids.append(b.item.item_id)
    assert len(ids) == len(set(ids))
    # every new id is above every id the original page used (text spans included)
    old_max = page._max_id_part2(page.load(orig).blocks, 1)
    new_blocks = p.blocks[-3:]
    for b in new_blocks:
        assert b.item.item_id.part2 > old_max
    layer_tree = next(b for b in p.blocks if isinstance(b, SceneTreeBlock)
                      and b.tree_id == new_blocks[0].parent_id)
    assert layer_tree.tree_id.part2 > old_max


def test_ids_skip_text_character_span():
    # With_SceneInfo_Block.rm has a text item at (1,102) holding 16 chars -> ids up to (1,117)
    orig, new = _appended("With_SceneInfo_Block.rm", 1)
    assert page._max_id_part2(page.load(orig).blocks, 1) >= 117
    p = page.load(new)
    tb = [b for b in p.blocks if isinstance(b, SceneTreeBlock)][-1]
    assert tb.tree_id == CrdtId(1, 118)


def test_append_twice_on_reloaded_page():
    orig, first = _appended("Normal_A_stroke_2_layers_v3.3.2.rm", 2)
    p = page.load(first)
    p.append_layer("rmdesk", _lines(1))
    second = p.to_bytes()
    page.verify(first, second, 1, None)
    assert _root_labels(_tree(second))[-2:] == ["rmdesk", "rmdesk"]


def test_new_blocks_use_device_versions():
    # v2 lines always; TreeNodeBlock header follows the file's existing tree nodes
    for name, tree_node_ver in [("Normal_A_stroke_2_layers.rm", 1),
                                ("Color_and_tool_v3.14.4.rm", 2)]:
        orig, new = _appended(name, 1)
        raw = _raw_blocks(new)
        old_n = len(_raw_blocks(orig))
        added = [r for r in raw if r not in _raw_blocks(orig)]
        assert len(added) == 4 and len(raw) == old_n + 4
        hdr = {r[7]: (r[5], r[6]) for r in added}
        assert hdr[0x05] == (2, 2)
        assert hdr[0x02] == (1, tree_node_ver)


def test_author_ids_added_when_missing():
    p0 = page.load(_data("Lines_v2.rm"))
    blocks = [b for b in p0.blocks if not isinstance(b, AuthorIdsBlock)]
    buf = io.BytesIO()
    rmscene.write_blocks(buf, blocks, {"version": "3.1"})
    orig = buf.getvalue()
    p = page.load(orig)
    p.append_layer("rmdesk", _lines(1))
    new = p.to_bytes()
    page.verify(orig, new, 1, None)
    authors = [b for b in page.load(new).blocks if isinstance(b, AuthorIdsBlock)]
    assert len(authors) == 1 and 1 in authors[0].author_uuids


def test_append_rejects_empty_lines():
    p = page.load(_data("Lines_v2.rm"))
    with pytest.raises(ValueError):
        p.append_layer("rmdesk", [])


def test_append_rejects_out_of_range_point():
    p = page.load(_data("Lines_v2.rm"))
    bad = _lines(1)
    bad[0].points[0].pressure = 300
    with pytest.raises(ValueError):
        p.append_layer("rmdesk", bad)


# --- verify failures ---------------------------------------------------------

def _appended_page(name: str, n: int = 3):
    orig = _data(name)
    p = page.load(orig)
    p.append_layer("rmdesk", _lines(n))
    return orig, p


def test_verify_detects_missing_line():
    orig, p = _appended_page("Normal_A_stroke_2_layers_v3.3.2.rm")
    first_line = next(b for b in p.blocks if isinstance(b, SceneLineItemBlock))
    p.blocks.remove(first_line)
    with pytest.raises(page.VerifyError):
        page.verify(orig, p.to_bytes(), 3, None)


def test_verify_detects_changed_line():
    orig, p = _appended_page("Lines_v2.rm")
    first_line = next(b for b in p.blocks if isinstance(b, SceneLineItemBlock)
                      and b.item.value is not None)
    first_line.item.value.points[0].x += 1.0
    with pytest.raises(page.VerifyError):
        page.verify(orig, p.to_bytes(), 3, None)


def test_verify_detects_moved_line():
    orig, p = _appended_page("Normal_A_stroke_2_layers_v3.3.2.rm")
    # (1,88) is a v2 line block rmscene can re-encode exactly; move it to another group
    line = next(b for b in p.blocks if isinstance(b, SceneLineItemBlock)
                and b.item.item_id == CrdtId(1, 88))
    line.parent_id = CrdtId(1, 26)
    with pytest.raises(page.VerifyError):
        page.verify(orig, p.to_bytes(), 3, None)


def test_modified_block_rmscene_cannot_reproduce_is_refused():
    # (1,25) has block header (1,2), which rmscene's writer never emits: re-encoding
    # it would silently change the stroke's encoding, so to_bytes refuses instead.
    p = page.load(_data("Normal_A_stroke_2_layers_v3.3.2.rm"))
    line = next(b for b in p.blocks if isinstance(b, SceneLineItemBlock)
                and b.item.item_id == CrdtId(1, 25))
    line.parent_id = CrdtId(1, 26)
    with pytest.raises(page.UnsupportedPage):
        p.to_bytes()


def test_verify_detects_wrong_count():
    orig, new = _appended("Lines_v2.rm", 3)
    with pytest.raises(page.VerifyError):
        page.verify(orig, new, 2, None)
    with pytest.raises(page.VerifyError):
        page.verify(orig, new, 4, None)


def test_verify_detects_renamed_original_layer():
    orig, p = _appended_page("Normal_A_stroke_2_layers_v3.3.2.rm")
    node = next(b for b in p.blocks if isinstance(b, TreeNodeBlock)
                and b.group.label.value == "Layer 2")
    node.group.label = LwwValue(node.group.label.timestamp, "Renamed")
    with pytest.raises(page.VerifyError):
        page.verify(orig, p.to_bytes(), 3, None)


def test_verify_detects_hidden_original_layer():
    orig, p = _appended_page("Normal_A_stroke_2_layers_v3.3.2.rm")
    node = next(b for b in p.blocks if isinstance(b, TreeNodeBlock)
                and b.group.label.value == "Layer 2")
    node.group.visible = LwwValue(node.group.visible.timestamp, False)
    with pytest.raises(page.VerifyError):
        page.verify(orig, p.to_bytes(), 3, None)


def test_verify_detects_new_layer_not_last():
    orig, p = _appended_page("Normal_A_stroke_2_layers_v3.3.2.rm")
    new_item = [b for b in p.blocks if isinstance(b, SceneGroupItemBlock)
                and b.parent_id == ROOT][-1]
    new_item.item.left_id = CrdtId(0, 0)
    new_item.item.right_id = CrdtId(0, 14)  # before Layer 1
    with pytest.raises(page.VerifyError):
        page.verify(orig, p.to_bytes(), 3, None)


def test_verify_detects_changed_scene_info():
    orig, p = _appended_page("Color_and_tool_v3.14.4.rm")
    info = next(b for b in p.blocks if isinstance(b, SceneInfo))
    info.paper_size = (1404, 1872)
    with pytest.raises(page.VerifyError):
        page.verify(orig, p.to_bytes(), 3, None)


def test_verify_detects_wrong_label():
    orig = _data("Lines_v2.rm")
    p = page.load(orig)
    p.append_layer("other", _lines(3))
    with pytest.raises(page.VerifyError):
        page.verify(orig, p.to_bytes(), 3, None)


def test_verify_detects_layer_under_wrong_parent():
    orig, p = _appended_page("Normal_A_stroke_2_layers_v3.3.2.rm")
    new_item = [b for b in p.blocks if isinstance(b, SceneGroupItemBlock)
                and b.parent_id == ROOT][-1]
    new_item.parent_id = CrdtId(0, 13)
    new_item.item.left_id = CrdtId(0, 0)
    with pytest.raises(page.VerifyError):
        page.verify(orig, p.to_bytes(), 3, None)


def test_verify_detects_changed_text():
    orig, p = _appended_page("Normal_AB.rm")
    rt = next(b for b in p.blocks if isinstance(b, RootTextBlock))
    item = rt.value.items.sequence_items()[0]
    item.value = "AX"
    with pytest.raises(page.VerifyError):
        page.verify(orig, p.to_bytes(), 3, None)


def test_verify_accepts_appended_text():
    orig = _data("Normal_AB.rm")
    p = page.load(orig)
    rt = next(b for b in p.blocks if isinstance(b, RootTextBlock))
    old = rt.value.items.sequence_items()[0]  # (1,16) "AB" -> ids 16,17
    rt.value.items.add(CrdtSequenceItem(CrdtId(1, 18), CrdtId(1, 17), CrdtId(0, 0), 0, "\nhi"))
    new = p.to_bytes()
    page.verify(orig, new, 0, "hi")
    with pytest.raises(page.VerifyError):
        page.verify(orig, new, 0, "ho")
    # (c) text block re-encoded but no text declared: raw check rejects it
    with pytest.raises(page.VerifyError, match="original block"):
        page.verify(orig, new, 0, None)


def test_verify_rejects_garbage_new():
    orig = _data("Lines_v2.rm")
    with pytest.raises(page.VerifyError):
        page.verify(orig, orig[:-5], 0, None)
    with pytest.raises(page.VerifyError):
        page.verify(orig, b"not a page", 0, None)


# --- load failures -----------------------------------------------------------

def _corrupt_first_line_block(data: bytes) -> bytes:
    raw = _raw_blocks(data)
    off = HEADER_LEN
    for r in raw:
        if r[7] == 0x05:  # SceneLineItemBlock
            b = bytearray(data)
            b[off + 8] = 0x2C  # replace parent_id tag (0x1F) with a bogus tag
            return bytes(b)
        off += len(r)
    raise AssertionError("no line block")


def test_unreadable_block_rejected():
    bad = _corrupt_first_line_block(_data("Lines_v2.rm"))
    blocks = list(rmscene.read_blocks(io.BytesIO(bad)))
    assert any(type(b).__name__ == "UnreadableBlock" for b in blocks)
    with pytest.raises(page.UnsupportedPage):
        page.load(bad)


def test_bad_header_rejected():
    data = _data("Lines_v2.rm")
    with pytest.raises(page.UnsupportedPage):
        page.load(data.replace(b"version=6", b"version=5", 1))


def test_truncated_page_rejected():
    data = _data("Lines_v2.rm")
    with pytest.raises(page.UnsupportedPage):
        page.load(data[:-3])
    with pytest.raises(page.UnsupportedPage):
        page.load(data + b"\x01\x02")


# --- render ------------------------------------------------------------------

@pytest.mark.parametrize("name", FIXTURES)
def test_render_svg_nonempty(name):
    assert b"<svg" in render.page_svg(_data(name))


def test_render_svg_after_append():
    _, new = _appended("Normal_A_stroke_2_layers_v3.3.2.rm")
    svg = render.page_svg(new)
    assert b"<svg" in svg and svg.count(b"<polyline") + svg.count(b"<path") > 0


# --- review fix round (R20) --------------------------------------------------

TYPE_CODE = {"AuthorIdsBlock": "A", "MigrationInfoBlock": "M", "PageInfoBlock": "P",
             "SceneInfo": "I", "SceneTreeBlock": "T", "RootTextBlock": "R",
             "TreeNodeBlock": "N", "SceneGroupItemBlock": "G", "SceneLineItemBlock": "L",
             "SceneGlyphItemBlock": "Y", "SceneTombstoneItemBlock": "X"}


def _layout(data: bytes) -> str:
    """Block-type sequence with runs collapsed, e.g. 'AMPTRNGL'."""
    seq = "".join(TYPE_CODE[type(b).__name__] for b in page.load(data).blocks)
    return "".join(c for i, c in enumerate(seq) if i == 0 or seq[i - 1] != c)


@pytest.mark.parametrize("name", FIXTURES)
def test_append_keeps_device_block_layout(name):
    orig, new = _appended(name)
    before = _layout(orig)
    expected = before if before.endswith("L") else before + "L"
    assert _layout(new) == expected


def _deleted_top_layer(form: str) -> bytes:
    """2-layer page whose right-most root item (Layer 2, item (1,19)) is deleted."""
    p = page.load(_data("Normal_A_stroke_2_layers_v3.3.2.rm"))
    i, gb = next((i, b) for i, b in enumerate(p.blocks) if isinstance(b, SceneGroupItemBlock)
                 and b.item.item_id == CrdtId(1, 19))
    if form == "value-none":
        gb.item.value = None
        gb.item.deleted_length = 1
    else:
        p.blocks[i] = SceneTombstoneItemBlock(
            parent_id=ROOT, item=CrdtSequenceItem(CrdtId(1, 19), CrdtId(0, 14), CrdtId(0, 0), 1, None))
    data = p.to_bytes()
    assert _root_labels(_tree(data)) == ["Layer 1"]
    return data


@pytest.mark.parametrize("form", ["value-none", "tombstone"])
def test_append_after_deleted_top_layer(form):
    orig = _deleted_top_layer(form)
    p = page.load(orig)
    p.append_layer("rmdesk", _lines(2))
    new = p.to_bytes()
    page.verify(orig, new, 2, None)
    new_item = [b for b in page.load(new).blocks if isinstance(b, SceneGroupItemBlock)
                and b.parent_id == ROOT][-1]
    assert new_item.item.left_id == CrdtId(1, 19)  # after the deleted item, as the device sees it
    assert page._root_order(page.load(new).blocks)[-1] == new_item.item.item_id


def test_verify_detects_dropped_tombstone():
    orig, p = _appended_page("More_color_highlight_shader_v3.15.4.2.rm")
    tomb = next(b for b in p.blocks if isinstance(b, SceneTombstoneItemBlock))
    p.blocks.remove(tomb)
    new = p.to_bytes()
    assert _items(_tree(orig)).items() <= _items(_tree(new)).items()  # tree can't see it
    with pytest.raises(page.VerifyError, match="original block"):
        page.verify(orig, new, 3, None)


def test_verify_rejects_mutated_tombstone():
    # (a) no caller-side exemption exists for a modified non-text block
    orig, p = _appended_page("More_color_highlight_shader_v3.15.4.2.rm")
    tomb = next(b for b in p.blocks if isinstance(b, SceneTombstoneItemBlock))
    tomb.item.left_id = CrdtId(0, 0)
    new = p.to_bytes()
    for text in (None, ""):
        with pytest.raises(page.VerifyError, match="original block"):
            page.verify(orig, new, 3, text)


def _ab_with_added_text():
    """Normal_AB page with "\nhi" appended after "B" (1,17), the last char of the original "AB" (ids 16,17)."""
    orig = _data("Normal_AB.rm")
    p = page.load(orig)
    rt = next(b for b in p.blocks if isinstance(b, RootTextBlock))
    old = rt.value.items.sequence_items()[0]
    rt.value.items.add(CrdtSequenceItem(CrdtId(1, 18), CrdtId(1, 17), CrdtId(0, 0), 0, "\nhi"))
    return orig, p, rt


def test_declaring_text_does_not_exempt_other_blocks():
    orig, p, _ = _ab_with_added_text()
    info = next(b for b in p.blocks if type(b).__name__ == "PageInfoBlock")
    info.loads_count += 1  # invisible to the scene tree
    with pytest.raises(page.VerifyError, match="original block"):
        page.verify(orig, p.to_bytes(), 0, "hi")


def test_empty_added_text_gives_no_exemption():
    orig = _data("Normal_AB.rm")
    p = page.load(orig)
    rt = next(b for b in p.blocks if isinstance(b, RootTextBlock))
    rt.value.pos_x += 5
    with pytest.raises(page.VerifyError, match="original block"):
        page.verify(orig, p.to_bytes(), 0, "")


def test_verify_rejects_new_format_item_in_text():
    orig = _data("Normal_AB.rm")
    p = page.load(orig)
    rt = next(b for b in p.blocks if isinstance(b, RootTextBlock))
    old = rt.value.items.sequence_items()[0]
    rt.value.items.add(CrdtSequenceItem(CrdtId(1, 18), CrdtId(0, 0), old.item_id, 0, 1))
    for text in ("", None):  # reviewer's accepted case: now caught by the byte check
        with pytest.raises(page.VerifyError):
            page.verify(orig, p.to_bytes(), 0, text)
    rt.value.items.add(CrdtSequenceItem(CrdtId(1, 19), old.item_id, CrdtId(0, 0), 0, "x"))
    with pytest.raises(page.VerifyError, match="not plain added text"):
        page.verify(orig, p.to_bytes(), 0, "x")


def test_verify_rejects_new_style_on_original_character():
    orig, p, rt = _ab_with_added_text()
    rt.value.styles[CrdtId(1, 16)] = LwwValue(CrdtId(1, 30), si.ParagraphStyle.HEADING)
    with pytest.raises(page.VerifyError, match="not an added character"):
        page.verify(orig, p.to_bytes(), 0, "hi")
    for text in ("", None):  # reviewer's accepted case
        with pytest.raises(page.VerifyError):
            page.verify(orig, p.to_bytes(), 0, text)


def test_verify_accepts_new_style_on_added_character():
    orig, p, rt = _ab_with_added_text()
    rt.value.styles[CrdtId(1, 18)] = LwwValue(CrdtId(1, 30), si.ParagraphStyle.HEADING)
    page.verify(orig, p.to_bytes(), 0, "hi")


def test_verify_rejects_dropped_text_extra_data():
    p0 = page.load(_data("Normal_AB.rm"))
    next(b for b in p0.blocks if isinstance(b, RootTextBlock)).extra_data = b"\x01\x02"
    orig = p0.to_bytes()
    p = page.load(orig)
    rt = next(b for b in p.blocks if isinstance(b, RootTextBlock))
    assert rt.extra_data == b"\x01\x02"
    old = rt.value.items.sequence_items()[0]
    rt.value.items.add(CrdtSequenceItem(CrdtId(1, 18), CrdtId(1, 17), CrdtId(0, 0), 0, "\nhi"))
    page.verify(orig, p.to_bytes(), 0, "hi")  # kept: fine
    rt.extra_data = b""
    with pytest.raises(page.VerifyError, match="unparsed data"):
        page.verify(orig, p.to_bytes(), 0, "hi")


def test_verify_rejects_dropped_text_block_when_adding_text():
    # (b) the exempt RootTextBlock must still be present
    orig = _data("Normal_AB.rm")
    p = page.load(orig)
    p.blocks = [b for b in p.blocks if not isinstance(b, RootTextBlock)]
    with pytest.raises(page.VerifyError):
        page.verify(orig, p.to_bytes(), 0, "AB")


def test_verify_signature_has_no_exemption_parameter():
    import inspect
    assert list(inspect.signature(page.verify).parameters) == [
        "original", "new", "added_lines", "added_text"]


# --- Task 4: reuse the page's rmdesk layer -----------------------------------

RM329 = "user-3.29.1.0-rmdesk-test.rm"
RM329_LAYER = CrdtId(1, 16)  # existing rmdesk layer node (lines in sub-groups (1,295), (1,548))


def _count(data: bytes, kind) -> int:
    return sum(isinstance(b, kind) for b in page.load(data).blocks)


def _layer_lines(data: bytes, node_id) -> list:
    """Line items directly under node `node_id`, in rmscene's tree order."""
    tree = _tree(data)
    return [v for v in tree[node_id].children.values() if isinstance(v, si.Line)]


def _reused(name: str = RM329, n: int = 3) -> tuple[bytes, "page.Page"]:
    orig = _data(name)
    p = page.load(orig)
    p.append_to_rmdesk_layer(_lines(n))
    return orig, p


def test_append_to_existing_rmdesk_layer():
    orig, p = _reused(RM329, 3)
    new = p.to_bytes()
    page.verify(orig, new, 3, None)
    for kind in (TreeNodeBlock, SceneTreeBlock, SceneGroupItemBlock, SceneTombstoneItemBlock):
        assert _count(new, kind) == _count(orig, kind)
    assert _count(new, SceneLineItemBlock) == _count(orig, SceneLineItemBlock) + 3
    old_raw, new_raw = _raw_blocks(orig), _raw_blocks(new)
    assert new_raw[: len(old_raw)] == old_raw  # every original block untouched, lines at the end
    added = [b for b in page.load(new).blocks[len(old_raw):]]
    assert all(isinstance(b, SceneLineItemBlock) and b.parent_id == RM329_LAYER for b in added)
    # chained after the layer's last child (group item (1,549)), right link END
    assert added[0].item.left_id == CrdtId(1, 549)
    assert [b.item.left_id for b in added[1:]] == [b.item.item_id for b in added[:-1]]
    assert all(b.item.right_id == CrdtId(0, 0) for b in added)
    assert [b.item.value for b in added] == _lines(3)
    assert _layer_lines(new, RM329_LAYER) == _lines(3)
    assert _root_labels(_tree(new)) == _root_labels(_tree(orig))  # no new layer
    # xochitl's sub-groups keep every original line
    for sub in (CrdtId(1, 295), CrdtId(1, 548)):
        assert _tree(new)[sub].children.values() == _tree(orig)[sub].children.values()


def test_append_to_rmdesk_layer_twice():
    _, p = _reused(RM329, 2)
    first = p.to_bytes()
    p2 = page.load(first)
    p2.append_to_rmdesk_layer(_lines(1))
    second = p2.to_bytes()
    page.verify(first, second, 1, None)
    assert _layer_lines(second, RM329_LAYER) == _lines(2) + _lines(1)


@pytest.mark.parametrize("name", [f for f in FIXTURES if f != RM329])
def test_creates_layer_when_absent(name):
    orig, p = _reused(name, 3)
    new = p.to_bytes()
    page.verify(orig, new, 3, None)
    labels = _root_labels(_tree(new))
    assert labels == _root_labels(_tree(orig)) + ["rmdesk"]
    assert _tree(new).root.children.values()[-1].children.values() == _lines(3)
    assert _layout(new) == _layout(_appended(name)[1])  # same block layout as append_layer


def test_last_rmdesk_layer_used_when_several():
    _, two = _appended("Normal_A_stroke_2_layers_v3.3.2.rm", 1)
    p = page.load(two)
    p.append_layer("rmdesk", _lines(1))
    orig = p.to_bytes()
    layers = _tree(orig).root.children.values()
    assert [g.label.value for g in layers][-2:] == ["rmdesk", "rmdesk"]
    first, last = layers[-2].node_id, layers[-1].node_id
    p = page.load(orig)
    p.append_to_rmdesk_layer(_lines(2))
    new = p.to_bytes()
    page.verify(orig, new, 2, None)
    assert _layer_lines(new, last) == _lines(1) + _lines(2)
    assert _layer_lines(new, first) == _lines(1)


def test_last_rmdesk_layer_in_root_order_not_file_order():
    # two rmdesk layers whose group items appear in the file in the reverse of
    # their CRDT order: the one that is last in root order is used
    _, two = _appended("Lines_v2.rm", 1)
    p = page.load(two)
    p.append_layer("rmdesk", _lines(1))
    gis = [b for b in p.blocks if isinstance(b, SceneGroupItemBlock) and b.parent_id == ROOT]
    a, b = gis[-2], gis[-1]
    i, j = p.blocks.index(a), p.blocks.index(b)
    p.blocks[i], p.blocks[j] = b, a
    orig = p.to_bytes()
    target = b.item.value  # still last in CRDT order (left link to a)
    assert page._root_order(page.load(orig).blocks)[-1] == b.item.item_id
    p = page.load(orig)
    p.append_to_rmdesk_layer(_lines(1))
    new = p.to_bytes()
    page.verify(orig, new, 1, None)
    assert _layer_lines(new, target) == _lines(1) + _lines(1)


def test_nested_rmdesk_group_is_not_a_layer():
    # only a sub-group (not a child of the root) is labelled rmdesk: a new layer is made
    p0 = page.load(_data(RM329))
    for b in p0.blocks:
        if isinstance(b, TreeNodeBlock) and b.group.node_id == RM329_LAYER:
            b.group.label = LwwValue(CrdtId(1, 998), "mine")
        if isinstance(b, TreeNodeBlock) and b.group.node_id == CrdtId(1, 295):
            b.group.label = LwwValue(CrdtId(1, 999), "rmdesk")
    orig = p0.to_bytes()
    p = page.load(orig)
    p.append_to_rmdesk_layer(_lines(1))
    new = p.to_bytes()
    page.verify(orig, new, 1, None)
    assert _root_labels(_tree(new)) == ["Layer 1", "mine", "rmdesk"]
    assert _count(new, TreeNodeBlock) == _count(orig, TreeNodeBlock) + 1


def test_deleted_rmdesk_layer_is_not_reused():
    _, one = _appended("Lines_v2.rm", 1)
    p = page.load(one)
    gi = [b for b in p.blocks if isinstance(b, SceneGroupItemBlock) and b.parent_id == ROOT][-1]
    gi.item.value = None
    gi.item.deleted_length = 1
    orig = p.to_bytes()
    assert "rmdesk" not in _root_labels(_tree(orig))
    p = page.load(orig)
    p.append_to_rmdesk_layer(_lines(1))
    new = p.to_bytes()
    page.verify(orig, new, 1, None)
    assert _count(new, TreeNodeBlock) == _count(orig, TreeNodeBlock) + 1
    nb = page.load(new).blocks
    top = page._root_order(nb)[-1]  # rmscene's tree ignores the link to the deleted item
    gi = next(b for b in nb if isinstance(b, SceneGroupItemBlock) and b.item.item_id == top)
    assert gi.item.value not in {b.group.node_id for b in page.load(orig).blocks
                                 if isinstance(b, TreeNodeBlock)}
    assert _tree(new)[gi.item.value].label.value == "rmdesk"


def test_anchor_after_deleted_run():
    # the layer's last child is a deleted run of 5 ids: hang the new line off
    # its last id, as xochitl does ((1,296) hangs off (1,284) in the 3.29 page)
    _, one = _appended("Lines_v2.rm", 2)
    p = page.load(one)
    lb = [b for b in p.blocks if isinstance(b, SceneLineItemBlock)][-1]
    lb.item.value = None
    lb.item.deleted_length = 5
    orig = p.to_bytes()
    run = lb.item.item_id
    p = page.load(orig)
    p.append_to_rmdesk_layer(_lines(1))
    new = p.to_bytes()
    page.verify(orig, new, 1, None)
    added = page.load(new).blocks[-1]
    assert added.item.left_id == CrdtId(run.part1, run.part2 + 4)
    assert added.item.item_id.part2 > run.part2 + 4


def test_append_to_rmdesk_layer_rejects_bad_lines():
    p = page.load(_data(RM329))
    with pytest.raises(ValueError):
        p.append_to_rmdesk_layer([])
    bad = _lines(1)
    bad[0].points[0].width = -1
    with pytest.raises(ValueError):
        p.append_to_rmdesk_layer(bad)
    assert p.to_bytes() == _data(RM329)


def test_verify_rejects_line_added_elsewhere():
    # lines under the existing NON-rmdesk layer "Layer 1" (0,11)
    orig, p = _reused(RM329, 2)
    for b in p.blocks[-2:]:
        b.parent_id = CrdtId(0, 11)
    p.blocks[-2].item.left_id = CrdtId(1, 572)
    with pytest.raises(page.VerifyError, match="not under the page's rmdesk layer"):
        page.verify(orig, p.to_bytes(), 2, None)


def test_verify_rejects_line_added_in_xochitl_subgroup():
    orig, p = _reused(RM329, 1)
    p.blocks[-1].parent_id = CrdtId(1, 295)
    p.blocks[-1].item.left_id = CrdtId(1, 547)
    with pytest.raises(page.VerifyError, match="not under the page's rmdesk layer"):
        page.verify(orig, p.to_bytes(), 1, None)


def test_verify_rejects_lines_split_between_parents():
    orig, p = _reused(RM329, 2)
    p.blocks[-1].parent_id = CrdtId(1, 295)
    p.blocks[-1].item.left_id = CrdtId(1, 547)
    with pytest.raises(page.VerifyError, match="not all under one layer"):
        page.verify(orig, p.to_bytes(), 2, None)


def test_verify_rejects_relabelled_rmdesk_layer():
    orig, p = _reused(RM329, 1)
    node = next(b for b in p.blocks if isinstance(b, TreeNodeBlock)
                and b.group.node_id == RM329_LAYER)
    node.group.label = LwwValue(node.group.label.timestamp, "renamed")
    try:
        new = p.to_bytes()
    except page.UnsupportedPage:  # cannot even be written: also safe
        return
    with pytest.raises(page.VerifyError):
        page.verify(orig, new, 1, None)


def test_verify_rejects_hidden_rmdesk_layer():
    orig, p = _reused(RM329, 1)
    node = next(b for b in p.blocks if isinstance(b, TreeNodeBlock)
                and b.group.node_id == RM329_LAYER)
    node.group.visible = LwwValue(node.group.visible.timestamp, False)
    try:
        new = p.to_bytes()
    except page.UnsupportedPage:
        return
    with pytest.raises(page.VerifyError):
        page.verify(orig, new, 1, None)


@pytest.mark.parametrize("iid", [CrdtId(1, 300), CrdtId(1, 553), CrdtId(1, 576)])
def test_verify_rejects_changed_original_line(iid):
    # an original line in each xochitl sub-group and in Layer 1, moved by one unit
    orig, p = _reused(RM329, 2)
    line = next(b for b in p.blocks if isinstance(b, SceneLineItemBlock)
                and b.item.item_id == iid)
    line.item.value.points[0].x += 1.0
    with pytest.raises(page.VerifyError, match="original block"):
        page.verify(orig, p.to_bytes(), 2, None)


def test_verify_rejects_dropped_original_line():
    orig, p = _reused(RM329, 1)
    victim = next(b for b in p.blocks if isinstance(b, SceneLineItemBlock)
                  and b.item.item_id == CrdtId(1, 300))
    p.blocks.remove(victim)
    with pytest.raises(page.VerifyError):
        page.verify(orig, p.to_bytes(), 1, None)


def test_verify_rejects_new_lines_not_last():
    orig, p = _reused(RM329, 2)
    first = p.blocks[-2]
    first.item.left_id = CrdtId(1, 296)
    first.item.right_id = CrdtId(1, 549)  # between the two sub-groups
    with pytest.raises(page.VerifyError, match="not one chain at the end"):
        page.verify(orig, p.to_bytes(), 2, None)


def test_verify_rejects_new_line_with_right_link():
    orig, p = _reused(RM329, 1)
    p.blocks[-1].item.right_id = CrdtId(1, 549)
    p.blocks[-1].item.left_id = CrdtId(0, 0)
    with pytest.raises(page.VerifyError, match="not one chain at the end"):
        page.verify(orig, p.to_bytes(), 1, None)


def test_verify_rejects_new_group_under_rmdesk_layer():
    # a new sub-group (tree + node + group item) under the existing layer, lines in it
    orig, p = _reused(RM329, 1)
    ids = page._IdAllocator(p.blocks, 1)
    sub = ids.next()
    p.blocks.insert(9, SceneTreeBlock(tree_id=sub, node_id=CrdtId(0, 0), is_update=True,
                                      parent_id=RM329_LAYER))
    p.blocks.append(TreeNodeBlock(si.Group(node_id=sub, label=LwwValue(ids.next(), ""),
                                           visible=LwwValue(ids.next(), True))))
    p.blocks.append(SceneGroupItemBlock(parent_id=RM329_LAYER, item=CrdtSequenceItem(
        ids.next(), CrdtId(1, 549), CrdtId(0, 0), 0, sub)))
    line = p.blocks[-3]
    assert isinstance(line, SceneLineItemBlock) and line.parent_id == RM329_LAYER
    p.blocks.remove(line)
    line.parent_id = sub
    line.item.left_id = CrdtId(0, 0)
    p.blocks.append(line)
    try:
        new = p.to_bytes()
    except page.UnsupportedPage:
        pytest.fail("test page should be writable")
    page.load(new)
    with pytest.raises(page.VerifyError, match="not a child of the root"):
        page.verify(orig, new, 1, None)


def test_verify_rejects_new_group_item_reusing_subgroup():
    # a second group item for xochitl's existing sub-group (1,548), plus the lines
    orig, p = _reused(RM329, 1)
    nid = page._IdAllocator(p.blocks, 1).next()
    p.blocks.append(SceneGroupItemBlock(parent_id=RM329_LAYER, item=CrdtSequenceItem(
        nid, p.blocks[-1].item.item_id, CrdtId(0, 0), 0, CrdtId(1, 548))))
    with pytest.raises(page.VerifyError, match="not a line"):
        page.verify(orig, p.to_bytes(), 1, None)


def test_verify_rejects_extra_tombstone_under_rmdesk_layer():
    orig, p = _reused(RM329, 1)
    nid = page._IdAllocator(p.blocks, 1).next()
    p.blocks.append(SceneTombstoneItemBlock(parent_id=RM329_LAYER, item=CrdtSequenceItem(
        nid, p.blocks[-1].item.item_id, CrdtId(0, 0), 1, None)))
    with pytest.raises(page.VerifyError, match="other than the new lines"):
        page.verify(orig, p.to_bytes(), 1, None)


def test_verify_rejects_extra_author_block_when_reusing():
    orig, p = _reused(RM329, 1)
    import uuid as _uuid
    p.blocks.insert(0, AuthorIdsBlock(author_uuids={7: _uuid.uuid4()}))
    with pytest.raises(page.VerifyError, match="other than the new lines"):
        page.verify(orig, p.to_bytes(), 1, None)


def test_verify_rejects_wrong_count_when_reusing():
    orig, p = _reused(RM329, 3)
    new = p.to_bytes()
    for n in (0, 2, 4):
        with pytest.raises(page.VerifyError):
            page.verify(orig, new, n, None)


def test_verify_rejects_lines_under_existing_non_last_rmdesk_layer():
    # two rmdesk layers: adding to the lower one is refused (append uses the last)
    _, two = _appended("Normal_A_stroke_2_layers_v3.3.2.rm", 1)
    p = page.load(two)
    p.append_layer("rmdesk", _lines(1))
    orig = p.to_bytes()
    lower = _tree(orig).root.children.values()[-2].node_id
    p = page.load(orig)
    p.append_to_rmdesk_layer(_lines(1))
    lb = p.blocks[-1]
    lb.item.left_id = [b for b in p.blocks if isinstance(b, SceneLineItemBlock)
                       and b.parent_id == lower][-1].item.item_id
    lb.parent_id = lower
    with pytest.raises(page.VerifyError, match="not under the page's rmdesk layer"):
        page.verify(orig, p.to_bytes(), 1, None)


def test_verify_rejects_lines_under_nested_rmdesk_group():
    # the 3.29 page's sub-group (1,295) relabelled "rmdesk" in the original:
    # it is not a root-level layer, so lines may not be added to it
    p0 = page.load(_data(RM329))
    node = next(b for b in p0.blocks if isinstance(b, TreeNodeBlock)
                and b.group.node_id == CrdtId(1, 295))
    node.group.label = LwwValue(CrdtId(1, 999), "rmdesk")
    try:
        orig = p0.to_bytes()
    except page.UnsupportedPage:
        pytest.skip("cannot build the test page")
    p = page.load(orig)
    p.append_to_rmdesk_layer(_lines(1))
    assert p.blocks[-1].parent_id == RM329_LAYER
    p.blocks[-1].parent_id = CrdtId(1, 295)
    p.blocks[-1].item.left_id = CrdtId(1, 547)
    with pytest.raises(page.VerifyError, match="not under the page's rmdesk layer"):
        page.verify(orig, p.to_bytes(), 1, None)


@pytest.mark.parametrize("name", FIXTURES)
def test_reuse_or_create_on_every_fixture(name):
    orig, p = _reused(name, 2)
    new = p.to_bytes()
    page.verify(orig, new, 2, None)
    old_raw, new_raw = _raw_blocks(orig), _raw_blocks(new)
    it = iter(new_raw)
    assert all(any(r == n for n in it) for r in old_raw)


# -- hidden rmdesk layer (the live Compose confirm warns about it) ------------------

def _hide_rmdesk_layer(data: bytes) -> bytes:
    p = page.load(data)
    p.append_layer("rmdesk", _lines(1))
    node = [b for b in p.blocks if isinstance(b, TreeNodeBlock)
            and b.group.label and b.group.label.value == "rmdesk"][-1]
    node.group.visible = LwwValue(node.group.visible.timestamp, False)
    return p.to_bytes()


def test_rmdesk_layer_hidden():
    assert not page.load(_data("Normal_AB.rm")).rmdesk_layer_hidden()  # no rmdesk layer
    assert not page.load(_data(RM329)).rmdesk_layer_hidden()  # visible rmdesk layer
    assert page.load(_hide_rmdesk_layer(_data("Normal_AB.rm"))).rmdesk_layer_hidden()
