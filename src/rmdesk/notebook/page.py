"""Load a v6 .rm notebook page, append an `rmdesk` layer with lines and/or a
paragraph of typed text, and verify that nothing that was already on the page
changed. Pure bytes in / bytes out.

Safety model
------------
rmscene's writer cannot reproduce every real page byte-for-byte: a single
`version` option controls the encoding of every block, real pages mix block
header versions (e.g. line blocks with min_version 1 and 2 in one file, which
rmscene always writes as 2), and some fields (the line timestamp) are not
kept at all. So `Page.to_bytes()` never
re-encodes a block it loaded unchanged: it copies that block's original bytes
verbatim and only serializes blocks that are new (or that a caller modified,
in which case it re-encodes with the writer option that reproduces the
original block exactly, or refuses). `verify()` then re-reads both files and
checks the trees.
"""
from __future__ import annotations

import collections
import copy
import dataclasses
import io
import struct
import uuid
from typing import Any

import rmscene
from packaging.version import Version
from rmscene import scene_items as si
from rmscene.crdt_sequence import CrdtSequence, CrdtSequenceItem, toposort_items
from rmscene.scene_stream import (
    AuthorIdsBlock,
    Block,
    RootTextBlock,
    SceneGroupItemBlock,
    SceneItemBlock,
    SceneLineItemBlock,
    SceneTombstoneItemBlock,
    SceneTreeBlock,
    TreeNodeBlock,
    UnreadableBlock,
)
from rmscene.tagged_block_common import HEADER_V6, CrdtId, LwwValue
from rmscene.text import expand_text_items
from rmscene.tagged_block_writer import TaggedBlockWriter

PAGE_W = 1404
PAGE_H = 1872
X_MIN = -702

LAYER_LABEL = "rmdesk"
AUTHOR = 1  # the local-device author id
ROOT_ID = CrdtId(0, 1)
END = CrdtId(0, 0)

_BLOCK_HEAD = 8  # uint32 length + unknown, min_version, current_version, type
_TREE_NODE_TYPE = TreeNodeBlock.BLOCK_TYPE
# A new page text box (no text on the page yet): rmscene.simple_text_document's values.
TEXT_POS_X = -468.0
TEXT_POS_Y = 234.0
TEXT_WIDTH = 936.0

# Writer options tried, in order, to re-encode a modified original block.
_CANDIDATE_VERSIONS = ("3.1", "3.0", "3.2.2", "3.4", "9999")


class UnsupportedPage(Exception):
    """The page is not a v6 file rmdesk can safely rewrite."""


class VerifyError(Exception):
    """The rewritten page does not preserve the original content."""


def _encode(block: Block, version: str) -> bytes:
    buf = io.BytesIO()
    block.write(TaggedBlockWriter(buf, options={"version": Version(version)}))
    return buf.getvalue()


def _split_raw(data: bytes) -> list[bytes]:
    """Split a v6 file into its raw top-level blocks; every byte must belong to one."""
    out: list[bytes] = []
    i = len(HEADER_V6)
    while i < len(data):
        if i + _BLOCK_HEAD > len(data):
            raise UnsupportedPage("truncated block header")
        n = struct.unpack_from("<I", data, i)[0]
        end = i + _BLOCK_HEAD + n
        if end > len(data):
            raise UnsupportedPage("truncated block")
        out.append(data[i:end])
        i = end
    return out


@dataclasses.dataclass
class _Original:
    block: Block      # the live object (kept so its id() stays unique)
    snapshot: Block   # deep copy taken at load time
    raw: bytes        # exact bytes of the block in the loaded file


