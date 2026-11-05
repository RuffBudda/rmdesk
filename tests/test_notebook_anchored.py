"""New lines in ONE new text-anchored group under the rmdesk layer, and verify's rules for it."""
import copy
import io
import struct
from pathlib import Path

import pytest
import rmscene
from rmscene import scene_items as si
from rmscene.crdt_sequence import CrdtSequenceItem
from rmscene.scene_stream import (
    SceneGroupItemBlock, SceneLineItemBlock, SceneTreeBlock, TreeNodeBlock,
)
from rmscene.tagged_block_common import CrdtId, LwwValue

from rmdesk.notebook import page
from rmdesk.notebook.page import VerifyError

FIX = Path(__file__).parent / "fixtures" / "rm"
RM329 = "user-3.29.1.0-rmdesk-test.rm"  # rmdesk layer (1,16) with groups anchored to text
RM329_LAYER = CrdtId(1, 16)
RM329_ANCHOR = CrdtId(1, 288)  # group (1,295), in the rmdesk layer
LAYER1_ANCHOR = CrdtId(1, 290)  # group (1,571), in Layer 1
NO_RMDESK = "Normal_A_stroke_2_layers_v3.3.2.rm"  # anchored groups, no rmdesk layer
END = CrdtId(0, 0)


def _data(name):
    return (FIX / name).read_bytes()


def _tree(data):
    return rmscene.read_tree(io.BytesIO(data))


def _raw_blocks(data):
    out, i = [], 43
    while i < len(data):
        n = struct.unpack_from("<I", data, i)[0]
        out.append(data[i:i + 8 + n])
        i += 8 + n
    return out


def _lines(n=3):
    return [si.Line(color=si.PenColor.BLACK, tool=si.Pen.FINELINER_2,
                    points=[si.Point(100.0 + 10 * i + k, -40.5 + k, 0, 0, 16, 220)
                            for k in range(5)],
                    thickness_scale=1.0, starting_length=0.0) for i in range(n)]


def _anchored(name=RM329, anchor=RM329_ANCHOR, n=3):
    orig = _data(name)
    p = page.load(orig)
    p.append_anchored_group(_lines(n), anchor)
    return orig, p


def _new_group(new, old):
    t_new, t_old = _tree(new), _tree(old)
    old_ids = set(t_old._node_ids)
    (g,) = [t_new[i] for i in t_new._node_ids if i not in old_ids
            and t_new[i].anchor_id is not None]
    return g


# -- writing ----------------------------------------------------------------------

def test_anchor_template_reads_an_existing_group():
    p = page.load(_data(RM329))
    g = p.anchor_template(RM329_ANCHOR)
    assert g.anchor_id.value == RM329_ANCHOR
    assert (g.anchor_type.value, g.anchor_origin_x.value) == (2, -464.0)
    assert p.anchor_template(CrdtId(1, 99999)) is None
    assert RM329_ANCHOR in p.anchor_ids() and LAYER1_ANCHOR in p.anchor_ids()


def test_append_anchored_group_rereads_with_the_reference_anchor():
    orig, p = _anchored()
    new = p.to_bytes()
    page.verify(orig, new, 3, None)
    g = _new_group(new, orig)
    ref = page.load(orig).anchor_template(RM329_ANCHOR)
    assert (g.anchor_id.value, g.anchor_type.value, g.anchor_threshold.value,
            g.anchor_origin_x.value) == (ref.anchor_id.value, ref.anchor_type.value,
                                         ref.anchor_threshold.value, ref.anchor_origin_x.value)
    assert (g.label.value, g.visible.value) == ("", True)
    # points are written exactly as given: local to the group
    got = [[(pt.x, pt.y) for pt in ln.points] for ln in g.children.values()]
    assert got == [[(pt.x, pt.y) for pt in ln.points] for ln in _lines(3)]
    # the group hangs last in the rmdesk layer
    tree = _tree(new)
    assert list(tree[RM329_LAYER].children.values())[-1].node_id == g.node_id


def test_new_group_blocks_mirror_the_device():
    """Rebuild a device group of the fixture with the writer's own block maker: the same
    bytes (ids and timestamps convention: origin_x stamped with the node id, the group item
    node+1, anchor id/type/threshold node+2..4, the label/visible stamps (0,0))."""
    orig = _data(RM329)
    raw = _raw_blocks(orig)
    blocks = page.load(orig).blocks
    want = {}
    for b, r in zip(blocks, raw):
        if isinstance(b, SceneTreeBlock) and b.tree_id == CrdtId(1, 295):
            want["tree"] = r
        elif isinstance(b, TreeNodeBlock) and b.group.node_id == CrdtId(1, 295):
            want["node"] = (b.group, r)
        elif isinstance(b, SceneGroupItemBlock) and b.item.value == CrdtId(1, 295):
            want["item"] = (b.item, r)
    tmpl, node_raw = want["node"]
    item, item_raw = want["item"]
    tree_b, node_b, item_b = page._anchored_group_blocks(
        CrdtId(1, 295), RM329_LAYER, item.left_id, tmpl)
    p = page.load(orig)
    assert page._encode(tree_b, "3.1") == want["tree"]
    assert page._encode(node_b, p._tree_node_version) == node_raw
    assert page._encode(item_b, "3.1") == item_raw


