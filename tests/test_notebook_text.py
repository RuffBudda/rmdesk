"""Task 10: typed text appended to a notebook page."""
import io
from pathlib import Path

import pytest
import rmscene
from rmscene import scene_items as si
from rmscene.crdt_sequence import CrdtSequenceItem
from rmscene.scene_stream import PageInfoBlock, RootTextBlock, SceneTreeBlock, TreeNodeBlock
from rmscene.tagged_block_common import CrdtId
from rmscene.text import TextDocument

from rmdesk.notebook import page

FIX = Path(__file__).parent / "fixtures" / "rm"
FIXTURES = sorted(p.name for p in FIX.glob("*.rm"))
END = CrdtId(0, 0)


def _data(name: str) -> bytes:
    return (FIX / name).read_bytes()


def _root_text(data: bytes):
    return rmscene.read_tree(io.BytesIO(data)).root_text


def _plain(data: bytes) -> str:
    t = _root_text(data)
    if t is None:
        return ""
    return "\n".join(str(p) for p in TextDocument.from_scene_item(t).contents)


def _has_text_block(name: str) -> bool:
    return any(isinstance(b, RootTextBlock) for b in page.load(_data(name)).blocks)


TEXT_FIXTURES = [n for n in FIXTURES if _has_text_block(n)]
NO_TEXT_FIXTURES = [n for n in FIXTURES if not _has_text_block(n)]


def _appended(name: str, text: str = "Hello") -> tuple[bytes, bytes, page.Page]:
    orig = _data(name)
    p = page.load(orig)
    p.append_text(text)
    return orig, p.to_bytes(), p


def test_fixture_split():
    assert TEXT_FIXTURES and NO_TEXT_FIXTURES
    assert "Normal_AB.rm" in TEXT_FIXTURES
    assert "Lines_v2.rm" in NO_TEXT_FIXTURES


# -- appending ------------------------------------------------------------------

@pytest.mark.parametrize("name", NO_TEXT_FIXTURES)
def test_append_text_to_page_without_text(name):
    orig, new, _ = _appended(name)
    assert _root_text(orig) is None
    assert "Hello" in _plain(new)
    assert _plain(new) == "Hello"
    page.verify(orig, new, 0, "Hello")


@pytest.mark.parametrize("name", NO_TEXT_FIXTURES)
def test_new_text_block_values_and_position(name):
    _, new, _ = _appended(name)
    p = page.load(new)
    kinds = [type(b) for b in p.blocks]
    rt_i = kinds.index(RootTextBlock)
    assert kinds.count(RootTextBlock) == 1
    assert rt_i + 1 == kinds.index(TreeNodeBlock)  # right before the first TreeNodeBlock
    assert all(k is not SceneTreeBlock for k in kinds[rt_i:])  # after every SceneTreeBlock
    rt = p.blocks[rt_i]
    assert rt.block_id == END
    # same block header (min_version 1, current_version 1, type 7) as every fixture's text block
    assert page._split_raw(new)[rt_i][5:8] == bytes([1, 1, RootTextBlock.BLOCK_TYPE])
    t = rt.value
    assert (t.pos_x, t.pos_y, t.width) == (-468.0, 234.0, 936.0)
    assert list(t.styles) == [END]
    assert t.styles[END].value == si.ParagraphStyle.PLAIN
    [item] = t.items.sequence_items()
    assert (item.left_id, item.right_id, item.deleted_length, item.value) == (END, END, 0, "Hello")
    assert item.item_id.part1 == page.AUTHOR


@pytest.mark.parametrize("name", TEXT_FIXTURES)
def test_append_text_keeps_existing_text(name):
    orig, new, _ = _appended(name)
    old = _plain(orig)
    assert old, "fixture has visible text"
    assert _plain(new).startswith(old)
    assert _plain(new).endswith("Hello")
    assert _plain(new) == old + "\nHello"
    page.verify(orig, new, 0, "Hello")


@pytest.mark.parametrize("name", TEXT_FIXTURES)
def test_existing_text_items_untouched(name):
    orig = _data(name)
    before = {i.item_id: i for i in _root_text(orig).items.sequence_items()}
    _, new, _ = _appended(name)
    after = {i.item_id: i for i in _root_text(new).items.sequence_items()}
    assert {k: after[k] for k in before} == before
    [added] = [after[k] for k in after if k not in before]
    assert added.value == "\nHello"
    assert added.right_id == END and added.deleted_length == 0