class Page:
    def __init__(self, blocks: list[Block], raw: bytes, raw_blocks: list[bytes]):
        self.blocks: list[Block] = blocks
        self.raw: bytes = raw
        self._header = raw[: len(HEADER_V6)]
        self._orig: dict[int, _Original] = {
            id(b): _Original(b, copy.deepcopy(b), r) for b, r in zip(blocks, raw_blocks)
        }
        # Existing TreeNodeBlocks' header version (1 or 2) -> new ones match it.
        versions = [r[6] for r in raw_blocks if r[7] == _TREE_NODE_TYPE]
        self._tree_node_version = "3.4" if versions and max(versions) >= 2 else "3.1"

    # -- writing --------------------------------------------------------------

    def _new_block_version(self, block: Block) -> str:
        if isinstance(block, TreeNodeBlock):
            return self._tree_node_version
        # "3.1": line blocks with v2 points, the format of every fixture newer
        # than firmware 3.0 (block headers are per block, so a page may mix them);
        # the other new block types encode the same under any version option.
        return "3.1"

    def _reencode_modified(self, orig: _Original) -> bytes:
        for version in _CANDIDATE_VERSIONS:
            if _encode(orig.snapshot, version) == orig.raw:
                return _encode(orig.block, version)
        raise UnsupportedPage(
            f"cannot safely re-encode modified {type(orig.block).__name__}"
        )

    def to_bytes(self) -> bytes:
        parts = [self._header]
        for block in self.blocks:
            orig = self._orig.get(id(block))
            if orig is not None and orig.block is block:
                if block == orig.snapshot:
                    parts.append(orig.raw)
                else:
                    parts.append(self._reencode_modified(orig))
            else:
                parts.append(_encode(block, self._new_block_version(block)))
        return b"".join(parts)

    # -- appending ------------------------------------------------------------

    def _ensure_author(self) -> None:
        authors = [b for b in self.blocks if isinstance(b, AuthorIdsBlock)]
        if not authors:
            self.blocks.insert(0, AuthorIdsBlock(author_uuids={AUTHOR: uuid.uuid4()}))
        elif not any(AUTHOR in b.author_uuids for b in authors):
            authors[0].author_uuids[AUTHOR] = uuid.uuid4()

    def _insert_layer_blocks(self, tree_b: Block, node_b: Block, group_b: Block) -> None:
        """Place each new block after the last existing block of its type, as
        xochitl lays pages out (T* R N* G* L*). If a type is missing or the
        types are not in that order, put all three right after the last
        SceneGroupItemBlock (else TreeNode, else SceneTree, else at the end)."""
        last = {}
        for i, b in enumerate(self.blocks):
            last[type(b)] = i
        t, n, g = (last.get(k) for k in (SceneTreeBlock, TreeNodeBlock, SceneGroupItemBlock))
        if t is not None and n is not None and g is not None and t < n < g:
            # insert from the back so earlier indices stay valid
            self.blocks.insert(g + 1, group_b)
            self.blocks.insert(n + 1, node_b)
            self.blocks.insert(t + 1, tree_b)
            return
        at = len(self.blocks)
        for kind in (SceneGroupItemBlock, TreeNodeBlock, SceneTreeBlock):
            if kind in last:
                at = last[kind] + 1
                break
        self.blocks[at:at] = [tree_b, node_b, group_b]

    def _add_layer(self, label: str, next_id) -> CrdtId:
        """Add the blocks of a new top layer `label` (visible, no children); its node id."""
        layer_id = next_id()
        tree_block = SceneTreeBlock(
            tree_id=layer_id, node_id=END, is_update=True, parent_id=ROOT_ID
        )
        node_block = TreeNodeBlock(
            si.Group(
                node_id=layer_id,
                label=LwwValue(next_id(), label),
                visible=LwwValue(next_id(), True),
            )
        )
        order = _root_order(self.blocks)
        left = order[-1] if order else END
        group_block = SceneGroupItemBlock(
            parent_id=ROOT_ID,
            item=CrdtSequenceItem(
                item_id=next_id(), left_id=left, right_id=END, deleted_length=0,
                value=layer_id,
            ),
        )
        self._insert_layer_blocks(tree_block, node_block, group_block)
        return layer_id

    def _append_lines(self, parent: CrdtId, prev: CrdtId, lines: list[si.Line],
                      next_id) -> None:
        """Append `lines` under `parent` at the end of the file, chained from `prev`."""
        for line in lines:
            iid = next_id()
            self.blocks.append(
                SceneLineItemBlock(
                    parent_id=parent,
                    item=CrdtSequenceItem(iid, prev, END, 0, copy.deepcopy(line)),
                )
            )
            prev = iid

    def append_layer(self, label: str, lines: list[si.Line]) -> None:
        """Add a new top layer `label` (visible) holding `lines`, in that order."""
        if not lines:
            raise ValueError("no lines to append")
        for line in lines:
            _check_line(line)
        self._ensure_author()

        next_id = _IdAllocator(self.blocks, AUTHOR).next
        layer_id = self._add_layer(label, next_id)
        self._append_lines(layer_id, END, lines, next_id)

    def append_to_rmdesk_layer(self, lines: list[si.Line]) -> None:
        """Add `lines` to the page's `rmdesk` layer, creating it if there is none.

        The layer reused is the last one in root CRDT order among the root's
        live children labelled exactly `rmdesk` (see `_rmdesk_layers`; hidden
        layers count). The lines go directly under that layer node, chained
        after its right-most child in CRDT order (line items, group items and
        tombstones alike; see `_tail_anchor`), with right_id END. Only new
        SceneLineItemBlocks are appended at the end of the file: no other
        block is added or changed (xochitl's sub-groups and their lines are
        untouched). With no such layer this is `append_layer("rmdesk", lines)`.
        """
        if not lines:
            raise ValueError("no lines to append")
        for line in lines:
            _check_line(line)
        layers = _rmdesk_layers(self.blocks)
        if not layers:
            self.append_layer(LAYER_LABEL, lines)
            return
        layer_id = layers[-1]
        try:
            prev = _tail_anchor(self.blocks, layer_id)
        except ValueError as e:  # cyclic CRDT links under the layer
            raise UnsupportedPage(f"cannot order the rmdesk layer: {e}") from e
        self._ensure_author()
        next_id = _IdAllocator(self.blocks, AUTHOR).next
        self._append_lines(layer_id, prev, lines, next_id)

    def anchor_ids(self) -> set[CrdtId]:
        """Every anchor_id used by a group on the page (as rmscene reads the groups)."""
        return {g.anchor_id.value for g in _groups(self.blocks).values()
                if g.anchor_id is not None}

    def anchor_template(self, anchor_id: CrdtId) -> si.Group | None:
        """An existing group anchored to `anchor_id` with all four anchor fields (the last
        one in file order), or None. A new group placed by that anchor copies its fields."""
        found = None
        for g in _groups(self.blocks).values():
            if g.anchor_id is not None and g.anchor_id.value == anchor_id \
                    and None not in (g.anchor_type, g.anchor_threshold, g.anchor_origin_x):
                found = g
        return found

    def append_anchored_group(self, lines: list[si.Line], anchor_id: CrdtId) -> None:
        """Add `lines` inside ONE new group anchored to the text like an existing group.

        The page's handwriting near typed text lives in groups anchored to a text character
        (`anchor_id`): their points are relative to the anchor's text line, which only
        xochitl's layout knows. A new group copying an existing group's anchor_id,
        anchor_type, anchor_threshold and anchor_origin_x (`anchor_template`) is laid out
        like it, so `lines` must already be in that group's local coordinates.

        The group goes last in the page's rmdesk layer (`append_to_rmdesk_layer`'s layer;
        a new top `rmdesk` layer if there is none), with the blocks xochitl writes for one:
        a SceneTreeBlock, a TreeNodeBlock (empty label, visible), a SceneGroupItemBlock
        chained after the layer's right-most child, each after the last block of its kind
        (`_insert_layer_blocks`), and the lines as SceneLineItemBlocks at the end of the
        file, chained from END. Ids as the device allocates them: node G, group item G+1,
        anchor id/type/threshold stamps G+2..G+4, origin_x stamped G, lines from G+5.
        ValueError if no group on the page is anchored to `anchor_id`."""
        if not lines:
            raise ValueError("no lines to append")
        for line in lines:
            _check_line(line)
        template = self.anchor_template(anchor_id)
        if template is None:
            raise ValueError(f"no group on the page is anchored to {anchor_id}")
        layers = _rmdesk_layers(self.blocks)
        prev = END
        if layers:
            try:
                prev = _tail_anchor(self.blocks, layers[-1])
            except ValueError as e:  # cyclic CRDT links under the layer
                raise UnsupportedPage(f"cannot order the rmdesk layer: {e}") from e
        self._ensure_author()
        next_id = _IdAllocator(self.blocks, AUTHOR).next
        layer_id = layers[-1] if layers else self._add_layer(LAYER_LABEL, next_id)
        node_id = next_id()
        for _ in range(4):  # group item, anchor id/type/threshold stamps
            next_id()
        self._insert_layer_blocks(*_anchored_group_blocks(node_id, layer_id, prev, template))
        self._append_lines(node_id, END, lines, next_id)

    def rmdesk_layer_hidden(self) -> bool:
        """True if append_to_rmdesk_layer would reuse a layer that is hidden on the tablet
        (the new ink would then be invisible until the user shows the layer)."""
        layers = _rmdesk_layers(self.blocks)
        if not layers:
            return False
        visible = None
        for b in self.blocks:  # the last TreeNodeBlock for the node wins, as rmscene reads it
            if isinstance(b, TreeNodeBlock) and b.group.node_id == layers[-1]:
                visible = b.group.visible
        return visible is not None and visible.value is False

    def append_text(self, text: str) -> None:
        """Append `text` to the page's typed text, as a new paragraph at the end.

        `text` must be non-empty printable ASCII, lines separated by "\\n"
        (ValueError otherwise; rmscene's string encoding and the device's
        per-character ids are only known to agree for ASCII).

        If the page already has visible text, the item added holds
        "\\n" + text (a paragraph break, then the text); otherwise just `text`.
        Either way, call `verify(original, new, added_lines, text)` with the
        same `text` that was passed here: verify derives the "\\n" itself from
        the original page. Two calls on one Page append "\\n" + "a" + "\\n" + "b",
        which verify accepts as `added_text="a\\nb"`.

        The new item hangs off the LAST CHARACTER of the existing text in CRDT
        order (deleted characters and formatting codes included), with
        right_id = END, so it is unambiguously at the end whatever the CRDT
        tie-break. If the existing text has no single last character (two
        branches both ending the text), the page is refused (UnsupportedPage).
        No existing text item, style or PageInfo count is modified.

        A page without a RootTextBlock gets one, with rmscene's default text
        box geometry and a PLAIN paragraph style keyed (0,0), inserted before
        the first TreeNodeBlock (after the SceneTreeBlocks), as on the device.
        """
        _check_text(text)
        texts = [b for b in self.blocks if isinstance(b, RootTextBlock)]
        if len(texts) > 1:
            raise UnsupportedPage("page has more than one text block")
        if texts:
            tail = _text_tail(texts[0].value)
            if tail is None:
                raise UnsupportedPage("page text has no single end")
        self._ensure_author()
        ids = _IdAllocator(self.blocks, AUTHOR)
        if texts:
            value = "\n" + text if _has_text(texts[0].value) else text
            texts[0].value.items.add(
                CrdtSequenceItem(ids.take(len(value)), tail, END, 0, value)
            )
            return
        style_id = ids.next()
        block = RootTextBlock(
            block_id=END,
            value=si.Text(
                items=CrdtSequence(
                    [CrdtSequenceItem(ids.take(len(text)), END, END, 0, text)]
                ),
                styles={END: LwwValue(style_id, si.ParagraphStyle.PLAIN)},
                pos_x=TEXT_POS_X,
                pos_y=TEXT_POS_Y,
                width=TEXT_WIDTH,
            ),
        )
        kinds = [type(b) for b in self.blocks]
        if TreeNodeBlock in kinds:
            at = kinds.index(TreeNodeBlock)
        elif SceneTreeBlock in kinds:
            at = len(kinds) - kinds[::-1].index(SceneTreeBlock)
        else:
            at = len(kinds)
        self.blocks.insert(at, block)


