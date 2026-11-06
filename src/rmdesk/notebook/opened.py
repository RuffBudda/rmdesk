"""Identify the page open on the tablet from xochitl's files. No Qt.

xochitl records the open notebook (`lastOpened`) and page (`lastOpenedPage`)
only when it stops or the notebook closes, so call this after stopping it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from rmdesk import ids as _ids
from rmdesk.device import Device, DeviceError
from rmdesk.library.model import TRASH, XOCHITL

ZOOM_MSG = "Zoom the page back to fit (pinch out) and send again."
BLANK_PAGE_MSG = ("This page is empty on the tablet. Draw anything on it first (a dot is enough), "
                  "then press Reload page.")
PDF_MSG = "The open document isn't a notebook (PDFs and ebooks can't be drawn on yet)."
NO_DOC_MSG = "No notebook was found on the tablet."
BAD_FILES_MSG = "Couldn't read the notebook's files on the tablet."
TIE_MSG = ("Two notebooks look equally recent, so rmdesk can't tell which one is open. "
           "Open the notebook on the tablet and try again.")
NO_PAGE_MSG = "rmdesk can't tell which page is open."
BAD_PAGE_MSG = "The page that was open can't be found in its notebook."


class NotEditable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class OpenedPage:
    notebook_id: str
    name: str
    page_id: str
    page_index: int
    page_count: int
    orientation: Literal["portrait", "landscape"]
    data: bytes
    synced: bool = False  # .metadata "synced": true (the notebook syncs with the cloud)
    # the page's cPages "verticalScroll" as last saved (absent = 0; None when unreadable).
    # Informational only: xochitl doesn't save it when stopped, so it can be stale at send
    # time; Compose finds the scroll from the live picture (compose.scroll_fit) instead.
    vertical_scroll: float | None = 0.0


def _damaged(name: str) -> NotEditable:
    return NotEditable(f"The tablet has a damaged file ({name}), so rmdesk can't tell "
                       "which notebook is open.")


def _num(v) -> int | None:
    """A non-negative int, or a string of digits; None for anything else."""
    if isinstance(v, int) and not isinstance(v, bool):
        return v if v >= 0 else None
    if isinstance(v, str) and v.isascii() and v.isdigit():
        return int(v)
    return None


def _scroll(entry: dict) -> float | None:
    """The page's verticalScroll ({"timestamp": ..., "value": N}); 0 when absent."""
    vs = entry.get("verticalScroll")
    if vs is None:
        return 0.0
    v = vs.get("value") if isinstance(vs, dict) else None
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        return None
    try:
        f = float(v)
    except ValueError:
        return None
    return f if 0.0 <= f < 1e6 else None


def _zoomed(content: dict) -> bool:
    # Observed on fw 3.29.1.0 for a default-zoom portrait notebook: zoomMode
    # "bestFit", customZoomScale 1, customZoomCenterX 0, customZoomCenterY 936,
    # customZoomOrientation "portrait", customZoomPageWidth 1404,
    # customZoomPageHeight 1872. Default zoom = zoomMode absent or "bestFit"
    # and customZoomScale absent or 1. Other zoom modes are unknown, so they
    # count as zoomed (refused).
    if content.get("zoomMode", "bestFit") != "bestFit":
        return True
    scale = content.get("customZoomScale")
    if scale is None:
        return False
    try:
        return float(scale) != 1.0
    except (TypeError, ValueError):
        return True


def find_opened_page(device: Device, root: str = XOCHITL) -> OpenedPage:
    metas: dict[str, dict] = {}
    try:
        entries = device.listdir(root)
    except DeviceError as e:
        raise NotEditable(BAD_FILES_MSG) from e
    for entry in entries:
        if not entry.endswith(".metadata"):
            continue
        try:
            m = json.loads(device.read_bytes(f"{root}/{entry}"))
        except (DeviceError, ValueError) as e:
            raise _damaged(entry) from e
        if not isinstance(m, dict):
            raise _damaged(entry)
        metas[entry[: -len(".metadata")]] = m

    def trashed(uid: str) -> bool:
        seen: set[str] = set()
        cur = metas[uid].get("parent") or ""
        while cur and cur not in seen:
            if cur == TRASH:
                return True
            seen.add(cur)
            if cur not in metas:
                return False
            cur = metas[cur].get("parent") or ""
        return False

    cands: list[tuple[int, int, str]] = []
    for uid, m in metas.items():
        if m.get("type") != "DocumentType" or m.get("deleted") or trashed(uid):
            continue
        opened_at = 0 if "lastOpened" not in m else _num(m["lastOpened"])
        modified = 0 if "lastModified" not in m else _num(m["lastModified"])
        if opened_at is None or modified is None:
            raise _damaged(f"{uid}.metadata")
        cands.append((opened_at, modified, uid))
    if not cands:
        raise NotEditable(NO_DOC_MSG)
    cands.sort(reverse=True)
    if len(cands) > 1 and cands[0][:2] == cands[1][:2]:
        raise NotEditable(TIE_MSG)
    uid = cands[0][2]
    if not _ids.is_safe(uid):
        raise NotEditable(BAD_FILES_MSG)
    meta = metas[uid]
    try:
        content = json.loads(device.read_bytes(f"{root}/{uid}.content"))
    except (DeviceError, ValueError) as e:
        raise NotEditable(BAD_FILES_MSG) from e
    if not isinstance(content, dict):
        raise NotEditable(BAD_FILES_MSG)
    if (content.get("fileType") or "notebook") != "notebook":
        raise NotEditable(PDF_MSG)

    cpages = content.get("cPages")
    pages = cpages.get("pages") if isinstance(cpages, dict) else None
    if not isinstance(pages, list):
        raise NotEditable(BAD_FILES_MSG)
    ids = [p["id"] for p in pages
           if isinstance(p, dict) and isinstance(p.get("id"), str) and not p.get("deleted")]
    if not all(_ids.is_safe(i) for i in ids):
        raise NotEditable(BAD_FILES_MSG)
    index = _num(meta.get("lastOpenedPage"))
    if index is None:
        if len(ids) != 1:
            raise NotEditable(NO_PAGE_MSG)
        index = 0
    if not 0 <= index < len(ids):
        raise NotEditable(BAD_PAGE_MSG)
    if _zoomed(content):
        raise NotEditable(ZOOM_MSG)
    page_id = ids[index]
    path = f"{root}/{uid}/{page_id}.rm"
    if not device.exists(path):
        raise NotEditable(BLANK_PAGE_MSG)
    try:
        data = device.read_bytes(path)
    except DeviceError as e:
        raise NotEditable(BAD_FILES_MSG) from e
    orientation = "landscape" if content.get("orientation") == "landscape" else "portrait"
    entry = next(p for p in pages if isinstance(p, dict) and p.get("id") == page_id)
    return OpenedPage(uid, str(meta.get("visibleName") or ""), page_id, index,
                      len(ids), orientation, data, synced=meta.get("synced") is True,
                      vertical_scroll=_scroll(entry))
