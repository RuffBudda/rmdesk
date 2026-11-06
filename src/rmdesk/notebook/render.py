"""Render notebook pages to SVG. No Qt."""
from __future__ import annotations

import html
import io
import re
import xml.etree.ElementTree as ET

import rmscene
from rmc.exporters.svg import tree_to_svg

from rmdesk.device import Device
from rmdesk.library.model import XOCHITL, Item

PAGE_W, PAGE_H = 1404, 1872
BLANK_SVG = (
    f'<svg xmlns="http://www.w3.org/2000/svg" width="{PAGE_W}" height="{PAGE_H}" '
    f'viewBox="0 0 {PAGE_W} {PAGE_H}"><rect width="100%" height="100%" fill="white"/></svg>'
).encode("utf-8")


# rmc writes each line of typed text as one line of SVG, its text not escaped
# (rmc/exporters/svg.py: `<text x=".." y=".." class="..">{str(p).strip()}</text>`); a
# paragraph never holds a newline, so the text is everything up to the line's last </text>
_TEXT_LINE = re.compile(r'^(\s*<text x="[^"<>]*" y="[^"<>]*" class="[A-Za-z0-9_]*">)(.*)(</text>)$',
                        re.MULTILINE)
# what an exported page may contain: nothing that loads a file, a URL or runs script
_FORBIDDEN_TAGS = {"image", "script", "foreignObject", "use", "feImage", "iframe", "a"}


def _escape_text(svg: str) -> str:
    return _TEXT_LINE.sub(lambda m: m.group(1) + html.escape(m.group(2), quote=False)
                          + m.group(3), svg)


def _check_safe(svg: str) -> None:
    """Raise ValueError if the SVG is not well-formed or could load anything (an <image>,
    any href, a script or event handler). Typed text from a crafted page must stay text."""
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as e:
        raise ValueError(f"the page's SVG is not well-formed: {e}") from e
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""
        if tag in _FORBIDDEN_TAGS:
            raise ValueError(f"the page's SVG has a <{tag}> element")
        for name in el.attrib:
            local = name.rsplit("}", 1)[-1].lower()
            if local == "href" or local.startswith("on"):
                raise ValueError(f"the page's SVG has a {local} attribute")


def page_svg(data: bytes) -> bytes:
    """Convert the bytes of one .rm page file to SVG (UTF-8). Typed text is escaped, and an
    SVG that could load a resource is refused (ValueError)."""
    tree = rmscene.read_tree(io.BytesIO(data))
    out = io.StringIO()
    tree_to_svg(tree, out)
    svg = _escape_text(out.getvalue())
    _check_safe(svg)
    return svg.encode("utf-8")


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