class _IdAllocator:
    """Fresh CrdtIds for `author`, above every id already used in `blocks`
    (text character spans included, see `_max_id_part2`). Shared by
    append_layer and append_text; each call builds one from the current
    blocks, so ids added by an earlier call are never handed out again."""

    def __init__(self, blocks: list[Block], author: int):
        self.author = author
        self._last = _max_id_part2(blocks, author)

    def take(self, n: int) -> CrdtId:
        """Reserve `n` consecutive ids (e.g. one per text character); return the first."""
        if n < 1:
            raise ValueError("must reserve at least one id")
        first = self._last + 1
        self._last += n
        return CrdtId(self.author, first)

    def next(self) -> CrdtId:
        return self.take(1)


def _groups(blocks: list[Block]) -> dict[CrdtId, si.Group]:
    """Each live group node's attributes (the last TreeNodeBlock for it wins, as rmscene
    reads it). Live: the root, or the value of a group item that is not deleted."""
    live = {ROOT_ID} | {b.item.value for b in blocks if isinstance(b, SceneGroupItemBlock)
                        and b.item.value is not None and b.item.deleted_length == 0}
    out: dict[CrdtId, si.Group] = {}
    for b in blocks:
        if isinstance(b, TreeNodeBlock) and b.group.node_id in live:
            out[b.group.node_id] = b.group
    return out


