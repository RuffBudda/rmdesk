"""Safe writes to notebooks on the tablet: backup, send transaction, undo, retention.

No Qt. Every wait goes through an injected `sleep`, every clock through an
injected `now`, so tests never really sleep.

Invariants:
- Nothing is written to the tablet until a verified local backup exists
  (all files copied, re-read and sha256-checked, manifest.json written last).
- A backup directory without manifest.json is incomplete and is never restored.
- Restore never deletes anything on the tablet; it only overwrites the files
  in the backup (temp file + atomic rename) and reports files it did not touch.
- A backup is either of the whole notebook (manifest "scope": "notebook", or no
  "scope" in backups made before page-scoped ones existed) or of one page
  ("scope": "page", "page_id"): that page's .rm plus the notebook's .content and
  .metadata. Compose sends and undo take page backups; a page backup is never
  treated as a copy of the whole notebook.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

from rmdesk.config import Config
from rmdesk import ids
from rmdesk.device import Device, DeviceError
from rmdesk.library.model import XOCHITL
from rmdesk.notebook import page as pagemod
from rmdesk.notebook.opened import OpenedPage, find_opened_page
from rmdesk.stream.install import UNIT as STREAM_UNIT

log = logging.getLogger(__name__)

BACKUP_ROOT = Path.home() / ".local" / "share" / "rmdesk" / "backups"
TMP_SUFFIX = ".rmdesk-tmp"
MANIFEST = "manifest.json"
RESTART_TIMEOUT = 30
# xochitl can crash-loop: seen `active` once, it must still be active this many seconds later
START_RECHECK = 3
# the attribute naming a failed xochitl start on an exception raised before any write
START_ERROR_ATTR = "xochitl_start_error"
RETENTION = timedelta(days=30)
KEEP_NEWEST = 5
IN_PROGRESS_GRACE = timedelta(hours=1)

_STAMP = "%Y%m%d-%H%M%S"
_NAME_RE = re.compile(r"^(\d{8}-\d{6})(?:-(\d+))?$")
# Sidecar files backed up when present (.metadata and .content are required).
_OPTIONAL = (".pagedata", ".local")
SCOPE_NOTEBOOK = "notebook"
SCOPE_PAGE = "page"

Sleep = Callable[[float], None]
Now = Callable[[], datetime]


class RestartTimeout(DeviceError):
    pass


class StopTimeout(DeviceError):
    pass


class BackupError(Exception):
    pass


class RestoreError(Exception):
    """`written`: device paths already replaced from the backup when it failed."""

    def __init__(self, msg: str, written: list[str] | None = None,
                 restart_error: BaseException | None = None):
        super().__init__(msg)
        self.written = list(written or [])
        self.restart_error = restart_error


class UndoFailed(RestoreError):
    """Undo failed after the current state was backed up to `safety_backup`."""

    def __init__(self, cause: BaseException, safety_backup: Path, files_written: bool,
                 restart_error: BaseException | None = None):
        self.cause = cause
        self.safety_backup = safety_backup
        self.files_written = files_written
        state = ("some files were already replaced" if files_written
                 else "no files on the tablet were changed")
        msg = (f"undo failed ({cause}); {state}. The page as it was before "
               f"the undo is backed up at {safety_backup}")
        if restart_error is not None:
            msg += f". Starting xochitl also failed ({restart_error})"
        super().__init__(msg, getattr(cause, "written", None), restart_error)


class UndoUnavailable(RestoreError):
    """The last send can't be undone safely (e.g. recorded by an older rmdesk); nothing changed."""


class PageChangedSinceSend(RestoreError):
    """The page no longer holds the bytes rmdesk wrote; nothing was changed."""


PAGE_CHANGED_MSG = ("The page changed on the tablet after the last send, "
                    "so rmdesk can't undo it safely.")


class NotebookChanged(Exception):
    pass


class Cancelled(Exception):
    """The user said No (or did not answer) to the confirm; nothing was written."""


class SendFailed(Exception):
    def __init__(self, cause: BaseException, restored: bool, backup_dir: Path | None = None,
                 restore_error: BaseException | None = None,
                 restart_error: BaseException | None = None):
        self.cause = cause
        self.restored = restored
        self.backup_dir = backup_dir
        self.restore_error = restore_error
        self.restart_error = restart_error
        if restored:
            msg = f"send failed ({cause}); the notebook was restored from {backup_dir}"
            if restart_error is not None:
                msg += f", but starting xochitl failed ({restart_error})"
        else:
            msg = (f"send failed ({cause}) and restoring the backup also failed "
                   f"({restore_error}); your backup is at {backup_dir}")
            if restart_error is not None:
                msg += f". Starting xochitl also failed ({restart_error})"
        super().__init__(msg)


