"""Render notebook pages to SVG. No Qt."""
from __future__ import annotations

import io

import rmscene
from rmc.exporters.svg import tree_to_svg

from rmdesk.device import Device
from rmdesk.library.model import XOCHITL, Item

PAGE_W, PAGE_H = 1404, 1872
BLANK_SVG = (
    f'<svg xmlns="http://www.w3.org/2000/svg" width="{PAGE_W}" height="{PAGE_H}" '
    f'viewBox="0 0 {PAGE_W} {PAGE_H}"><rect width="100%" height="100%" fill="white"/></svg>'
).encode("utf-8")


def page_svg(data: bytes) -> bytes:
    """Convert the bytes of one .rm page file to SVG (UTF-8)."""
    tree = rmscene.read_tree(io.BytesIO(data))
    out = io.StringIO()
    tree_to_svg(tree, out)
    return out.getvalue().encode("utf-8")


def notebook_svgs(device: Device, item: Item, root: str = XOCHITL) -> list[bytes]:
    """One SVG per page of the notebook; a page without a .rm file is blank."""
    svgs: list[bytes] = []
    for page_id in item.page_ids:
        path = f"{root}/{item.id}/{page_id}.rm"
        if device.exists(path):
            svgs.append(page_svg(device.read_bytes(path)))
        else:
            svgs.append(BLANK_SVG)
    return svgs
