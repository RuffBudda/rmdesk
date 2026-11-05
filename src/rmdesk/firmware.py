"""Firmware detection and the compose (write) safety gate."""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Literal

from rmdesk.device import Device, DeviceError

XOCHITL_DIR = "/home/root/.local/share/remarkable/xochitl"
TESTED_VERSIONS: set[str] = {"3.29.1.0"}
ACCEPTANCE_NOTEBOOK = "rmdesk test"
COMPOSE_DISABLED_MSG = "This firmware isn't tested yet. Writing to notebooks is disabled."


@dataclass
class FirmwareInfo:
    version: str
    serial: str
    rm_format: int | None


def _read_text(device: Device, path: str) -> str | None:
    try:
        return device.read_bytes(path).decode("utf-8", "replace")
    except DeviceError:
        return None


def _version(device: Device) -> str:
    text = _read_text(device, "/etc/os-release")
    if text:
        m = re.search(r'^IMG_VERSION=["\']?([^"\'\n]+)["\']?\s*$', text, re.M)
        if m:
            return m.group(1).strip()
    text = _read_text(device, "/etc/version")
    return text.strip() if text else ""


def _rm_format(device: Device) -> int | None:
    res = device.run(f"find {XOCHITL_DIR} -name '*.rm' -print -quit")
    path = res.out.strip().splitlines()[0] if res.out.strip() else ""
    if not path:
        return None
    try:
        head = device.read_bytes(path)[:43]
    except DeviceError:
        return None
    m = re.match(rb"reMarkable \.lines file, version=(\d+)", head)
    return int(m.group(1)) if m else None


SERIAL_SOURCES = ("/proc/device-tree/serial-number", "/sys/devices/soc0/serial_number")


def _cat(device: Device, path: str) -> str:
    """File contents via `cat` over run() ("" on error). /proc and /sys files report
    size 0, so they are read with cat rather than over SFTP."""
    try:
        res = device.run(f"cat {shlex.quote(path)}")
    except DeviceError:
        return ""
    return res.out if res.code == 0 else ""


def read_serial(device: Device) -> str:
    """The tablet serial number, or "" if it cannot be read.

    rM2 keeps it in the device tree (NUL-terminated); the soc0 path and the
    "Serial" line of /proc/cpuinfo are fallbacks."""
    for path in SERIAL_SOURCES:
        text = _cat(device, path).replace("\0", "").strip()
        if text:
            return text
    m = re.search(r"^Serial\s*:\s*(\S+)\s*$", _cat(device, "/proc/cpuinfo"), re.M)
    return m.group(1) if m else ""


def detect(device: Device) -> FirmwareInfo:
    serial = read_serial(device)
    return FirmwareInfo(_version(device), serial, _rm_format(device))


def compose_allowed(info: FirmwareInfo) -> bool:
    return info.version in TESTED_VERSIONS and info.rm_format == 6


def compose_mode(info: FirmwareInfo | None) -> Literal["full", "test-only", "off"]:
    """"full" on a tested firmware; "test-only" (only the ACCEPTANCE_NOTEBOOK may be
    written) on an untested one that still uses the v6 .rm format; otherwise "off"."""
    if info is None:
        return "off"
    if compose_allowed(info):
        return "full"
    return "test-only" if info.rm_format == 6 else "off"


def testing_banner(version: str) -> str:
    return (f"Testing mode: firmware {version} isn't verified yet, so only the notebook "
            f"named \u201c{ACCEPTANCE_NOTEBOOK}\u201d can be edited.")


class NotTestNotebook(Exception):
    """In testing mode, the notebook about to be written is not the acceptance notebook."""


def check_acceptance(backup_dir, notebook_id: str, version: str) -> None:
    """Fail closed (NotTestNotebook) unless the backup's metadata for `notebook_id` says it
    is a document named ACCEPTANCE_NOTEBOOK."""
    import json
    from pathlib import Path

    try:
        meta = json.loads((Path(backup_dir) / f"{notebook_id}.metadata")
                          .read_text(encoding="utf-8"))
        ok = (meta.get("type") == "DocumentType"
              and is_acceptance_name(str(meta.get("visibleName", ""))))
    except (OSError, ValueError, AttributeError):
        ok = False
    if not ok:
        raise NotTestNotebook(testing_banner(version))


def acceptance_guard(version: str):
    """The guard for safety.send_to_open_page: guard(backup_dir, opened) checks the
    notebook identified after the stop (opened.notebook_id) in the post-stop backup."""
    def guard(backup_dir, opened) -> None:
        check_acceptance(backup_dir, opened.notebook_id, version)
    return guard


def undo_acceptance_guard(notebook_id: str, version: str):
    """The guard for safety.undo_last: guard(backup_dir) on the safety backup."""
    def guard(backup_dir) -> None:
        check_acceptance(backup_dir, notebook_id, version)
    return guard


def is_acceptance_name(name: str) -> bool:
    return name.strip() == ACCEPTANCE_NOTEBOOK