@dataclass
class SendResult:
    backup_dir: Path
    notebook_id: str | None = None
    page_id: str | None = None
    page_sha256: str | None = None  # sha256 of the page bytes rmdesk wrote

    def last_send(self, name: str | None = None) -> dict:
        """The cfg.last_send record that undo_last needs for a page-scoped undo."""
        return {"notebook_id": self.notebook_id, "name": name, "page_id": self.page_id,
                "page_sha256": self.page_sha256, "backup_dir": str(self.backup_dir)}


@dataclass
class RestoreResult:
    written: list[str] = field(default_factory=list)
    # Files of the notebook on the tablet that are not in the backup; left alone.
    extra: list[str] = field(default_factory=list)
    # undo_last only: backup of the state that the undo replaced.
    safety_backup: Path | None = None


# --- xochitl ---------------------------------------------------------------

def _systemctl(device: Device, verb: str) -> None:
    res = device.run(f"systemctl {verb} xochitl")
    if res.code != 0:
        raise DeviceError(f"systemctl {verb} xochitl failed: {res.err.strip() or res.code}")


def _poll(device: Device, want_active: bool, timeout: float, sleep: Sleep) -> bool:
    """Poll `systemctl is-active xochitl` every 1 s; True once the state is reached."""
    waited = 0.0
    while True:
        active = device.run("systemctl is-active xochitl").out.strip() == "active"
        if active == want_active:
            return True
        if waited >= timeout:
            return False
        sleep(1)
        waited += 1


STREAM_LATER_S = 8  # a second stream restart this long after xochitl starts, on the tablet
STREAM_LATER_CMD = (f"nohup sh -c 'sleep {STREAM_LATER_S}; systemctl try-restart {STREAM_UNIT}' "
                    ">/dev/null 2>&1 &")


def _refresh_stream_service(device: Device) -> None:
    """Best effort: goMarkableStream reads xochitl's process memory, so it must be
    restarted (only if running) whenever xochitl gets a new process.

    Restarted once now and once more STREAM_LATER_S seconds later, by the tablet itself (the
    command returns at once): right after a start xochitl may still be loading, and a stream
    that latched on then shows noise (seen on fw 3.29.1.0 with the Type Folio attached)."""
    for cmd in (f"systemctl try-restart {STREAM_UNIT}", STREAM_LATER_CMD):
        try:
            res = device.run(cmd, timeout=30)
            if res.code != 0:
                log.warning("%s failed: %s", cmd, res.err.strip() or res.code)
        except Exception as e:
            log.warning("%s failed: %s", cmd, e)


def restart_xochitl(device: Device, timeout: float = RESTART_TIMEOUT,
                    sleep: Sleep = time.sleep) -> None:
    """Restart xochitl and wait until systemd reports it exactly `active`.

    For uploads/trash only; notebook edits use stop_xochitl/start_xochitl so
    xochitl cannot save an open notebook over the files we write.
    """
    _systemctl(device, "restart")
    if not _poll(device, True, timeout, sleep):
        raise RestartTimeout(f"xochitl did not become active within {timeout:g} s")
    _refresh_stream_service(device)


def stop_xochitl(device: Device, timeout: float = RESTART_TIMEOUT,
                 sleep: Sleep = time.sleep) -> None:
    """Stop xochitl and wait until `systemctl is-active` no longer says `active`."""
    _systemctl(device, "stop")
    if not _poll(device, False, timeout, sleep):
        raise StopTimeout(f"xochitl did not stop within {timeout:g} s")


def start_xochitl(device: Device, timeout: float = RESTART_TIMEOUT,
                  sleep: Sleep = time.sleep) -> None:
    """Start xochitl and wait until systemd reports it exactly `active`, then check it is
    still active START_RECHECK seconds later (a crash right after starting is a failed
    start: RestartTimeout)."""
    _systemctl(device, "start")
    if not _poll(device, True, timeout, sleep):
        raise RestartTimeout(f"xochitl did not become active within {timeout:g} s")
    sleep(START_RECHECK)
    if device.run("systemctl is-active xochitl").out.strip() != "active":
        raise RestartTimeout(f"xochitl stopped again within {START_RECHECK:g} s of starting")
    _refresh_stream_service(device)