def _anchored_group_blocks(node_id: CrdtId, parent: CrdtId, left: CrdtId,
                           template: si.Group) -> tuple[Block, Block, Block]:
    """The SceneTreeBlock, TreeNodeBlock and SceneGroupItemBlock of a new group `node_id`
    under `parent`, hanging off `left`, anchored like `template`. Mirrors the device's own
    groups (fw 3.29.1.0): label "" and visible True stamped (0,0), anchor_origin_x stamped
    with the node id, the group item node+1, anchor id/type/threshold stamped node+2..4."""
    def at(k: int) -> CrdtId:
        return CrdtId(node_id.part1, node_id.part2 + k)
    tree_block = SceneTreeBlock(tree_id=node_id, node_id=END, is_update=True, parent_id=parent)
    node_block = TreeNodeBlock(si.Group(
        node_id=node_id,
        label=LwwValue(END, ""),
        visible=LwwValue(END, True),
        anchor_id=LwwValue(at(2), template.anchor_id.value),
        anchor_type=LwwValue(at(3), template.anchor_type.value),
        anchor_threshold=LwwValue(at(4), template.anchor_threshold.value),
        anchor_origin_x=LwwValue(node_id, template.anchor_origin_x.value),
    ))
    item_block = SceneGroupItemBlock(parent_id=parent, item=CrdtSequenceItem(
        item_id=at(1), left_id=left, right_id=END, deleted_length=0, value=node_id))
    return tree_block, node_block, item_block


def _check_text(text: Any) -> None:
    if not isinstance(text, str) or not text:
        raise ValueError("expected non-empty text")
    if not _plain_ascii(text):
        raise ValueError("text must be printable ASCII and newlines")


def _plain_ascii(s: str) -> bool:
    return all(c == "\n" or " " <= c <= "~" for c in s)


def _text_chars(text: si.Text) -> list[CrdtSequenceItem]:
    """One item per character id (deleted characters and format codes included)."""
    return list(expand_text_items(text.items.sequence_items()))


def _text_tail(text: si.Text | None) -> CrdtId | None:
    """Id of the last character of `text` in every valid CRDT order: END if
    there are no characters, None if no single character is last (more than
    one character has nothing after it)."""
    if text is None:
        return END
    chars = _text_chars(text)
    if not chars:
        return END
    ids = {c.item_id for c in chars}
    lefts = {c.left_id for c in chars}
    sinks = [c.item_id for c in chars
             if c.item_id not in lefts and (c.right_id == END or c.right_id not in ids)]
    return sinks[0] if len(sinks) == 1 else None


def _has_text(text: si.Text | None) -> bool:
    """True if `text` holds at least one visible (non-deleted) character."""
    return text is not None and any(
        isinstance(i.value, str) and i.value for i in text.items.sequence_items()
    )


def _plain_text(text: si.Text | None) -> str:
    """The visible characters of `text` in CRDT order (format codes dropped)."""
    if text is None:
        return ""
    seq = CrdtSequence(_text_chars(text))
    return "".join(v for v in seq.values() if isinstance(v, str))


def _id_span(item: CrdtSequenceItem) -> int:
    """How many consecutive ids a text item occupies (at least one)."""
    n = (len(item.value) if isinstance(item.value, str) else 0) + item.deleted_length
    return max(n, 1)


def _root_order(blocks: list[Block]) -> list[CrdtId]:
    """Item ids of the root's children in CRDT order, deleted items included.

    rmscene's tree drops deleted group items and tombstone blocks, so its order
    can differ from the device's; this works on the raw blocks instead.
    """
    items = [
        b.item
        for b in blocks
        if isinstance(b, (SceneGroupItemBlock, SceneTombstoneItemBlock))
        and b.parent_id == ROOT_ID
    ]
    return list(toposort_items(items))


def _run_len(item: CrdtSequenceItem) -> int:
    """How many consecutive ids a scene item occupies: a deleted run of n
    items is stored as one item with deleted_length n (at least one)."""
    return max(item.deleted_length, 1)


def _child_items(blocks: list[Block], parent: CrdtId) -> list[CrdtSequenceItem]:
    """Every item under `parent` in the raw blocks: lines, glyphs, group items
    and tombstones, deleted ones included."""
    return [b.item for b in blocks
            if isinstance(b, SceneItemBlock) and b.parent_id == parent]


def _child_order(blocks: list[Block], parent: CrdtId) -> list[CrdtId]:
    """Item ids of `parent`'s children in CRDT order, deleted items and
    tombstones included (from the raw blocks, like `_root_order`).

    A link to an id inside a deleted run (xochitl hangs the next item off the
    run's last id, e.g. (1,296) -> (1,284) for the run (1,20)+265) is resolved
    to the run's item; rmscene's toposort would treat it as unknown and put
    the item first. Raises ValueError on cyclic links.
    """
    items = _child_items(blocks, parent)
    ids = {it.item_id for it in items}

    def resolve(link: CrdtId) -> CrdtId:
        if link == END or link in ids:
            return link
        for it in items:
            i = it.item_id
            if i.part1 == link.part1 and i.part2 <= link.part2 < i.part2 + _run_len(it):
                return i
        return link

    resolved = [dataclasses.replace(it, left_id=resolve(it.left_id),
                                    right_id=resolve(it.right_id)) for it in items]
    return list(toposort_items(resolved))


def _tail_anchor(blocks: list[Block], parent: CrdtId) -> CrdtId:
    """The id a new item must hang off (left_id) to follow every child of
    `parent`: the last id of the right-most child in CRDT order, END if none."""
    order = _child_order(blocks, parent)
    if not order:
        return END
    last = next(it for it in _child_items(blocks, parent) if it.item_id == order[-1])
    return CrdtId(last.item_id.part1, last.item_id.part2 + _run_len(last) - 1)