def test_left_id_is_last_character_not_last_item():
    # Normal_AB: one item (1,16) "AB" = chars (1,16),(1,17). The new paragraph
    # must hang off "B" (1,17); hanging it off (1,16) would be an insert between
    # A and B whose final position depends on the CRDT's tie-break.
    _, new, _ = _appended("Normal_AB.rm")
    [added] = [i for i in _root_text(new).items.sequence_items() if i.item_id != CrdtId(1, 16)]
    assert added.left_id == CrdtId(1, 17)
    assert added.item_id == CrdtId(1, 18)


def test_left_id_skips_deleted_tail_and_format_codes():
    # Bold_Heading_Bullet_Normal: last char in CRDT order is (1,21) ("C" of "\nC").
    _, new, _ = _appended("Bold_Heading_Bullet_Normal.rm")
    orig_ids = {i.item_id for i in _root_text(_data("Bold_Heading_Bullet_Normal.rm")).items.sequence_items()}
    [added] = [i for i in _root_text(new).items.sequence_items() if i.item_id not in orig_ids]
    assert added.left_id == CrdtId(1, 21)
    # v3.3.2 fixture: last item (1,115) holds 34 chars -> last char (1,148)
    _, new, _ = _appended("Normal_A_stroke_2_layers_v3.3.2.rm")
    orig_ids = {i.item_id for i in _root_text(_data("Normal_A_stroke_2_layers_v3.3.2.rm")).items.sequence_items()}
    [added] = [i for i in _root_text(new).items.sequence_items() if i.item_id not in orig_ids]
    assert added.left_id == CrdtId(1, 148)
    assert added.item_id.part2 > 149  # above every id in the file (style timestamp (1,149))


@pytest.mark.parametrize("name", FIXTURES)
def test_page_info_and_other_blocks_unchanged(name):
    orig, new, _ = _appended(name)
    o, n = page.load(orig), page.load(new)
    oi = next(b for b in o.blocks if isinstance(b, PageInfoBlock))
    ni = next(b for b in n.blocks if isinstance(b, PageInfoBlock))
    assert oi == ni
    keep = [r for b, r in zip(o.blocks, page._split_raw(orig)) if not isinstance(b, RootTextBlock)]
    new_raw = page._split_raw(new)
    it = iter(new_raw)
    assert all(any(r == x for x in it) for r in keep)


def test_verify_with_text():
    orig, new, _ = _appended("Lines_v2.rm")
    page.verify(orig, new, 0, "Hello")
    orig, new, _ = _appended("Normal_AB.rm")
    page.verify(orig, new, 0, "Hello")


def test_verify_with_text_rejects_wrong_declaration():
    orig, new, _ = _appended("Normal_AB.rm")
    for wrong in ("Hellp", "\nHello", "Hello\n", None, ""):
        with pytest.raises(page.VerifyError):
            page.verify(orig, new, 0, wrong)
    with pytest.raises(page.VerifyError):
        page.verify(orig, new, 1, "Hello")


def test_multiline_text():
    orig, new, _ = _appended("Normal_AB.rm", "one\ntwo")
    assert _plain(new) == "AB\none\ntwo"
    page.verify(orig, new, 0, "one\ntwo")
    orig, new, _ = _appended("Lines_v2.rm", "one\ntwo")
    assert _plain(new) == "one\ntwo"
    page.verify(orig, new, 0, "one\ntwo")


def test_append_text_twice_on_one_page():
    orig = _data("Normal_AB.rm")
    p = page.load(orig)
    p.append_text("one")
    p.append_text("two")
    new = p.to_bytes()
    assert _plain(new) == "AB\none\ntwo"
    page.verify(orig, new, 0, "one\ntwo")


def test_append_text_on_reloaded_page():
    orig, mid, _ = _appended("Lines_v2.rm", "one")
    p = page.load(mid)
    p.append_text("two")
    new = p.to_bytes()
    assert _plain(new) == "one\ntwo"
    page.verify(mid, new, 0, "two")
    page.verify(orig, new, 0, "one\ntwo")