def test_new_group_block_layout_and_ids():
    orig, p = _anchored()
    new = p.to_bytes()
    old_raw, new_raw = _raw_blocks(orig), _raw_blocks(new)
    nb = page.load(new).blocks
    added = [b for b, r in zip(nb, new_raw) if r not in old_raw]
    kinds = [type(b) for b in added]
    assert kinds == [SceneTreeBlock, TreeNodeBlock, SceneGroupItemBlock] + [SceneLineItemBlock] * 3
    t, n, g = added[:3]
    gid = t.tree_id
    assert (t.node_id, t.is_update, t.parent_id) == (END, True, RM329_LAYER)
    assert n.group.node_id == gid
    assert n.group.anchor_origin_x.timestamp == gid
    assert g.item.item_id == CrdtId(1, gid.part2 + 1) and g.item.value == gid
    assert [n.group.anchor_id.timestamp.part2, n.group.anchor_type.timestamp.part2,
            n.group.anchor_threshold.timestamp.part2] == [gid.part2 + 2, gid.part2 + 3,
                                                          gid.part2 + 4]
    lines = added[3:]
    assert all(b.parent_id == gid for b in lines)
    assert lines[0].item.left_id == END
    assert [b.item.left_id for b in lines[1:]] == [b.item.item_id for b in lines[:-1]]
    assert all(b.item.right_id == END for b in lines)
    # each new block sits after the last original block of its kind (device layout)
    for b in added[:3]:
        i = nb.index(b)
        assert all(not isinstance(x, type(b)) for x in nb[i + 1:]) or isinstance(b, SceneLineItemBlock)


def test_append_anchored_group_creates_the_rmdesk_layer():
    orig, p = _anchored(NO_RMDESK, CrdtId(1, 14))
    new = p.to_bytes()
    page.verify(orig, new, 3, None)
    tree = _tree(new)
    top = list(tree.root.children.values())[-1]
    assert top.label.value == "rmdesk"
    (g,) = list(top.children.values())
    assert g.anchor_id.value == CrdtId(1, 14) and len(list(g.children.values())) == 3


def test_append_anchored_group_refuses_an_unknown_anchor():
    p = page.load(_data(RM329))
    with pytest.raises(ValueError):
        p.append_anchored_group(_lines(1), CrdtId(1, 99999))
    with pytest.raises(ValueError):
        p.append_anchored_group([], RM329_ANCHOR)


def test_anchor_of_another_layer_is_copied():
    """The reference may be a group in another layer: the new group still goes under rmdesk."""
    orig, p = _anchored(RM329, LAYER1_ANCHOR)
    new = p.to_bytes()
    page.verify(orig, new, 3, None)
    g = _new_group(new, orig)
    assert g.anchor_id.value == LAYER1_ANCHOR
    assert list(_tree(new)[RM329_LAYER].children.values())[-1].node_id == g.node_id


# -- verify: what it must reject --------------------------------------------------

def _second_group(p, anchor=RM329_ANCHOR, n=1):
    """Add another anchored group by hand (bypassing the one-group writer)."""
    p.append_anchored_group(_lines(n), anchor)


def test_verify_rejects_two_new_groups():
    orig, p = _anchored(n=2)
    _second_group(p)
    with pytest.raises(VerifyError):
        page.verify(orig, p.to_bytes(), 3, None)


def test_verify_rejects_a_new_group_outside_the_rmdesk_layer():
    orig, p = _anchored()
    for b in p.blocks:
        if isinstance(b, SceneGroupItemBlock) and b.item.value == _gid(p, orig):
            b.parent_id = CrdtId(0, 11)  # under Layer 1
            b.item = CrdtSequenceItem(b.item.item_id, CrdtId(1, 572), END, 0, b.item.value)
        if isinstance(b, SceneTreeBlock) and b.tree_id == _gid(p, orig):
            b.parent_id = CrdtId(0, 11)
    with pytest.raises(VerifyError, match="rmdesk"):
        page.verify(orig, p.to_bytes(), 3, None)


def _gid(p, orig):
    old = {b.tree_id for b in page.load(orig).blocks if isinstance(b, SceneTreeBlock)}
    (g,) = [b.tree_id for b in p.blocks if isinstance(b, SceneTreeBlock) and b.tree_id not in old]
    return g


def _node_block(p, gid):
    (b,) = [b for b in p.blocks if isinstance(b, TreeNodeBlock) and b.group.node_id == gid]
    return b


