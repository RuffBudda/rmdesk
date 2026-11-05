"""Library operations: upload, download, trash. No Qt."""
from __future__ import annotations

import json
import os
import shlex
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from rmdesk.device import Device, DeviceError
from rmdesk.library.model import XOCHITL, Item
from rmdesk.notebook.render import notebook_svgs
from rmdesk.safety import restart_xochitl

PdfWriter = Callable[[list[bytes], Path], None]

SPACE_MARGIN = 10 * 1024 * 1024
UPLOAD_SUFFIXES = (".pdf", ".epub")  # what an upload takes
_TMP = ".rmdesk-tmp"


class NeedsUsb(Exception):
    pass


@dataclass
class TransferResult:
    path: Path
    ok: bool
    error: str | None = None


@dataclass
class UploadReport:
    results: list[TransferResult]
    restart_error: str | None = None
    restarted: bool = False


class UsbWeb:
    """The tablet's USB web interface."""

    def __init__(self, base: str = "http://10.11.99.1",
                 session: requests.Session | None = None):
        self.base = base.rstrip("/")
        self.session = session if session is not None else requests.Session()

    def available(self) -> bool:
        try:
            return self.session.get(f"{self.base}/documents/", timeout=3).status_code == 200
        except Exception:
            return False

    def upload(self, path: Path, folder_id: str) -> None:
        url = f"{self.base}/documents/{folder_id}" if folder_id else f"{self.base}/documents/"
        self.session.get(url, timeout=10).raise_for_status()
        with open(path, "rb") as f:
            r = self.session.post(f"{self.base}/upload", files={"file": (path.name, f)},
                                  timeout=300)
        r.raise_for_status()

    def download_pdf(self, doc_id: str, dest: Path) -> None:
        r = self.session.get(f"{self.base}/download/{doc_id}/pdf", stream=True, timeout=300)
        if r.status_code == 404:
            r.close()
            r = self.session.get(f"{self.base}/download/{doc_id}/placeholder",
                                 stream=True, timeout=300)
        try:
            r.raise_for_status()
            chunks = r.iter_content(65536)
            first = b""
            for c in chunks:
                if c:
                    first = c
                    break
            if not first.startswith(b"%PDF"):
                raise DeviceError("tablet did not return a PDF")
            with open(dest, "wb") as f:
                f.write(first)
                for chunk in chunks:
                    f.write(chunk)
        finally:
            r.close()


def _write_atomic(device: Device, path: str, data: bytes) -> None:
    tmp = path + _TMP
    device.write_bytes(tmp, data)
    device.rename(tmp, path)


def _free_bytes(device: Device) -> int | None:
    """Free bytes on /home from POSIX `df -Pk` (BusyBox has no --output/-B)."""
    try:
        res = device.run("df -Pk /home")
        if res.code != 0:
            return None
        lines = [ln.split() for ln in res.out.strip().splitlines() if ln.strip()]
        col = lines[0].index("Available")
        return int(lines[-1][col]) * 1024
    except (DeviceError, ValueError, IndexError):
        return None


def upload(device: Device, transport: str, paths: list[Path], folder_id: str,
           web: UsbWeb | None = None) -> UploadReport:
    use_web = transport == "usb" and web is not None and web.available()
    results: dict[int, TransferResult] = {}
    valid: list[tuple[int, Path]] = []
    for i, p in enumerate(paths):
        if p.suffix.lower() in UPLOAD_SUFFIXES:
            valid.append((i, p))
        else:
            results[i] = TransferResult(p, False, "not a PDF/EPUB")

    if use_web:
        for i, p in valid:
            try:
                web.upload(p, folder_id)
                results[i] = TransferResult(p, True, None)
            except Exception as e:
                results[i] = TransferResult(p, False, str(e))
        return UploadReport([results[i] for i in range(len(paths))])

    if valid:
        try:
            need = sum(p.stat().st_size for _, p in valid) + SPACE_MARGIN
        except OSError:
            need = SPACE_MARGIN
        free = _free_bytes(device)
        if free is not None and free < need:
            for i, p in valid:
                results[i] = TransferResult(p, False, "tablet storage full")
            valid = []

    wrote = 0
    for i, p in valid:
        uid = str(uuid.uuid4())
        base = f"{XOCHITL}/{uid}"
        ext = p.suffix.lower()[1:]
        try:
            now = str(int(time.time() * 1000))
            meta = {
                "visibleName": p.stem, "type": "DocumentType", "parent": folder_id,
                "lastModified": now, "version": 0, "deleted": False, "pinned": False,
                "lastOpened": "0", "createdTime": now,
            }
            _write_atomic(device, f"{base}.{ext}", p.read_bytes())
            _write_atomic(device, f"{base}.content", json.dumps({"fileType": ext}).encode())
            _write_atomic(device, f"{base}.metadata", json.dumps(meta).encode("utf-8"))
            wrote += 1
            results[i] = TransferResult(p, True, None)
        except (DeviceError, OSError) as e:
            _cleanup(device, base, ext)
            results[i] = TransferResult(p, False, str(e))
    restart_error = None
    restarted = False
    if wrote:
        try:
            restart_xochitl(device)
            restarted = True
        except DeviceError as e:
            restart_error = str(e)
    return UploadReport([results[i] for i in range(len(paths))], restart_error, restarted)


def _cleanup(device: Device, base: str, ext: str) -> None:
    names = [f"{base}.{ext}", f"{base}.content", f"{base}.metadata"]
    names += [n + _TMP for n in names]
    try:
        device.run("rm -f " + " ".join(shlex.quote(n) for n in names))
    except DeviceError:
        pass


def _atomic_local(dest: Path, produce: Callable[[Path], None]) -> None:
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        produce(part)
        os.replace(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def download_pdf(device: Device, transport: str, item: Item, dest: Path,
                 web: UsbWeb | None = None, writer: PdfWriter | None = None) -> None:
    if transport == "usb" and web is not None and web.available():
        _atomic_local(dest, lambda part: web.download_pdf(item.id, part))
        return
    if item.kind == "notebook":
        if writer is None:
            raise ValueError("a PDF writer is required for Wi-Fi export")
        _atomic_local(dest, lambda part: writer(notebook_svgs(device, item), part))
        return
    raise NeedsUsb("Connect over USB to export annotated PDFs")


def download_original(device: Device, item: Item, dest: Path) -> None:
    ext = "epub" if item.kind == "epub" else "pdf"
    data = device.read_bytes(f"{XOCHITL}/{item.id}.{ext}")
    _atomic_local(dest, lambda part: part.write_bytes(data))


def _trash_metadata(device: Device, item: Item) -> None:
    path = f"{XOCHITL}/{item.id}.metadata"
    meta = json.loads(device.read_bytes(path).decode("utf-8"))
    meta["parent"] = "trash"
    meta["lastModified"] = str(int(time.time() * 1000))
    _write_atomic(device, path, json.dumps(meta).encode("utf-8"))


def trash(device: Device, item: Item, *, restart: bool = True) -> None:
    _trash_metadata(device, item)
    if restart:
        restart_xochitl(device)


def trash_many(device: Device, items: list[Item]) -> list[TransferResult]:
    """Trash each item without restarting xochitl; results are in `items` order.

    One failure does not stop the rest. `path` carries the item's name.
    """
    results: list[TransferResult] = []
    for item in items:
        try:
            _trash_metadata(device, item)
            results.append(TransferResult(Path(item.name), True, None))
        except (DeviceError, OSError, ValueError) as e:
            results.append(TransferResult(Path(item.name), False, str(e)))
    return results