def _line() -> si.Line:
    return si.Line(
        color=si.PenColor.BLACK, tool=si.Pen.BALLPOINT_2, thickness_scale=2.0,
        starting_length=0.0,
        points=[si.Point(x=0.0, y=100.0, speed=0, direction=0, width=2, pressure=100),
                si.Point(x=10.0, y=110.0, speed=0, direction=0, width=2, pressure=100)],
    )


@pytest.mark.parametrize("name", ["Normal_AB.rm", "Lines_v2.rm", "With_SceneInfo_Block.rm"])
@pytest.mark.parametrize("text_first", [True, False])
def test_text_and_layer_share_one_id_source(name, text_first):
    orig = _data(name)
    p = page.load(orig)
    if text_first:
        p.append_text("Hello")
        p.append_layer(page.LAYER_LABEL, [_line(), _line()])
    else:
        p.append_layer(page.LAYER_LABEL, [_line(), _line()])
        p.append_text("Hello")
    new = p.to_bytes()
    page.verify(orig, new, 2, "Hello")
    # every id/char id the two appends used is distinct and above the original's
    base = page._max_id_part2(page.load(orig).blocks, page.AUTHOR)
    used = []
    for b in page.load(new).blocks:
        item = getattr(b, "item", None)
        if item is not None and item.item_id.part2 > base:
            used.append(item.item_id.part2)
        if isinstance(b, RootTextBlock):
            for it in b.value.items.sequence_items():
                if it.item_id.part2 > base and it.item_id.part1 == page.AUTHOR:
                    used.extend(range(it.item_id.part2, it.item_id.part2 + len(it.value)))
    assert len(used) == len(set(used))


def test_id_allocator():
    blocks = page.load(_data("With_SceneInfo_Block.rm")).blocks
    ids = page._IdAllocator(blocks, page.AUTHOR)
    assert ids.next() == CrdtId(1, 118)  # text span (1,102)+16 chars ends at (1,117)
    assert ids.take(5) == CrdtId(1, 119)
    assert ids.next() == CrdtId(1, 124)


@pytest.mark.parametrize("bad", ["", None, 5, "café", "tab\there", "cr\r\n", "\x00", "smile \U0001F600"])
def test_append_text_rejects_bad_text(bad):
    p = page.load(_data("Normal_AB.rm"))
    before = p.to_bytes()
    with pytest.raises(ValueError):
        p.append_text(bad)
    assert p.to_bytes() == before


def _ambiguous_tail_page() -> bytes:
    """Normal_AB with a second branch after "A": chars (1,17) and (1,50) both
    end the text, so where "the end" is depends on the CRDT tie-break."""
    p = page.load(_data("Normal_AB.rm"))
    rt = next(b for b in p.blocks if isinstance(b, RootTextBlock))
    rt.value.items.add(CrdtSequenceItem(CrdtId(1, 50), CrdtId(1, 16), END, 0, "X"))
    return p.to_bytes()


def test_append_text_refuses_ambiguous_end():
    orig = _ambiguous_tail_page()
    p = page.load(orig)
    with pytest.raises(page.UnsupportedPage):
        p.append_text("Hello")
    assert p.to_bytes() == orig


# -- verify: forged text additions --------------------------------------------

def _ab_forged(*items: CrdtSequenceItem) -> tuple[bytes, bytes]:
    orig = _data("Normal_AB.rm")
    p = page.load(orig)
    rt = next(b for b in p.blocks if isinstance(b, RootTextBlock))
    for it in items:
        rt.value.items.add(it)
    return orig, p.to_bytes()


def test_verify_accepts_correct_forgery():
    orig, new = _ab_forged(CrdtSequenceItem(CrdtId(1, 18), CrdtId(1, 17), END, 0, "\nHello"))
    page.verify(orig, new, 0, "Hello")


def test_verify_rejects_text_hung_off_first_char():
    orig, new = _ab_forged(CrdtSequenceItem(CrdtId(1, 18), CrdtId(1, 16), END, 0, "\nHello"))
    with pytest.raises(page.VerifyError, match="end of the page text"):
        page.verify(orig, new, 0, "Hello")