def _rmdesk_layers(blocks: list[Block]) -> list[CrdtId]:
    """Node ids of the root-level layers labelled exactly `rmdesk`, in root
    CRDT order: live (not deleted) group items under the root whose node's
    SceneTreeBlock also has the root as parent and whose label (the last
    TreeNodeBlock for that node, as rmscene reads it) is `rmdesk`."""
    labels: dict[CrdtId, Any] = {}
    tree_parent: dict[CrdtId, CrdtId] = {}
    for b in blocks:
        if isinstance(b, TreeNodeBlock):
            labels[b.group.node_id] = b.group.label.value if b.group.label else None
        elif isinstance(b, SceneTreeBlock):
            tree_parent[b.tree_id] = b.parent_id
    node_of = {
        b.item.item_id: b.item.value
        for b in blocks
        if isinstance(b, SceneGroupItemBlock) and b.parent_id == ROOT_ID
        and b.item.value is not None and b.item.deleted_length == 0
    }
    out = []
    for iid in _root_order(blocks):
        node = node_of.get(iid)
        if node is not None and labels.get(node) == LAYER_LABEL \
                and tree_parent.get(node) == ROOT_ID:
            out.append(node)
    return out


def _check_line(line: si.Line) -> None:
    if not isinstance(line, si.Line) or not line.points:
        raise ValueError("expected a si.Line with points")
    for p in line.points:
        for name, hi in (("speed", 0xFFFF), ("width", 0xFFFF),
                         ("direction", 0xFF), ("pressure", 0xFF)):
            v = getattr(p, name)
            if not isinstance(v, int) or not 0 <= v <= hi:
                raise ValueError(f"point {name}={v!r} out of range")