def test_verify_rejects_an_anchor_not_on_the_page():
    orig, p = _anchored()
    b = _node_block(p, _gid(p, orig))
    b.group.anchor_id = LwwValue(b.group.anchor_id.timestamp, CrdtId(1, 99999))
    with pytest.raises(VerifyError, match="anchor"):
        page.verify(orig, p.to_bytes(), 3, None)


def test_verify_rejects_changed_anchor_fields():
    orig, p = _anchored()
    b = _node_block(p, _gid(p, orig))
    b.group.anchor_origin_x = LwwValue(b.group.anchor_origin_x.timestamp, 0.0)
    with pytest.raises(VerifyError, match="anchor"):
        page.verify(orig, p.to_bytes(), 3, None)


@pytest.mark.parametrize("field,value", [("label", "notes"), ("visible", False)])
def test_verify_rejects_a_labelled_or_hidden_group(field, value):
    orig, p = _anchored()
    b = _node_block(p, _gid(p, orig))
    setattr(b.group, field, LwwValue(getattr(b.group, field).timestamp, value))
    with pytest.raises(VerifyError):
        page.verify(orig, p.to_bytes(), 3, None)


def test_verify_rejects_extra_items_in_the_group():
    orig, p = _anchored()
    gid = _gid(p, orig)
    last = [b for b in p.blocks if isinstance(b, SceneLineItemBlock) and b.parent_id == gid][-1]
    extra = copy.deepcopy(last)
    extra.item = CrdtSequenceItem(CrdtId(1, 99990), last.item.item_id, END, 0, last.item.value)
    p.blocks.append(extra)
    with pytest.raises(VerifyError):
        page.verify(orig, p.to_bytes(), 3, None)  # 4 lines where 3 were declared
    with pytest.raises(VerifyError):  # nor a deleted item, even if counted
        extra.item = CrdtSequenceItem(CrdtId(1, 99990), last.item.item_id, END, 1, None)
        page.verify(orig, p.to_bytes(), 3, None)


def test_verify_rejects_lines_outside_the_new_group():
    orig, p = _anchored()
    p.append_to_rmdesk_layer(_lines(1))
    with pytest.raises(VerifyError):
        page.verify(orig, p.to_bytes(), 4, None)


def test_verify_rejects_a_group_not_last_in_the_layer():
    orig, p = _anchored()
    gid = _gid(p, orig)
    for b in p.blocks:
        if isinstance(b, SceneGroupItemBlock) and b.item.value == gid:
            b.item = CrdtSequenceItem(b.item.item_id, END, CrdtId(1, 296), 0, gid)
    with pytest.raises(VerifyError):
        page.verify(orig, p.to_bytes(), 3, None)


def test_verify_rejects_a_changed_original_group():
    orig, p = _anchored()
    b = _node_block(p, CrdtId(1, 295))
    b.group.anchor_origin_x = LwwValue(b.group.anchor_origin_x.timestamp, -400.0)
    with pytest.raises(VerifyError):  # its block is re-encoded: no longer the original bytes
        page.verify(orig, p.to_bytes(), 3, None)


def test_verify_rejects_a_changed_original_group_bytes():
    """Even if the bytes of an original group were swapped wholesale: refused."""
    orig, p = _anchored()
    new = p.to_bytes()
    raw = _raw_blocks(orig)
    blocks = page.load(orig).blocks
    i = next(i for i, b in enumerate(blocks)
             if isinstance(b, TreeNodeBlock) and b.group.node_id == CrdtId(1, 295))
    g = copy.deepcopy(blocks[i])
    g.group.anchor_threshold = LwwValue(g.group.anchor_threshold.timestamp, 10.0)
    tampered = new.replace(raw[i], page._encode(g, "3.4"))
    assert tampered != new
    with pytest.raises(VerifyError):
        page.verify(orig, tampered, 3, None)


def test_verify_rejects_an_unanchored_new_group():
    orig, p = _anchored()
    b = _node_block(p, _gid(p, orig))
    g = b.group
    g.anchor_id = g.anchor_type = g.anchor_threshold = g.anchor_origin_x = None
    with pytest.raises(VerifyError):
        page.verify(orig, p.to_bytes(), 3, None)


def test_verify_rejects_a_group_without_lines():
    orig, p = _anchored()
    gid = _gid(p, orig)
    p.blocks = [b for b in p.blocks
                if not (isinstance(b, SceneLineItemBlock) and b.parent_id == gid)]
    with pytest.raises(VerifyError):
        page.verify(orig, p.to_bytes(), 3, None)


def test_verify_rejects_a_new_layer_with_an_extra_line_beside_the_group():
    orig, p = _anchored(NO_RMDESK, CrdtId(1, 14))
    layer = page._rmdesk_layers(p.blocks)[-1]
    p.blocks.append(SceneLineItemBlock(layer, CrdtSequenceItem(
        CrdtId(1, 99990), END, END, 0, _lines(1)[0])))
    with pytest.raises(VerifyError):
        page.verify(orig, p.to_bytes(), 4, None)