def test_verify_rejects_text_with_right_link():
    orig, new = _ab_forged(CrdtSequenceItem(CrdtId(1, 18), CrdtId(1, 17), CrdtId(1, 16), 0, "\nHello"))
    with pytest.raises(page.VerifyError):
        page.verify(orig, new, 0, "Hello")


def test_verify_rejects_missing_paragraph_break():
    orig, new = _ab_forged(CrdtSequenceItem(CrdtId(1, 18), CrdtId(1, 17), END, 0, "Hello"))
    with pytest.raises(page.VerifyError):
        page.verify(orig, new, 0, "Hello")


def test_verify_rejects_unchained_items():
    # two new items both hung off the old end: their order is a tie-break
    orig, new = _ab_forged(
        CrdtSequenceItem(CrdtId(1, 18), CrdtId(1, 17), END, 0, "\nHel"),
        CrdtSequenceItem(CrdtId(1, 30), CrdtId(1, 17), END, 0, "lo"),
    )
    with pytest.raises(page.VerifyError, match="end of the page text"):
        page.verify(orig, new, 0, "Hello")
    # chained properly: accepted
    orig, new = _ab_forged(
        CrdtSequenceItem(CrdtId(1, 18), CrdtId(1, 17), END, 0, "\nHel"),
        CrdtSequenceItem(CrdtId(1, 30), CrdtId(1, 21), END, 0, "lo"),
    )
    page.verify(orig, new, 0, "Hello")


def test_verify_rejects_new_item_overlapping_original_chars():
    # R23: (1,17) is the id of "B" inside the original item (1,16) "AB"
    orig, new = _ab_forged(CrdtSequenceItem(CrdtId(1, 17), CrdtId(1, 17), END, 0, "\nHello"))
    with pytest.raises(page.VerifyError, match="overlaps"):
        page.verify(orig, new, 0, "Hello")


def test_verify_rejects_new_item_overlapping_deleted_chars():
    # Bold_Heading_Bullet_Normal has a deleted run (1,26) of length 10 -> ids 26..35
    orig = _data("Bold_Heading_Bullet_Normal.rm")
    p = page.load(orig)
    rt = next(b for b in p.blocks if isinstance(b, RootTextBlock))
    rt.value.items.add(CrdtSequenceItem(CrdtId(1, 30), CrdtId(1, 21), END, 0, "\nHello"))
    with pytest.raises(page.VerifyError, match="overlaps"):
        page.verify(orig, p.to_bytes(), 0, "Hello")


def test_verify_rejects_new_items_overlapping_each_other():
    orig, new = _ab_forged(
        CrdtSequenceItem(CrdtId(1, 18), CrdtId(1, 17), END, 0, "\nHel"),
        CrdtSequenceItem(CrdtId(1, 20), CrdtId(1, 21), END, 0, "lo"),
    )
    with pytest.raises(page.VerifyError, match="overlaps"):
        page.verify(orig, new, 0, "Hello")


def test_verify_rejects_non_ascii_new_text():
    orig, new = _ab_forged(CrdtSequenceItem(CrdtId(1, 18), CrdtId(1, 17), END, 0, "\ncafé"))
    with pytest.raises(page.VerifyError, match="not plain added text"):
        page.verify(orig, new, 0, "café")


def test_verify_rejects_append_to_ambiguous_end():
    orig = _ambiguous_tail_page()
    p = page.load(orig)
    rt = next(b for b in p.blocks if isinstance(b, RootTextBlock))
    rt.value.items.add(CrdtSequenceItem(CrdtId(1, 51), CrdtId(1, 50), END, 0, "\nHello"))
    with pytest.raises(page.VerifyError, match="end of the page text"):
        page.verify(orig, p.to_bytes(), 0, "Hello")


def test_verify_new_text_block_must_start_at_beginning():
    orig = _data("Lines_v2.rm")
    p = page.load(orig)
    p.append_text("Hello")
    rt = next(b for b in p.blocks if isinstance(b, RootTextBlock))
    [it] = rt.value.items.sequence_items()
    it.left_id = CrdtId(1, 5)  # dangling link instead of the start marker
    with pytest.raises(page.VerifyError, match="end of the page text"):
        page.verify(orig, p.to_bytes(), 0, "Hello")
