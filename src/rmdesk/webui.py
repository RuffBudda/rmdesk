"""The tablet's USB web interface switch (WebInterfaceEnabled in xochitl.conf). No Qt.

With the web interface on, USB uploads go through http://10.11.99.1 and need no
xochitl restart. Firmware 3.29 has no menu toggle, so rmdesk edits the one line
in xochitl.conf, with xochitl stopped (it rewrites its conf on exit).

Invariants:
- Only the value of the single `WebInterfaceEnabled=` line changes; every other
  byte of the file is kept. If the key is missing or appears more than once,
  nothing is written.
- Before writing, the original is copied to xochitl.conf.rmdesk-bak on the
  tablet (and, the first time only, to xochitl.conf.rmdesk-orig), and to a
  local copy (0600, in a 0700 folder) with the values of the secret lines
  (DeveloperPassword=, Passcode=) blanked. Device writes use a temp file + rename.
- On any failure after the stop, the original bytes are put back while
  xochitl is stopped, then xochitl is started.
"""
from __future__ import annotations

import re
import shlex
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, NoReturn

from rmdesk.device import Device
from rmdesk.safety import private_mkdir, start_xochitl, stop_xochitl, write_private

XOCHITL_CONF = "/home/root/.config/remarkable/xochitl.conf"
KEY = "WebInterfaceEnabled"
DEVICE_BACKUP = XOCHITL_CONF + ".rmdesk-bak"  # the conf before rmdesk's last change
DEVICE_ORIG = XOCHITL_CONF + ".rmdesk-orig"  # the conf before rmdesk's first change
TMP = XOCHITL_CONF + ".rmdesk-tmp"
LOCAL_DIR = "xochitl-conf"
WEB_TIMEOUT = 15
WEB_NO_ANSWER = ("The tablet restarted but the USB web interface didn't answer. "
                 "Is the USB cable connected?")

_STAMP = "%Y%m%d-%H%M%S"
# lines whose values must never be copied off the tablet in plain text
SECRET_KEYS = ("DeveloperPassword", "Passcode")
REDACTED = b"<removed by rmdesk>"
_SECRET_RE = re.compile(rb"^((?:" + b"|".join(k.encode() for k in SECRET_KEYS)
                        + rb")=)[^\r\n]*", re.MULTILINE)
# the value runs to the end of the line, excluding a CR of a CRLF ending
_LINE_RE = re.compile(rb"^" + KEY.encode() + rb"=([^\r\n]*)", re.MULTILINE)


class WebUiError(Exception):
    """`backup`: the local copy of the original xochitl.conf, if one was made."""

    def __init__(self, msg: str, backup: Path | None = None):
        super().__init__(msg)
        self.backup = backup


def _parse(data: bytes) -> bool | None:
    matches = _LINE_RE.findall(data)
    if len(matches) != 1:
        return None
    value = matches[0].strip().lower()
    return {b"true": True, b"false": False}.get(value)


def read_state(device: Device) -> bool | None:
    """True/False from the WebInterfaceEnabled line; None if absent or ambiguous.

    A missing conf file reads as None; any other read error (DeviceError) is raised.
    """
    if not device.exists(XOCHITL_CONF):
        return None
    return _parse(device.read_bytes(XOCHITL_CONF))


def _write_atomic(device: Device, path: str, data: bytes) -> None:
    tmp = path + ".rmdesk-tmp"  # TMP for XOCHITL_CONF
    device.write_bytes(tmp, data)
    device.rename(tmp, path)


def _remove_quiet(device: Device, paths: list[str]) -> None:
    try:
        device.run("rm -f -- " + " ".join(shlex.quote(p) for p in paths))
    except Exception:
        pass


def _start_quiet(device: Device, sleep) -> BaseException | None:
    try:
        start_xochitl(device, sleep=sleep)
    except Exception as e:
        return e
    return None


def redact(data: bytes) -> bytes:
    """The conf with the values of the SECRET_KEYS lines replaced; every other byte kept."""
    return _SECRET_RE.sub(lambda m: m.group(1) + REDACTED, data)