def _start_quiet(device: Device, timeout: float, sleep: Sleep) -> BaseException | None:
    try:
        start_xochitl(device, timeout=timeout, sleep=sleep)
    except Exception as e:
        return e
    return None


def start_error_of(e: BaseException) -> BaseException | None:
    """The failed xochitl start that came with error `e`, if any."""
    err = getattr(e, START_ERROR_ATTR, None)
    if err is None:
        err = getattr(e, "restart_error", None)
    return err if isinstance(err, BaseException) else None


def _start_after(e: BaseException, device: Device, timeout: float, sleep: Sleep) -> None:
    """Nothing was written and `e` is about to be re-raised: start xochitl again. A failed
    start is never silent: it is attached to `e` (start_error_of) and logged."""
    err = _start_quiet(device, timeout, sleep)
    if err is None:
        return
    log.warning("starting xochitl after %s failed: %s", type(e).__name__, err)
    try:
        setattr(e, START_ERROR_ATTR, err)
    except Exception:
        pass


# --- helpers ---------------------------------------------------------------

def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _local(backup_dir: Path, rel: str) -> Path:
    parts = rel.split("/")
    if rel.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise RestoreError(f"bad path in manifest: {rel!r}")
    return backup_dir.joinpath(*parts)


def _device_files(device: Device, notebook_id: str) -> list[str]:
    """Device paths (relative to XOCHITL) of the notebook's files worth backing up."""
    base = f"{XOCHITL}/{notebook_id}"
    rels = [f"{notebook_id}.metadata", f"{notebook_id}.content"]
    rels += [notebook_id + ext for ext in _OPTIONAL if device.exists(base + ext)]
    if device.exists(base):
        res = device.run(f"find {shlex.quote(base)} -type f")
        if res.code != 0:
            raise DeviceError(f"cannot list {base}: {res.err.strip() or res.code}")
        prefix = base + "/"
        for line in sorted(res.out.splitlines()):
            if not line:
                continue
            if not line.startswith(prefix) or "/../" in line or line.endswith("/.."):
                raise DeviceError(f"unexpected path from find: {line!r}")
            if line.endswith(TMP_SUFFIX):
                continue  # a leftover temp file of ours, not user data
            rels.append(line[len(XOCHITL) + 1:])
    return rels


def private_mkdir(path: Path) -> None:
    """Create `path` and any missing parents as 0700 (backups hold the user's notes and
    tablet settings). Directories that already exist are left as they are."""
    missing = []
    p = Path(path)
    while not p.exists():
        missing.append(p)
        if p.parent == p:
            break
        p = p.parent
    for d in reversed(missing):
        try:
            d.mkdir(mode=0o700)
        except FileExistsError:
            continue
        os.chmod(d, 0o700)  # mkdir's mode is masked by the umask


