"""Document tree built from the tablet's xochitl metadata files. No Qt."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Literal

from rmdesk.device import Device, DeviceError

log = logging.getLogger(__name__)

XOCHITL = "/home/root/.local/share/remarkable/xochitl"

Kind = Literal["folder", "notebook", "pdf", "epub"]
TRASH = "trash"


@dataclass
class Item:
    id: str
    name: str
    kind: Kind
    parent: str
    modified: int
    page_ids: list[str] = field(default_factory=list)


class Library:
    def __init__(self, items: dict[str, Item]):
        self.items = items

    def children(self, folder_id: str = "") -> list[Item]:
        kids = [i for i in self.items.values() if i.parent == folder_id]
        kids.sort(key=lambda i: (i.kind != "folder", i.name.lower()))
        return kids

    def in_trash(self, item_id: str) -> bool:
        seen: set[str] = set()
        cur = self.items.get(item_id)
        while cur is not None and cur.id not in seen:
            if cur.parent == TRASH:
                return True
            seen.add(cur.id)
            cur = self.items.get(cur.parent)
        return False

    def search(self, text: str) -> list[Item]:
        needle = text.lower()
        hits = [i for i in self.items.values()
                if needle in i.name.lower() and not self.in_trash(i.id)]
        hits.sort(key=lambda i: (i.kind != "folder", i.name.lower()))
        return hits

    def path(self, item_id: str) -> list[Item]:
        """Ancestor folders from the top down, excluding the item itself."""
        chain: list[Item] = []
        seen = {item_id}
        item = self.items.get(item_id)
        while item is not None:
            parent = self.items.get(item.parent)
            if parent is None or parent.id in seen:
                break
            chain.append(parent)
            seen.add(parent.id)
            item = parent
        chain.reverse()
        return chain


def _page_ids(content: dict) -> list[str]:
    cpages = content.get("cPages")
    if isinstance(cpages, dict) and isinstance(cpages.get("pages"), list):
        return [p["id"] for p in cpages["pages"]
                if isinstance(p, dict) and "id" in p and not p.get("deleted")]
    pages = content.get("pages")
    if isinstance(pages, list):
        return [p for p in pages if isinstance(p, str)]
    return []


def _load_item(device: Device, root: str, uid: str) -> Item | None:
    meta = json.loads(device.read_bytes(f"{root}/{uid}.metadata"))
    if not isinstance(meta, dict):
        raise ValueError("metadata is not an object")
    if meta.get("deleted"):
        return None
    name, typ = meta["visibleName"], meta["type"]
    parent = meta.get("parent") or ""
    modified = int(meta.get("lastModified") or 0)
    if typ == "CollectionType":
        return Item(uid, name, "folder", parent, modified, [])
    if typ != "DocumentType":
        raise ValueError(f"unknown type {typ!r}")
    content = json.loads(device.read_bytes(f"{root}/{uid}.content"))
    if not isinstance(content, dict):
        raise ValueError("content is not an object")
    kind = content.get("fileType") or "notebook"
    if kind not in ("notebook", "pdf", "epub"):
        raise ValueError(f"unsupported fileType {kind!r}")
    return Item(uid, name, kind, parent, modified, _page_ids(content))


def load(device: Device, root: str = XOCHITL) -> Library:
    items: dict[str, Item] = {}
    suffix = ".metadata"
    for entry in device.listdir(root):
        if not entry.endswith(suffix):
            continue
        uid = entry[: -len(suffix)]
        try:
            item = _load_item(device, root, uid)
        except (DeviceError, ValueError, KeyError, TypeError) as exc:
            log.warning("skipping %s: %s", uid, exc)
            continue
        if item is not None:
            items[uid] = item
    return Library(items)