def _save_local(root: Path, data: bytes, now: Callable[[], datetime]) -> Path:
    folder = root / LOCAL_DIR
    private_mkdir(folder)
    data = redact(data)
    stamp = now().strftime(_STAMP)
    n = 1
    while True:
        path = folder / (f"{stamp}.conf" if n == 1 else f"{stamp}-{n}.conf")
        try:
            write_private(path, data, exclusive=True)  # never overwrite an earlier backup
        except FileExistsError:
            n += 1
            continue
        if path.read_bytes() != data:
            raise OSError(f"backup {path} did not read back correctly")
        return path


def set_enabled(device: Device, enabled: bool, *, backup_root: Path,
                sleep: Callable[[float], None] = time.sleep,
                check_web: Callable[[], bool],
                now: Callable[[], datetime] = datetime.now,
                clock: Callable[[], float] = time.monotonic) -> None:
    """Set WebInterfaceEnabled, restarting xochitl once.

    When enabling, waits up to 15 s (by `clock`, probe time included) for `check_web()`; if it never answers the
    setting is kept (the interface only runs while USB is connected) and
    WebUiError(WEB_NO_ANSWER) is raised.
    """
    try:
        stop_xochitl(device, sleep=sleep)
    except Exception as e:
        start_err = _start_quiet(device, sleep)
        msg = f"Could not stop the tablet's interface ({e}); nothing was changed."
        if start_err is not None:
            msg += f" Starting it again also failed ({start_err}); restart the tablet."
        raise WebUiError(msg) from e

    def give_up(msg: str, cause: BaseException | None = None) -> NoReturn:
        start_err = _start_quiet(device, sleep)
        if start_err is not None:
            msg += f" Starting the tablet's interface also failed ({start_err}); restart the tablet."
        raise WebUiError(msg) from cause

    try:
        original = device.read_bytes(XOCHITL_CONF)
    except Exception as e:
        give_up(f"Could not read {XOCHITL_CONF} ({e}); nothing was changed.", e)
    matches = list(_LINE_RE.finditer(original))
    if not matches:
        give_up(f"The setting {KEY} isn't in {XOCHITL_CONF}; nothing was changed.")
    if len(matches) > 1:
        give_up(f"The setting {KEY} appears more than once in {XOCHITL_CONF}; "
                "nothing was changed.")
    m = matches[0]
    new = original[:m.start(1)] + (b"true" if enabled else b"false") + original[m.end(1):]

    if new != original:
        try:
            local = _save_local(backup_root, original, now)
        except Exception as e:
            give_up(f"Could not save a local backup of the tablet's settings ({e}); "
                    "nothing was changed.", e)
        try:
            if not device.exists(DEVICE_ORIG):
                _write_atomic(device, DEVICE_ORIG, original)
            _write_atomic(device, DEVICE_BACKUP, original)
            _write_atomic(device, XOCHITL_CONF, new)
            if device.read_bytes(XOCHITL_CONF) != new:
                raise OSError(f"{XOCHITL_CONF} did not read back correctly")
            start_xochitl(device, sleep=sleep)
        except Exception as e:
            _restore(device, original, local, e, sleep)
    else:
        try:
            start_xochitl(device, sleep=sleep)
        except Exception as e:
            raise WebUiError(f"Could not start the tablet's interface ({e}); "
                             "restart the tablet.") from e

    if enabled:
        deadline = clock() + WEB_TIMEOUT
        while not check_web():
            if clock() >= deadline:
                raise WebUiError(WEB_NO_ANSWER)
            sleep(1)


def _restore(device: Device, original: bytes, local: Path, cause: BaseException,
             sleep) -> NoReturn:
    """Put the original conf back with xochitl stopped, start it, raise WebUiError."""
    restore_err: BaseException | None = None
    try:
        stop_xochitl(device, sleep=sleep)  # a failed start may have half-worked
    except Exception:
        pass  # most likely not running; restoring is still the right move
    try:
        _write_atomic(device, XOCHITL_CONF, original)
    except Exception as e:
        restore_err = e
        _remove_quiet(device, [TMP])
    start_err = _start_quiet(device, sleep)
    if restore_err is None:
        msg = (f"Could not change the setting ({cause}); the original settings file was "
               f"put back. A backup is at {local}.")
    else:
        msg = (f"Could not change the setting ({cause}) and restoring the original also "
               f"failed ({restore_err}). Your backup is on the tablet at {DEVICE_BACKUP} "
               f"(and at {local}, with its passwords removed).")
    if start_err is not None:
        msg += f" Starting the tablet's interface also failed ({start_err}); restart the tablet."
    raise WebUiError(msg, backup=local) from cause