def _max_id_part2(obj: Any, author: int) -> int:
    """Largest CrdtId.part2 used by `author` anywhere inside `obj` (0 if none).

    A sequence item also reserves the ids of the characters it holds (text
    items: one id per character) and of deleted characters (deleted_length).
    """
    best = 0

    def visit(o: Any) -> None:
        nonlocal best
        if isinstance(o, CrdtId):
            if o.part1 == author:
                best = max(best, o.part2)
        elif isinstance(o, CrdtSequenceItem):
            if o.item_id.part1 == author:
                span = (len(o.value) if isinstance(o.value, str) else 0) + o.deleted_length
                best = max(best, o.item_id.part2 + max(span, 1) - 1)
            for f in dataclasses.fields(o):
                visit(getattr(o, f.name))
        elif isinstance(o, CrdtSequence):
            for it in o.sequence_items():
                visit(it)
        elif isinstance(o, dict):
            for k, v in o.items():
                visit(k)
                visit(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                visit(v)
        elif dataclasses.is_dataclass(o) and not isinstance(o, type):
            for f in dataclasses.fields(o):
                visit(getattr(o, f.name))
        # str, bytes, numbers, enums, UUIDs: no ids inside

    visit(obj)
    return best


def load(data: bytes) -> Page:
    if data[: len(HEADER_V6)] != HEADER_V6:
        raise UnsupportedPage("not a v6 .rm page")
    raw_blocks = _split_raw(data)
    try:
        blocks = list(rmscene.read_blocks(io.BytesIO(data)))
    except Exception as e:  # malformed block structure
        raise UnsupportedPage(f"cannot read page: {e}") from e
    bad = [b for b in blocks if isinstance(b, UnreadableBlock)]
    if bad:
        raise UnsupportedPage(f"unreadable block: {bad[0].error}")
    if len(blocks) != len(raw_blocks):
        raise UnsupportedPage("block count mismatch")
    try:
        rmscene.read_tree(io.BytesIO(data))
    except Exception as e:
        raise UnsupportedPage(f"cannot build scene tree: {e}") from e
    return Page(blocks, data, raw_blocks)


# -- verification -------------------------------------------------------------

def _group_attrs(g: si.Group) -> tuple:
    return (g.label, g.visible, g.anchor_id, g.anchor_type,
            g.anchor_threshold, g.anchor_origin_x)


def _index(tree) -> tuple[dict, dict, dict]:
    """Walk a SceneTree.

    Returns (items, groups, orders):
      items:  (parent node id, item id) -> CrdtSequenceItem (a group's value is
              replaced by its node id)
      groups: node id -> group attributes (label, visible, anchors)
      orders: node id -> list of child item ids in CRDT order
    """
    items: dict = {}
    groups: dict = {}
    orders: dict = {}

    def walk(group: si.Group) -> None:
        groups[group.node_id] = _group_attrs(group)
        orders[group.node_id] = group.children.keys()
        for it in group.children.sequence_items():
            if isinstance(it.value, si.Group):
                items[(group.node_id, it.item_id)] = dataclasses.replace(
                    it, value=it.value.node_id
                )
                walk(it.value)
            else:
                items[(group.node_id, it.item_id)] = it

    walk(tree.root)
    return items, groups, orders


def _text_pieces(text: si.Text | None) -> dict:
    return {} if text is None else {i.item_id: i for i in text.items.sequence_items()}


def _verify_text(old: si.Text | None, new: si.Text | None, added: str | None) -> None:
    """`added` None or "" means no text was added: the text must be unchanged.

    Otherwise `added` is the text given to Page.append_text: the new page
    text must be exactly the old text followed by `expected` = "\n" + added
    (if the old page had visible text) or `added` (if not).
    """
    if not added:
        if old != new:
            raise VerifyError("page text changed")
        return
    if new is None:
        raise VerifyError("added text missing")
    expected = "\n" + added if _has_text(old) else added
    old_items, new_items = _text_pieces(old), _text_pieces(new)
    for iid, it in old_items.items():
        if new_items.get(iid) != it:
            raise VerifyError(f"original text item {iid} changed")
    for cid, style in (old.styles if old else {}).items():
        if new.styles.get(cid) != style:
            raise VerifyError(f"original text style {cid} changed")
    # Every new text item is a plain string (no formatting codes, no deletions,
    # printable ASCII and newlines only), and every new style is keyed by a
    # character inside those new items.
    added_items = {iid: it for iid, it in new_items.items() if iid not in old_items}
    new_chars: set[CrdtId] = set()
    for iid, it in added_items.items():
        if (not isinstance(it.value, str) or not it.value or it.deleted_length != 0
                or not _plain_ascii(it.value)):
            raise VerifyError(f"new text item {iid} is not plain added text")
        new_chars.update(CrdtId(iid.part1, iid.part2 + k) for k in range(len(it.value)))
    # No new item may reuse a character id of an original item (its text,
    # deleted run or format code) or of another new item.
    taken: list[tuple[int, int, int, CrdtId]] = [
        (it.item_id.part1, it.item_id.part2, it.item_id.part2 + _id_span(it) - 1, iid)
        for iid, it in old_items.items()
    ]
    for iid, it in added_items.items():
        lo, hi = iid.part2, iid.part2 + _id_span(it) - 1
        for author, olo, ohi, other in taken:
            if author == iid.part1 and lo <= ohi and olo <= hi:
                raise VerifyError(f"new text item {iid} overlaps the ids of item {other}")
        taken.append((iid.part1, lo, hi, iid))
    old_styles = old.styles if old else {}
    for cid in new.styles:
        if cid in old_styles:
            continue
        # A page that had no text gets its default paragraph style keyed (0,0).
        if cid in new_chars or (old is None and cid == END):
            continue
        raise VerifyError(f"new text style on {cid}, which is not an added character")
    if old is not None and (old.pos_x, old.pos_y, old.width) != (new.pos_x, new.pos_y, new.width):
        raise VerifyError("text box geometry changed")
    added_str = "".join(
        new_items[iid].value
        for iid in new.items
        if iid not in old_items and isinstance(new_items[iid].value, str)
    )
    if added_str != expected:
        raise VerifyError(f"added text is {added_str!r}, expected {expected!r}")
    # The new items form one chain hanging off the single last character of
    # the original text (END for no characters), each linked to the previous
    # one's last character, with no right link: then they come after every
    # original character in every valid CRDT order, whatever the tie-break.
    prev = _text_tail(old)
    if prev is None:
        raise VerifyError("original text has no single end; cannot be at the end of the page text")
    remaining = dict(added_items)
    chain: list[str] = []
    while remaining:
        nxt = [iid for iid, it in remaining.items() if it.left_id == prev]
        if len(nxt) != 1:
            raise VerifyError("added text is not one chain at the end of the page text")
        it = remaining.pop(nxt[0])
        if it.right_id != END:
            raise VerifyError("added text is not one chain at the end of the page text")
        chain.append(it.value)
        prev = CrdtId(it.item_id.part1, it.item_id.part2 + len(it.value) - 1)
    if "".join(chain) != expected:
        raise VerifyError("added text chain does not spell the added text")
    if _plain_text(new) != _plain_text(old) + expected:
        raise VerifyError("new page text is not the old text followed by the added text")


def _read(data: bytes, what: str):
    """Return (Page, SceneTree, raw blocks) or raise VerifyError."""
    try:
        return load(data), rmscene.read_tree(io.BytesIO(data)), _split_raw(data)
    except Exception as e:
        raise VerifyError(f"{what} page unreadable: {e}") from e


def _verify_raw_blocks(old_page: Page, new_page: Page, old_raw: list[bytes],
                       new_raw: list[bytes], added_text: str | None) -> None:
    """Every original block must appear byte-identical in the new file, in the
    same order. This sees what the scene tree cannot: tombstones, deleted
    items, unparsed extra data, page info.

    The single exemption is derived here, never supplied by the caller: when
    non-empty text is being added, the original RootTextBlock may be
    re-encoded (its unparsed extra_data must still be equal). It must
    then still exist exactly once in the new file; its content is checked by
    _verify_text.
    """
    exempt: set[int] = set()
    if added_text:  # None or "": no exemption, the text block is byte-checked too
        texts = [i for i, b in enumerate(old_page.blocks) if isinstance(b, RootTextBlock)]
        if len(texts) > 1:
            raise VerifyError("original page has more than one text block")
        exempt = set(texts)
        new_texts = [b for b in new_page.blocks if isinstance(b, RootTextBlock)]
        if len(new_texts) != 1:
            raise VerifyError("new page must have exactly one text block")
        if texts and old_page.blocks[texts[0]].extra_data != new_texts[0].extra_data:
            raise VerifyError("unparsed data of the text block changed")
    it = iter(new_raw)
    for i, raw in enumerate(old_raw):
        if i in exempt:
            continue
        if not any(raw == n for n in it):
            raise VerifyError(f"original block {i} missing, changed or moved")


def _verify_lines_in_existing_layer(old_page: Page, new_page: Page, added: dict,
                                    added_lines: int) -> None:
    """Case (b) of `verify`: no layer was created; the new items must be
    exactly `added_lines` live lines under the original page's last
    `rmdesk` layer, after all of its original children."""
    for (_, iid), v in added.items():
        if not isinstance(v.value, si.Line) or v.deleted_length != 0:
            raise VerifyError(f"unexpected new item {iid}: not a line")
    if len(added) != added_lines:
        raise VerifyError(f"{len(added)} new lines, expected {added_lines}")
    parents = {p for (p, _) in added}
    if len(parents) != 1:
        raise VerifyError("new lines are not all under one layer")
    (layer_id,) = parents
    old_layers = _rmdesk_layers(old_page.blocks)
    if not old_layers or layer_id != old_layers[-1]:
        raise VerifyError("new lines are not under the page's rmdesk layer")
    if _rmdesk_layers(new_page.blocks)[-1:] != [layer_id]:
        raise VerifyError("the rmdesk layer is no longer the page's last rmdesk layer")
    # Raw level (the original blocks are already known to be an ordered
    # subsequence of the new ones): the only new blocks are the lines.
    old_kinds = collections.Counter(type(b) for b in old_page.blocks)
    new_kinds = collections.Counter(type(b) for b in new_page.blocks)
    if new_kinds - old_kinds != collections.Counter({SceneLineItemBlock: added_lines}) \
            or len(new_page.blocks) != len(old_page.blocks) + added_lines:
        raise VerifyError("blocks other than the new lines were added")
    # The new lines form one chain hanging off the last id of the layer's
    # right-most original child (END if it had none), each linked to the
    # previous one, with no right link; so they are last in the layer.
    try:
        prev = _tail_anchor(old_page.blocks, layer_id)
        order = _child_order(new_page.blocks, layer_id)
    except ValueError as e:
        raise VerifyError(f"cannot order the rmdesk layer: {e}") from e
    remaining = {iid: v for (_, iid), v in added.items()}
    chain: list[CrdtId] = []
    while remaining:
        nxt = [iid for iid, v in remaining.items() if v.left_id == prev]
        if len(nxt) != 1 or remaining[nxt[0]].right_id != END:
            raise VerifyError("new lines are not one chain at the end of the rmdesk layer")
        remaining.pop(nxt[0])
        chain.append(nxt[0])
        prev = nxt[0]
    if order[len(order) - len(chain):] != chain:
        raise VerifyError("new lines are not last in the rmdesk layer")


def _verify_anchored_group(old_page: Page, new_page: Page, added: dict, old_groups: dict,
                           new_groups: dict, extra_groups: set, added_lines: int) -> None:
    """Case (c) of `verify`: exactly one new text-anchored group holding all the new lines.

    The group (`Page.append_anchored_group`) must be:
      - the only new anchored group, and a child of the rmdesk layer: the original page's
        last `rmdesk` layer, or one new `rmdesk` layer (visible, unanchored, a child of the
        root, stacked last) that holds nothing but this group;
      - labelled "" and visible, anchored to an anchor_id an original group already uses,
        with anchor_type, anchor_threshold and anchor_origin_x equal to such a group's;
      - hung last in its layer: after the layer's right-most original child (END in a new
        layer), right_id END;
      - holding exactly `added_lines` live lines (at least one), one chain from END, and
        nothing else; no new item anywhere else.
    At the raw level the only new blocks are one SceneTreeBlock, TreeNodeBlock and
    SceneGroupItemBlock per new group (tree parent = the item's parent) and the lines'
    SceneLineItemBlocks (plus the AuthorIdsBlock a page without one gets)."""
    anchored = [g for g in extra_groups if new_groups[g][2] is not None]
    if len(anchored) != 1:
        raise VerifyError("expected exactly one new anchored group")
    (gid,) = anchored
    group_items = {v.value: (p, iid, v) for (p, iid), v in added.items()
                   if isinstance(v.value, CrdtId)}
    n_group_items = sum(isinstance(v.value, CrdtId) for v in added.values())
    if set(group_items) != extra_groups or n_group_items != len(extra_groups):
        raise VerifyError("unexpected new group items")
    parent, item_id, item = group_items[gid]
    old_layers = _rmdesk_layers(old_page.blocks)
    others = extra_groups - {gid}
    if others:
        if others != {parent}:
            raise VerifyError("new anchored group is not in the rmdesk layer")
        lp, l_item_id, _ = group_items[parent]
        label, visible, anchor = new_groups[parent][:3]
        if lp != ROOT_ID or label.value != LAYER_LABEL or visible.value is not True \
                or anchor is not None:
            raise VerifyError("new anchored group is not in a new visible rmdesk layer")
        root_order = _root_order(new_page.blocks)
        if not root_order or root_order[-1] != l_item_id:
            raise VerifyError("new layer is not the top layer")
        tail = END
    else:
        if not old_layers or parent != old_layers[-1]:
            raise VerifyError("new anchored group is not in the page's rmdesk layer")
        if _rmdesk_layers(new_page.blocks)[-1:] != [parent]:
            raise VerifyError("the rmdesk layer is no longer the page's last rmdesk layer")
        try:
            tail = _tail_anchor(old_page.blocks, parent)
        except ValueError as e:
            raise VerifyError(f"cannot order the rmdesk layer: {e}") from e
    label, visible, anchor_id, anchor_type, threshold, origin_x = new_groups[gid]
    if label is None or label.value != "" or visible is None or visible.value is not True:
        raise VerifyError("new anchored group is labelled or hidden")
    fields = (anchor_id.value, *(None if v is None else v.value
                                 for v in (anchor_type, threshold, origin_x)))
    known = {(a[2].value, a[3].value, a[4].value, a[5].value) for a in old_groups.values()
             if a[2] is not None and None not in a[3:6]}
    if fields not in known:
        raise VerifyError("new group's anchor is not copied from a group on the page")
    if item.left_id != tail or item.right_id != END or item.deleted_length != 0:
        raise VerifyError("new anchored group is not last in the rmdesk layer")
    try:
        if _child_order(new_page.blocks, parent)[-1:] != [item_id]:
            raise VerifyError("new anchored group is not last in the rmdesk layer")
        order = _child_order(new_page.blocks, gid)
    except ValueError as e:
        raise VerifyError(f"cannot order the rmdesk layer: {e}") from e
    lines = {iid: v for (p, iid), v in added.items() if not isinstance(v.value, CrdtId)}
    for (p, iid), v in added.items():
        if isinstance(v.value, CrdtId):
            continue
        if p != gid or not isinstance(v.value, si.Line) or v.deleted_length != 0:
            raise VerifyError(f"unexpected new item {iid} outside the new anchored group")
    if not lines or len(lines) != added_lines:
        raise VerifyError(f"{len(lines)} new lines, expected {added_lines}")
    prev, chain, remaining = END, [], dict(lines)
    while remaining:
        nxt = [iid for iid, v in remaining.items() if v.left_id == prev]
        if len(nxt) != 1 or remaining[nxt[0]].right_id != END:
            raise VerifyError("new lines are not one chain in the new anchored group")
        remaining.pop(nxt[0])
        chain.append(nxt[0])
        prev = nxt[0]
    if order != chain:
        raise VerifyError("new anchored group holds other items")
    # raw level (the original blocks are already an ordered subsequence of the new ones)
    g = len(extra_groups)
    want = collections.Counter({SceneTreeBlock: g, TreeNodeBlock: g, SceneGroupItemBlock: g,
                                SceneLineItemBlock: added_lines})
    old_kinds = collections.Counter(type(b) for b in old_page.blocks)
    diff = collections.Counter(type(b) for b in new_page.blocks) - old_kinds
    if not old_kinds[AuthorIdsBlock] and diff[AuthorIdsBlock] == 1:
        diff[AuthorIdsBlock] = 0
    if +diff != want or len(new_page.blocks) - len(old_page.blocks) != sum(diff.values()):
        raise VerifyError("blocks other than the new group and its lines were added")
    old_trees = {b.tree_id for b in old_page.blocks if isinstance(b, SceneTreeBlock)}
    new_trees = {b.tree_id: b for b in new_page.blocks
                 if isinstance(b, SceneTreeBlock) and b.tree_id not in old_trees}
    if set(new_trees) != extra_groups:
        raise VerifyError("new tree blocks do not match the new groups")
    for node, b in new_trees.items():
        if b.parent_id != group_items[node][0] or b.node_id != END or b.is_update is not True:
            raise VerifyError(f"tree block of new group {node} has the wrong parent")
    old_nodes = collections.Counter(b.group.node_id for b in old_page.blocks
                                    if isinstance(b, TreeNodeBlock))
    new_nodes = collections.Counter(b.group.node_id for b in new_page.blocks
                                    if isinstance(b, TreeNodeBlock)) - old_nodes
    if new_nodes != collections.Counter(extra_groups):
        raise VerifyError("new tree node blocks do not match the new groups")


def verify(original: bytes, new: bytes, added_lines: int, added_text: str | None) -> None:
    """Raise VerifyError unless `new` is `original` plus exactly the additions.

    Every original item must still be present with the same item id, parent,
    CRDT links and value; every original group must keep its label,
    visibility and anchors, and every group's original children keep their
    order. The only new things allowed are exactly `added_lines` new lines,
    all under one parent, which is either
      (a) one new `rmdesk` layer (visible, a child of the root, stacked last)
          holding exactly those lines, or
      (b) the original page's existing `rmdesk` layer (the last one in root
          order, `_rmdesk_layers`), with no new block other than the lines'
          SceneLineItemBlocks and the lines chained after every original
          child of the layer (`_verify_lines_in_existing_layer`),
      (c) one new text-anchored group, a child of the rmdesk layer (the existing one or a
          new one created for it), anchored exactly like an original group and holding
          exactly those lines (`_verify_anchored_group`),
    and (Task 10) `added_text` appended to the page text.

    Independently of the tree, every raw block of `original` must appear
    byte-identical and in order in `new`. The only exemption is the original
    RootTextBlock, and only when `added_text` is non-empty ("" counts as None).
    """
    old_page, old_tree, old_raw = _read(original, "original")
    new_page, new_tree, new_raw = _read(new, "new")
    _verify_raw_blocks(old_page, new_page, old_raw, new_raw, added_text)
    try:
        old_items, old_groups, old_orders = _index(old_tree)
        new_items, new_groups, new_orders = _index(new_tree)
    except VerifyError:
        raise
    except Exception as e:
        raise VerifyError(f"cannot walk scene tree: {e}") from e

    for key, item in old_items.items():
        if new_items.get(key) != item:
            raise VerifyError(f"original item {key[1]} under {key[0]} missing or changed")
    for node_id, attrs in old_groups.items():
        if new_groups.get(node_id) != attrs:
            raise VerifyError(f"original group {node_id} missing or changed")
    # Defense in depth: with identical items, originals can only be reordered by
    # new items that rmscene's toposort resolves through links to unknown ids.
    for node_id, order in old_orders.items():
        kept = [iid for iid in new_orders[node_id] if (node_id, iid) in old_items]
        if kept != order:
            raise VerifyError(f"order of items in group {node_id} changed")
    if old_tree.scene_info != new_tree.scene_info:
        raise VerifyError("scene info changed")

    added = {k: v for k, v in new_items.items() if k not in old_items}
    new_layers = [(k, v) for k, v in added.items() if isinstance(v.value, CrdtId)
                  and v.value in new_groups and v.value not in old_groups]
    extra_groups = set(new_groups) - set(old_groups)
    if added_lines == 0:
        if added or extra_groups:
            raise VerifyError("unexpected new items")
    elif any(new_groups[g][2] is not None for g in extra_groups):
        _verify_anchored_group(old_page, new_page, added, old_groups, new_groups,
                               extra_groups, added_lines)
    elif not extra_groups and not new_layers:
        _verify_lines_in_existing_layer(old_page, new_page, added, added_lines)
    else:
        if len(new_layers) != 1 or extra_groups != {new_layers[0][1].value}:
            raise VerifyError("expected exactly one new layer")
        (parent, item_id), layer_item = new_layers[0]
        layer_id = layer_item.value
        if parent != ROOT_ID:
            raise VerifyError("new layer is not a child of the root")
        label, visible = new_groups[layer_id][:2]
        if label.value != LAYER_LABEL or visible.value is not True:
            raise VerifyError("new layer is not a visible 'rmdesk' layer")
        root_order = _root_order(new_page.blocks)
        if not root_order or root_order[-1] != item_id:
            raise VerifyError("new layer is not the top layer")
        lines = [(k, v) for k, v in added.items() if k != (parent, item_id)]
        for (p, _), v in lines:
            if p != layer_id or not isinstance(v.value, si.Line) or v.deleted_length != 0:
                raise VerifyError("unexpected new item outside the rmdesk layer")
        if len(lines) != added_lines:
            raise VerifyError(f"{len(lines)} new lines, expected {added_lines}")

    try:
        _verify_text(old_tree.root_text, new_tree.root_text, added_text)
    except VerifyError:
        raise
    except Exception as e:  # e.g. a cyclic CRDT text sequence
        raise VerifyError(f"cannot check page text: {e}") from e