def write_private(path: Path, data: bytes, exclusive: bool = False) -> None:
    """Write a 0600 file (fsynced); `exclusive` raises FileExistsError instead of overwriting."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    with open(fd, "wb") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _write_local(path: Path, data: bytes) -> None:
    private_mkdir(path.parent)
    write_private(path, data)


def _new_backup_dir(root: Path, notebook_id: str, now: Now) -> Path:
    parent = root / notebook_id
    private_mkdir(parent)
    stamp = now().strftime(_STAMP)
    n = 1
    while True:
        d = parent / (stamp if n == 1 else f"{stamp}-{n}")
        try:
            d.mkdir(mode=0o700)  # atomic: never reuse an existing directory
        except FileExistsError:
            n += 1
            continue
        os.chmod(d, 0o700)
        return d


def _remove_quiet(device: Device, paths: list[str]) -> None:
    if not paths:
        return
    try:
        device.run("rm -f -- " + " ".join(shlex.quote(p) for p in paths))
    except Exception:
        pass


# --- backup ----------------------------------------------------------------

def backup(device: Device, notebook_id: str, root: Path = BACKUP_ROOT,
           now: Now | None = None) -> Path:
    """Copy the notebook's files to root/<id>/<YYYYmmdd-HHMMSS>/ and verify them.

    Only reads from the device. manifest.json is written last (atomically); a
    directory without it is incomplete. On failure the partial directory is
    removed and BackupError is raised.
    """
    return _backup(device, notebook_id, lambda: _device_files(device, notebook_id),
                   {"scope": SCOPE_NOTEBOOK}, root, now)


def backup_page(device: Device, notebook_id: str, page_id: str, root: Path = BACKUP_ROOT,
                now: Now | None = None) -> Path:
    """A page-scoped backup: <id>/<page_id>.rm (when it exists) plus <id>.content and
    <id>.metadata, nothing else of the notebook is read. Same layout, checks and
    manifest as backup(), with "scope": "page" and "page_id" in the manifest."""
    if not ids.is_safe(page_id):
        raise BackupError(f"bad page id: {page_id!r}")

    def rels() -> list[str]:
        out = [f"{notebook_id}.metadata", f"{notebook_id}.content"]
        page = f"{notebook_id}/{page_id}.rm"
        if device.exists(f"{XOCHITL}/{page}"):
            out.append(page)
        return out
    return _backup(device, notebook_id, rels, {"scope": SCOPE_PAGE, "page_id": page_id},
                   root, now)


def _backup(device: Device, notebook_id: str, list_files: Callable[[], list[str]],
            extra: dict, root: Path, now: Now | None) -> Path:
    now = now or datetime.now
    if not ids.is_safe(notebook_id):
        raise BackupError(f"bad notebook id: {notebook_id!r}")
    try:
        bdir = _new_backup_dir(root, notebook_id, now)
    except OSError as e:
        raise BackupError(f"cannot create a backup directory under {root}: {e}") from e
    try:
        try:
            rels = list_files()
            copied = {rel: device.read_bytes(f"{XOCHITL}/{rel}") for rel in rels}
        except DeviceError as e:
            raise BackupError(f"cannot read notebook {notebook_id}: {e}") from e
        for rel, data in copied.items():
            _write_local(_local(bdir, rel), data)
        files = {}
        for rel, data in copied.items():
            want = _sha(data)
            if _sha(_local(bdir, rel).read_bytes()) != want:
                raise BackupError(f"backup copy of {rel} does not match the tablet")
            files[rel] = {"sha256": want, "size": len(data)}
        manifest = {"notebook_id": notebook_id, "created": now().isoformat(),
                    "device_root": XOCHITL, "files": files, **extra}
        tmp = bdir / (MANIFEST + ".tmp")
        _write_local(tmp, json.dumps(manifest, indent=1, sort_keys=True).encode("utf-8"))
        os.replace(tmp, bdir / MANIFEST)
    except BaseException as e:
        shutil.rmtree(bdir, ignore_errors=True)
        if isinstance(e, (BackupError, KeyboardInterrupt, SystemExit)):
            raise
        raise BackupError(f"backup of {notebook_id} failed: {e}") from e
    return bdir


def is_full_backup(manifest: dict) -> bool:
    """A copy of the whole notebook (older backups have no "scope": they all are)."""
    return manifest.get("scope", SCOPE_NOTEBOOK) == SCOPE_NOTEBOOK


def _load_backup(backup_dir: Path) -> tuple[dict, dict[str, bytes]]:
    """Read and check a complete backup; returns (manifest, rel -> bytes)."""
    try:
        manifest = json.loads((backup_dir / MANIFEST).read_text(encoding="utf-8"))
        entries = manifest["files"]
        manifest["notebook_id"]
    except FileNotFoundError:
        raise RestoreError(f"{backup_dir} is incomplete (no {MANIFEST}); refusing to restore")
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise RestoreError(f"cannot read {backup_dir / MANIFEST}: {e}") from e
    data = {}
    for rel, entry in entries.items():
        try:
            b = _local(backup_dir, rel).read_bytes()
        except OSError as e:
            raise RestoreError(f"backup file {rel} unreadable: {e}") from e
        if len(b) != entry["size"] or _sha(b) != entry["sha256"]:
            raise RestoreError(f"backup file {rel} is corrupted; refusing to restore")
        data[rel] = b
    return manifest, data


def _restore_order(rel: str) -> tuple[int, str]:
    # Page files first, sidecars next, .content then .metadata last.
    if "/" in rel:
        return (0, rel)
    if rel.endswith(".content"):
        return (2, rel)
    if rel.endswith(".metadata"):
        return (3, rel)
    return (1, rel)


def _restore_files(device: Device, backup_dir: Path) -> RestoreResult:
    manifest, data = _load_backup(backup_dir)  # all checks before any write
    result = RestoreResult()
    for rel in sorted(data, key=_restore_order):
        dst = f"{XOCHITL}/{rel}"
        tmp = dst + TMP_SUFFIX
        try:
            device.write_bytes(tmp, data[rel])
            device.rename(tmp, dst)
        except Exception as e:
            _remove_quiet(device, [tmp])
            raise RestoreError(f"restoring {rel} failed: {e}", written=result.written) from e
        result.written.append(dst)
    if not is_full_backup(manifest):
        return result  # a page backup never had the other files: none of them is "extra"
    try:
        current = _device_files(device, manifest["notebook_id"])
        result.extra = [f"{XOCHITL}/{r}" for r in current if r not in data]
    except Exception:
        pass  # informational only
    return result


def _with_start_error(e: RestoreError, start_error: BaseException | None) -> RestoreError:
    if start_error is None:
        return e
    return RestoreError(f"{e}; starting xochitl also failed ({start_error})",
                        written=e.written, restart_error=start_error)


def restore(device: Device, backup_dir: Path, timeout: float = RESTART_TIMEOUT,
            sleep: Sleep = time.sleep) -> RestoreResult:
    """Write every file of a complete backup back (temp + rename) with xochitl stopped.

    Sequence: check the backup, stop xochitl (wait until not active), write the
    files, start xochitl (wait until active). Files on the tablet that are not
    in the backup are never deleted; they are listed in RestoreResult.extra.
    Every failure is a RestoreError whose `written` lists the files already
    replaced (empty if nothing on the tablet changed) and `restart_error` set
    if xochitl could not be started again.
    """
    backup_dir = Path(backup_dir)
    _load_backup(backup_dir)  # refuse a bad backup before stopping anything
    try:
        stop_xochitl(device, timeout=timeout, sleep=sleep)
    except BaseException as e:
        start_error = _start_quiet(device, timeout, sleep)
        if not isinstance(e, Exception):
            raise
        raise RestoreError(f"could not stop xochitl ({e}); nothing was written",
                           restart_error=start_error) from e
    try:
        result = _restore_files(device, backup_dir)
    except BaseException as e:
        start_error = _start_quiet(device, timeout, sleep)
        if not isinstance(e, RestoreError) or start_error is None:
            raise
        raise _with_start_error(e, start_error) from e
    try:
        start_xochitl(device, timeout=timeout, sleep=sleep)
    except Exception as e:
        raise RestoreError(f"files restored, but xochitl did not start ({e})",
                           written=result.written, restart_error=e) from e
    return result


# --- send ------------------------------------------------------------------

def send_to_page(device: Device, notebook_id: str, page_id: str, new_page: bytes,
                 original: bytes, expected_lines: int, expected_text: str | None,
                 *, root: Path = BACKUP_ROOT,
                 timeout: float = RESTART_TIMEOUT, sleep: Sleep = time.sleep,
                 now: Now | None = None,
                 guard: Callable[[Path], None] | None = None) -> SendResult:
    """Replace one page on the tablet, fail-safe.

    (1) stop xochitl and wait until it is not active (stopping may save its
    in-memory edits), (2) verified backup of the stopped notebook, (3) the
    target page in that backup equals `original` (else NotebookChanged: the
    user's strokes on it were just saved), (4) page.verify, (5) write temp +
    rename over the page, (6) start xochitl and wait until active.
    The target-page bytes check in (3) is the only staleness check: other
    files (.metadata, .content, .local, other pages) may legitimately change
    when the notebook is opened or xochitl stops, and are backed up as they are.
    Steps 1-4 never write to the tablet; on failure xochitl is started again
    and the error re-raised. A failure in 5 or 6 restores the post-stop backup
    while xochitl is stopped, starts it, and raises SendFailed;
    KeyboardInterrupt/SystemExit are re-raised after the restore attempt.
    """
    # (1) stop xochitl: it may save in-memory edits now, and must not save over our write
    try:
        stop_xochitl(device, timeout=timeout, sleep=sleep)
    except BaseException as e:
        _start_after(e, device, timeout, sleep)  # nothing was written
        raise
    return _write_stopped(device, notebook_id, page_id, new_page, original,
                          expected_lines, expected_text, root=root, timeout=timeout,
                          sleep=sleep, now=now, guard=guard)


def _write_stopped(device: Device, notebook_id: str, page_id: str, new_page: bytes,
                   original: bytes, expected_lines: int, expected_text: str | None,
                   *, root: Path, timeout: float, sleep: Sleep, now: Now | None,
                   guard: Callable[[Path], None] | None,
                   page_scoped: bool = False) -> SendResult:
    """Steps 2-6 of send_to_page; the caller has already stopped xochitl.

    Always ends in a start attempt. A failure before the write starts
    xochitl and re-raises unchanged; a failure in the write or the start
    restores the post-stop backup while stopped, starts xochitl and raises
    SendFailed (KeyboardInterrupt/SystemExit re-raised after the restore).
    `page_scoped`: back up only the page plus .content and .metadata (backup_page)
    instead of the whole notebook; the write only ever replaces that page.
    """
    if not (ids.is_safe(notebook_id) and ids.is_safe(page_id)):
        raise BackupError(f"bad notebook or page id: {notebook_id!r}, {page_id!r}")
    rel = f"{notebook_id}/{page_id}.rm"
    dst = f"{XOCHITL}/{rel}"
    tmp = dst + TMP_SUFFIX

    # (2) backup of the stopped state, (3) target page unchanged, (4) verify.
    # Any failure here: start xochitl again; nothing was written.
    try:
        if page_scoped:
            bdir = backup_page(device, notebook_id, page_id, root=root, now=now)
        else:
            bdir = backup(device, notebook_id, root=root, now=now)
        if guard is not None:
            guard(bdir)  # sees the notebook as it is now; raising aborts before any write
        try:
            entries = json.loads((bdir / MANIFEST).read_text(encoding="utf-8"))["files"]
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise BackupError(f"cannot read the manifest of {bdir}: {e}") from e
        page_existed = rel in entries
        if page_existed and entries[rel]["sha256"] != _sha(original):
            raise NotebookChanged(f"page {page_id} changed on the tablet since it was loaded")
        if not page_existed and original:
            raise NotebookChanged(f"page {page_id} was removed on the tablet since it was loaded")
        pagemod.verify(original, new_page, expected_lines, expected_text)
    except BaseException as e:
        _start_after(e, device, timeout, sleep)
        raise
    # (5) write temp + rename, (6) start xochitl
    starting = False
    try:
        device.write_bytes(tmp, new_page)
        device.rename(tmp, dst)
        starting = True
        start_xochitl(device, timeout=timeout, sleep=sleep)
    except BaseException as cause:
        if starting:
            try:  # xochitl may be (half) running: stop it again before restoring
                stop_xochitl(device, timeout=timeout, sleep=sleep)
            except Exception:
                pass
        _remove_quiet(device, [tmp])
        restore_error = None
        try:
            _restore_files(device, bdir)
            if not page_existed:
                _remove_quiet(device, [dst])  # the file we created; not user data
        except Exception as e:
            restore_error = e
        restart_error = _start_quiet(device, timeout, sleep)
        if not isinstance(cause, Exception):
            raise  # KeyboardInterrupt/SystemExit: restore was attempted above
        if restore_error is not None:
            raise SendFailed(cause, restored=False, backup_dir=bdir,
                             restore_error=restore_error,
                             restart_error=restart_error) from cause
        raise SendFailed(cause, restored=True, backup_dir=bdir,
                         restart_error=restart_error) from cause
    return SendResult(backup_dir=bdir, notebook_id=notebook_id, page_id=page_id,
                      page_sha256=_sha(new_page))


def send_to_open_page(device: Device,
                      build: Callable[[OpenedPage], tuple[bytes, int]],
                      confirm: Callable[[OpenedPage], bool],
                      *, root: Path = BACKUP_ROOT,
                      timeout: float = RESTART_TIMEOUT, sleep: Sleep = time.sleep,
                      now: Now | None = None,
                      guard: Callable[[Path, OpenedPage], None] | None = None,
                      ) -> tuple[SendResult, OpenedPage]:
    """Add to the page open on the tablet, in one stop/start cycle.

    Order: stop xochitl -> find_opened_page -> confirm(opened) ->
    build(opened) -> (send_to_page steps 2-6 via _write_stopped) post-stop
    backup -> guard(backup_dir, opened) -> backup's page bytes == opened.data
    (else NotebookChanged) -> page.verify(opened.data, new, added_lines, None)
    -> temp + rename -> start xochitl.
    The post-stop backup is page-scoped (backup_page): the open page's .rm plus the
    notebook's .content and .metadata; the other pages are never read.
    `build` returns (new_page_bytes, added_lines). `confirm` and `build` run on
    the caller's thread while xochitl is stopped; no timeout is imposed here.
    Any exception before the write (NotEditable, Cancelled when confirm returns
    False, NotebookChanged, VerifyError, anything from confirm/build/guard,
    including KeyboardInterrupt) starts xochitl and is re-raised unchanged.
    A failure in the write or the start restores the post-stop backup while
    stopped, starts xochitl and raises SendFailed, exactly as send_to_page.
    """
    try:
        stop_xochitl(device, timeout=timeout, sleep=sleep)
    except BaseException as e:
        _start_after(e, device, timeout, sleep)  # nothing was written
        raise
    try:
        opened = find_opened_page(device)
        if not confirm(opened):
            raise Cancelled("not sent")
        new_page, added_lines = build(opened)
    except BaseException as e:
        _start_after(e, device, timeout, sleep)  # nothing was written
        raise
    page_guard = None
    if guard is not None:
        def page_guard(bdir: Path) -> None:
            guard(bdir, opened)
    result = _write_stopped(device, opened.notebook_id, opened.page_id, new_page,
                            opened.data, added_lines, None, root=root, timeout=timeout,
                            sleep=sleep, now=now, guard=page_guard, page_scoped=True)
    return result, opened


# --- undo ------------------------------------------------------------------

def undo_last(device: Device, cfg: Config, *, root: Path = BACKUP_ROOT,
              timeout: float = RESTART_TIMEOUT, sleep: Sleep = time.sleep,
              now: Now | None = None,
              guard: Callable[[Path], None] | None = None) -> RestoreResult:
    """Put back the one page the last send wrote, then clear cfg.last_send (caller saves cfg).

    Page-scoped: only <notebook>/<page_id>.rm is written, from cfg.last_send["backup_dir"],
    and only if the tablet's bytes for that page still have the sha256 recorded at send
    time (cfg.last_send["page_sha256"]). .content, .metadata and every other page are left
    alone. A record without page_id/page_sha256 (an older rmdesk) raises UndoUnavailable;
    a backup of another notebook, or without the page, raises RestoreError; both before
    anything on the tablet is touched.

    Sequence: check the record and the backup, stop xochitl (it may save in-memory
    edits), back up the stopped current page (the safety backup: backup_page), compare the page
    (else PageChangedSinceSend, xochitl started, nothing written), guard, write the page
    (temp + rename), start xochitl. If the stop or the safety backup fails, xochitl is
    started again and RestoreError ("nothing was changed") is raised before anything is
    written. A failure after the safety backup raises UndoFailed, which names the safety
    backup, says whether the page was already replaced and carries any start failure.
    cfg.last_send is cleared only on success.
    """
    last = cfg.last_send or {}
    if not last.get("backup_dir"):
        raise RestoreError("nothing to undo")
    nb, page_id, want = last.get("notebook_id"), last.get("page_id"), last.get("page_sha256")
    if not (isinstance(nb, str) and nb and isinstance(page_id, str) and page_id
            and isinstance(want, str) and want):
        raise UndoUnavailable("this send was recorded without the page it wrote, so it "
                              "can't be undone safely; nothing was changed")
    if "/" in nb or "/" in page_id:
        raise UndoUnavailable(f"bad page in the last send record: {nb}/{page_id}")
    rel = f"{nb}/{page_id}.rm"
    bdir = Path(last["backup_dir"])
    _local(bdir, rel)  # refuses odd ids
    manifest, data = _load_backup(bdir)  # refuse an incomplete backup before anything else
    if manifest["notebook_id"] != nb:
        raise RestoreError(f"the backup {bdir} is of another notebook "
                           f"({manifest['notebook_id']}); nothing was changed")
    if rel not in data:  # nothing to put back (a send never creates a page file)
        raise RestoreError(f"the backup {bdir} has no copy of page {page_id}; "
                           "nothing was changed")
    dst = f"{XOCHITL}/{rel}"
    tmp = dst + TMP_SUFFIX
    try:
        stop_xochitl(device, timeout=timeout, sleep=sleep)
    except BaseException as e:
        start_error = _start_quiet(device, timeout, sleep)
        if not isinstance(e, Exception):
            raise
        raise RestoreError(f"could not stop xochitl ({e}); nothing was changed",
                           restart_error=start_error) from e
    try:
        safety_backup = backup_page(device, nb, page_id, root=root, now=now)
    except BaseException as e:
        start_error = _start_quiet(device, timeout, sleep)
        if not isinstance(e, Exception):
            raise
        raise RestoreError(f"could not back up the current notebook before undo ({e}); "
                           "nothing was changed", restart_error=start_error) from e
    # the page as it is now, read with xochitl stopped: still exactly what rmdesk wrote?
    try:
        current = json.loads((safety_backup / MANIFEST).read_text(encoding="utf-8"))["files"]
        same = current.get(rel, {}).get("sha256") == want
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        start_error = _start_quiet(device, timeout, sleep)
        raise RestoreError(f"cannot read the manifest of {safety_backup} ({e}); "
                           "nothing was changed", restart_error=start_error) from e
    if not same:
        start_error = _start_quiet(device, timeout, sleep)
        shutil.rmtree(safety_backup, ignore_errors=True)  # a copy of an unchanged tablet
        raise PageChangedSinceSend(PAGE_CHANGED_MSG, restart_error=start_error)
    if guard is not None:
        try:
            guard(safety_backup)  # the notebook as it is now; raising aborts before any write
        except BaseException as e:
            _start_after(e, device, timeout, sleep)
            raise
    written: list[str] = []
    try:
        device.write_bytes(tmp, data[rel])
        device.rename(tmp, dst)
        written.append(dst)
    except BaseException as e:
        _remove_quiet(device, [tmp])
        start_error = _start_quiet(device, timeout, sleep)
        if not isinstance(e, Exception):
            raise
        raise UndoFailed(RestoreError(f"restoring {rel} failed: {e}"), safety_backup,
                         files_written=False, restart_error=start_error) from e
    try:
        start_xochitl(device, timeout=timeout, sleep=sleep)
    except Exception as e:
        raise UndoFailed(e, safety_backup, files_written=True, restart_error=e) from e
    cfg.last_send = None
    return RestoreResult(written=written, safety_backup=safety_backup)


# --- retention -------------------------------------------------------------

def _scope_of(bdir: Path) -> str:
    """"page", "notebook" (also for backups without a scope), or "?" if unreadable."""
    try:
        m = json.loads((bdir / MANIFEST).read_text(encoding="utf-8"))
        return SCOPE_PAGE if not is_full_backup(m) else SCOPE_NOTEBOOK
    except (OSError, ValueError, AttributeError):
        return "?"


def prune(root: Path = BACKUP_ROOT, now: datetime | None = None,
          protect: Iterable[Path] = ()) -> list[Path]:
    """Delete old backups; returns the deleted directories.

    Per notebook: complete backups older than 30 days are deleted, except the 5
    newest complete ones. An incomplete backup (no manifest) is deleted once it
    is older than 1 hour; younger ones may be in progress and are kept.
    Directories whose name is not a backup timestamp are never touched, nor are
    `protect`ed ones (e.g. cfg.last_send's backup_dir); those do not count
    toward the 5 kept. Whole-notebook and page backups are counted separately (5
    newest of each are kept), so page backups never push out the last full copies.
    """
    now = now or datetime.now()
    protected = {Path(p).resolve() for p in protect}
    deleted: list[Path] = []
    if not root.is_dir():
        return deleted
    for nb_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        complete: dict[str, list] = {}
        incomplete = []
        for d in nb_dir.iterdir():
            m = _NAME_RE.match(d.name)
            if not m or not d.is_dir() or d.resolve() in protected:
                continue
            try:
                when = datetime.strptime(m.group(1), _STAMP)
            except ValueError:
                continue
            key = (when, int(m.group(2) or 1))
            if (d / MANIFEST).is_file():
                complete.setdefault(_scope_of(d), []).append((key, d))
            else:
                incomplete.append((key, d))
        doomed = []
        for group in complete.values():
            group.sort(reverse=True)
            doomed += [d for (when, _), d in group[KEEP_NEWEST:] if now - when > RETENTION]
        doomed += [d for (when, _), d in incomplete if now - when > IN_PROGRESS_GRACE]
        for d in doomed:
            try:
                shutil.rmtree(d)
            except OSError:
                continue
            deleted.append(d)
    return deleted
