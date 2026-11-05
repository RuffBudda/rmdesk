import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from rmscene import scene_items as si

from rmdesk import safety
from rmdesk.config import Config
from rmdesk.device import CommandResult, DeviceError
from rmdesk.library.model import XOCHITL
from rmdesk.notebook import page
from rmdesk.notebook.page import VerifyError
from tests.fakes import FailRule, FakeDevice

FIX = Path(__file__).parent / "fixtures" / "rm"
NB = "11111111-2222-3333-4444-555555555555"
PG = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PG2 = "ffffffff-0000-1111-2222-333333333333"
META = f"{XOCHITL}/{NB}.metadata"
CONTENT = f"{XOCHITL}/{NB}.content"
PAGE = f"{XOCHITL}/{NB}/{PG}.rm"
PAGE2 = f"{XOCHITL}/{NB}/{PG2}.rm"
TMP = PAGE + ".rmdesk-tmp"
NOW = datetime(2026, 11, 3, 12, 0, 0)

WRITES = ("write_bytes", "rename")


def _lines(n=2):
    return [
        si.Line(
            color=si.PenColor.RED, tool=si.Pen.FINELINER_2,
            points=[si.Point(-100.0 + 10 * i + k, 200.5 + k, 0, 0, 4, 255) for k in range(5)],
            thickness_scale=2.0, starting_length=0.0,
        )
        for i in range(n)
    ]


@pytest.fixture
def pages():
    orig = (FIX / "Normal_AB.rm").read_bytes()
    p = page.load(orig)
    p.append_layer("rmdesk", _lines(2))
    return orig, p.to_bytes()


def device(orig, **kw):
    files = {
        META: b'{"visibleName": "nb"}',
        CONTENT: b'{"pages": []}',
        f"{XOCHITL}/{NB}.pagedata": b"Blank\n",
        PAGE: orig,
        f"{XOCHITL}/{NB}/{PG}-metadata.json": b"{}",
        PAGE2: b"other page",
        f"{XOCHITL}/{NB}.thumbnails/{PG}.png": b"png",
        f"{XOCHITL}/other.metadata": b"{}",
    }
    return FakeDevice(files=files, **kw)


def never_active(cmd):
    if "is-active" in cmd:
        return CommandResult(3, "activating\n", "")
    return None


def device_writes(d):
    """Writes, renames and xochitl state changes (anything but is-active polls)."""
    return [(m, a) for m, a in d.log if m in WRITES
            or (m == "run" and a.startswith("systemctl") and "is-active" not in a)]


def nothing_written(d):
    """No write/rename reached the tablet, and xochitl was left running
    (either never stopped, or stopped and started again)."""
    return (not any(m in WRITES for m, _ in d.log) and d.xochitl_active
            and systemctl(d) in ([], ["systemctl stop xochitl", "systemctl start xochitl"]))


def writes_while_running(d):
    """write_bytes/rename calls issued while xochitl had been started (per the log)."""
    running, bad = True, []
    for m, a in d.log:
        if m == "run" and a in ("systemctl start xochitl", "systemctl restart xochitl"):
            running = True
        elif m == "run" and a == "systemctl stop xochitl":
            running = False
        elif m in WRITES and running:
            bad.append((m, a))
    return bad


def index(d, entry, last=False):
    hits = [i for i, e in enumerate(d.log) if e == entry]
    return hits[-1] if last else hits[0]


def systemctl(d):
    return [a for m, a in d.log if m == "run" and a.startswith("systemctl")
            and "is-active" not in a and "try-restart" not in a]


def send(d, orig, new, root, **kw):
    args = dict(expected_lines=2, expected_text=None,
                root=root, sleep=lambda s: None, now=lambda: NOW)
    args.update(kw)
    return safety.send_to_page(d, NB, PG, new, orig, **args)


# --- restart_xochitl -------------------------------------------------------

def test_restart_polls_until_active():
    answers = iter(["inactive\n", "activating\n", "active\n"])
    d = FakeDevice(run_handler=lambda c: CommandResult(0, next(answers), "") if "is-active" in c else None)
    sleeps = []
    safety.restart_xochitl(d, sleep=sleeps.append)
    runs = [a for m, a in d.log if m == "run"]
    assert runs[0] == "systemctl restart xochitl"
    assert runs[1:4] == ["systemctl is-active xochitl"] * 3
    assert runs[4:] == ["systemctl try-restart goMarkableStream", safety.STREAM_LATER_CMD]
    assert sleeps == [1, 1]


def test_restart_timeout():
    d = FakeDevice(run_handler=never_active)
    sleeps = []
    with pytest.raises(safety.RestartTimeout) as ei:
        safety.restart_xochitl(d, timeout=30, sleep=sleeps.append)
    assert isinstance(ei.value, DeviceError)
    assert sum(sleeps) == 30


STREAM_CMD = "systemctl try-restart goMarkableStream"


def _runs(d):
    return [a for m, a in d.log if m == "run"]


def test_restart_and_start_refresh_stream_after_xochitl():
    for fn, verb in ((safety.restart_xochitl, "restart"), (safety.start_xochitl, "start")):
        d = FakeDevice()
        fn(d, sleep=lambda s: None)
        runs = _runs(d)
        assert runs.index(STREAM_CMD) > runs.index(f"systemctl {verb} xochitl")
        assert runs[-2:] == [STREAM_CMD, safety.STREAM_LATER_CMD]


@pytest.mark.parametrize("failure", [
    {"code": 1}, {"exc": DeviceError("ssh down")}, {"exc": TimeoutError("slow")}])
def test_stream_refresh_failure_is_ignored(failure):
    def handler(cmd):
        if "try-restart" in cmd:
            if "exc" in failure:
                raise failure["exc"]
            return CommandResult(failure["code"], "", "boom")
        return None
    for fn in (safety.restart_xochitl, safety.start_xochitl):
        fn(FakeDevice(run_handler=handler), sleep=lambda s: None)


def test_stream_refresh_not_run_when_xochitl_never_active():
    d = FakeDevice(run_handler=never_active)
    with pytest.raises(safety.RestartTimeout):
        safety.start_xochitl(d, timeout=2, sleep=lambda s: None)
    assert STREAM_CMD not in _runs(d)


def test_send_and_undo_succeed_when_stream_refresh_fails(pages, tmp_path):
    orig, new = pages

    def handler(cmd):
        if "try-restart" in cmd:
            raise DeviceError("nope")
        return None
    d = device(orig)
    d.run_handler = handler
    res = send(d, orig, new, tmp_path)
    assert d.files[PAGE] == new
    cfg = Config(last_send=res.last_send(name="nb"))
    safety.undo_last(d, cfg, root=tmp_path, sleep=lambda s: None,
                     now=lambda: NOW + timedelta(seconds=5))
    assert d.files[PAGE] == orig


def test_ops_uses_safety_restart():
    from rmdesk.library import ops
    assert ops.restart_xochitl is safety.restart_xochitl


# --- backup ----------------------------------------------------------------

def test_backup_copies_and_writes_manifest(pages, tmp_path):
    orig, _ = pages
    d = device(orig)
    bdir = safety.backup(d, NB, root=tmp_path, now=lambda: NOW)
    assert bdir == tmp_path / NB / "20261103-120000"
    man = json.loads((bdir / "manifest.json").read_text())
    files = man["files"]
    assert set(files) == {f"{NB}.metadata", f"{NB}.content", f"{NB}.pagedata",
                          f"{NB}/{PG}.rm", f"{NB}/{PG}-metadata.json", f"{NB}/{PG2}.rm"}
    assert files[f"{NB}/{PG}.rm"] == {"sha256": hashlib.sha256(orig).hexdigest(),
                                      "size": len(orig)}
    assert (bdir / NB / f"{PG}.rm").read_bytes() == orig
    assert not any(m in WRITES for m, _ in d.log)


def test_backup_same_second_gets_suffix(pages, tmp_path):
    d = device(pages[0])
    names = [safety.backup(d, NB, root=tmp_path, now=lambda: NOW).name for _ in range(3)]
    assert names == ["20261103-120000", "20261103-120000-2", "20261103-120000-3"]


def test_backup_mismatch_raises_and_leaves_no_manifest(pages, tmp_path, monkeypatch):
    d = device(pages[0])
    real = Path.read_bytes

    def corrupt(self):
        data = real(self)
        return data + b"x" if self.name == f"{PG}.rm" else data
    monkeypatch.setattr(Path, "read_bytes", corrupt)
    with pytest.raises(safety.BackupError):
        safety.backup(d, NB, root=tmp_path, now=lambda: NOW)
    assert not list(tmp_path.rglob("manifest.json"))


def test_backup_missing_metadata_raises(tmp_path):
    d = FakeDevice(files={CONTENT: b"{}"})
    with pytest.raises(safety.BackupError):
        safety.backup(d, NB, root=tmp_path, now=lambda: NOW)


# --- restore ---------------------------------------------------------------

def test_restore_writes_back_and_reports_extras(pages, tmp_path):
    orig, new = pages
    d = device(orig)
    bdir = safety.backup(d, NB, root=tmp_path, now=lambda: NOW)
    d.files[PAGE] = b"garbage"
    del d.files[PAGE2]
    extra = f"{XOCHITL}/{NB}/new-page.rm"
    d.files[extra] = b"user drew this"
    res = safety.restore(d, bdir, sleep=lambda s: None)
    assert d.files[PAGE] == orig
    assert d.files[PAGE2] == b"other page"
    assert d.files[extra] == b"user drew this"  # never deleted
    assert res.extra == [extra]
    assert not any(p.endswith(".rmdesk-tmp") for p in d.files)
    assert systemctl(d) == ["systemctl stop xochitl", "systemctl start xochitl"]
    first_write = next(i for i, (m, _) in enumerate(d.log) if m == "write_bytes")
    last_rename = max(i for i, (m, _) in enumerate(d.log) if m == "rename")
    assert index(d, ("run", "systemctl stop xochitl")) < first_write
    assert index(d, ("run", "systemctl start xochitl")) > last_rename
    assert writes_while_running(d) == []
    assert d.xochitl_active


def test_restore_refuses_backup_without_manifest(pages, tmp_path):
    orig, _ = pages
    d = device(orig)
    bdir = safety.backup(d, NB, root=tmp_path, now=lambda: NOW)
    (bdir / "manifest.json").unlink()
    d.log.clear()
    with pytest.raises(safety.RestoreError):
        safety.restore(d, bdir, sleep=lambda s: None)
    assert device_writes(d) == []  # refused before stopping xochitl


def test_restore_refuses_corrupted_backup(pages, tmp_path):
    orig, _ = pages
    d = device(orig)
    bdir = safety.backup(d, NB, root=tmp_path, now=lambda: NOW)
    (bdir / NB / f"{PG}.rm").write_bytes(b"bitrot")
    d.log.clear()
    with pytest.raises(safety.RestoreError):
        safety.restore(d, bdir, sleep=lambda s: None)
    assert device_writes(d) == []  # refused before stopping xochitl


# --- send_to_page ----------------------------------------------------------

def test_send_happy_path(pages, tmp_path):
    orig, new = pages
    d = device(orig)
    res = send(d, orig, new, tmp_path)
    assert d.files[PAGE] == new
    assert TMP not in d.files
    assert (res.backup_dir / "manifest.json").exists()
    first_write = next(i for i, (m, _) in enumerate(d.log) if m == "write_bytes")
    backup_reads = [i for i, (m, a) in enumerate(d.log) if m == "read_bytes" and a == PAGE]
    assert backup_reads and backup_reads[0] < first_write
    assert index(d, ("run", "systemctl stop xochitl")) < backup_reads[0]  # backup is post-stop
    assert d.log[first_write] == ("write_bytes", TMP)
    assert ("rename", f"{TMP} -> {PAGE}") in d.log
    assert systemctl(d) == ["systemctl stop xochitl", "systemctl start xochitl"]
    assert index(d, ("run", "systemctl stop xochitl")) < first_write
    last_rename = index(d, ("rename", f"{TMP} -> {PAGE}"), last=True)
    assert index(d, ("run", "systemctl start xochitl")) > last_rename
    assert writes_while_running(d) == []
    assert d.xochitl_active


def test_verify_failure_writes_nothing(pages, tmp_path):
    orig, new = pages
    d = device(orig)
    with pytest.raises(VerifyError):
        send(d, orig, new[:-7], tmp_path)
    assert nothing_written(d)
    assert d.files[PAGE] == orig


def test_wrong_line_count_writes_nothing(pages, tmp_path):
    orig, new = pages
    d = device(orig)
    with pytest.raises(VerifyError):
        send(d, orig, new, tmp_path, expected_lines=3)
    assert nothing_written(d)


def test_upload_failure_restores(pages, tmp_path):
    orig, new = pages
    d = device(orig, fail_on={"rename": FailRule(DeviceError("rename boom"), times=1)})
    with pytest.raises(safety.SendFailed) as ei:
        send(d, orig, new, tmp_path)
    assert ei.value.restored is True
    assert isinstance(ei.value.cause, DeviceError)
    assert d.files[PAGE] == orig
    assert TMP not in d.files


def test_write_failure_restores(pages, tmp_path):
    orig, new = pages
    d = device(orig, fail_on={"write_bytes": FailRule(DeviceError("disk full"), times=1)})
    with pytest.raises(safety.SendFailed) as ei:
        send(d, orig, new, tmp_path)
    assert ei.value.restored is True
    assert d.files[PAGE] == orig


def test_restart_timeout_restores(pages, tmp_path):
    orig, new = pages
    d = device(orig, run_handler=never_active)
    with pytest.raises(safety.SendFailed) as ei:
        send(d, orig, new, tmp_path)
    assert ei.value.restored is True
    assert isinstance(ei.value.cause, safety.RestartTimeout)
    assert isinstance(ei.value.restart_error, safety.RestartTimeout)
    assert d.files[PAGE] == orig
    assert writes_while_running(d) == []  # stopped again before restoring


def test_stop_timeout_writes_nothing(pages, tmp_path):
    orig, new = pages
    d = device(orig, run_handler=lambda c: CommandResult(0, "active\n", "")
               if "is-active" in c else None)
    with pytest.raises(safety.StopTimeout):
        send(d, orig, new, tmp_path)
    assert not any(m in WRITES for m, _ in d.log)
    assert systemctl(d) == ["systemctl stop xochitl", "systemctl start xochitl"]
    assert d.files[PAGE] == orig


def test_interrupt_after_rename_restores_and_reraises(pages, tmp_path):
    orig, new = pages
    starts = []

    def h(cmd):
        if cmd == "systemctl start xochitl":
            starts.append(cmd)
            if len(starts) == 1:
                raise KeyboardInterrupt
        return None
    d = device(orig, run_handler=h)
    with pytest.raises(KeyboardInterrupt):
        send(d, orig, new, tmp_path)
    assert d.files[PAGE] == orig
    assert TMP not in d.files
    assert writes_while_running(d) == []
    assert len(starts) == 2  # started again after the restore


def saves_on_stop(d, path, data):
    """xochitl writes its in-memory copy to `path` when it is stopped."""
    def h(cmd):
        if cmd == "systemctl stop xochitl":
            d.files[path] = data
        return None
    d.run_handler = h


def test_xochitl_saving_target_page_on_stop_aborts(pages, tmp_path):
    orig, new = pages
    d = device(orig)
    saves_on_stop(d, PAGE, b"strokes saved at shutdown")
    with pytest.raises(safety.NotebookChanged):
        send(d, orig, new, tmp_path)
    assert nothing_written(d)
    assert systemctl(d) == ["systemctl stop xochitl", "systemctl start xochitl"]
    assert d.files[PAGE] == b"strokes saved at shutdown"


@pytest.mark.parametrize("path", [META, f"{XOCHITL}/{NB}.local", CONTENT, PAGE2,
                                  f"{XOCHITL}/{NB}/brand-new.rm"])
def test_xochitl_saving_other_files_on_stop_still_sends(pages, tmp_path, path):
    orig, new = pages
    d = device(orig)
    saves_on_stop(d, path, b'{"lastOpenedPage": 3}')
    res = send(d, orig, new, tmp_path)
    assert d.files[PAGE] == new
    assert d.files[path] == b'{"lastOpenedPage": 3}'  # not touched by us
    rel = path[len(XOCHITL) + 1:]
    assert (res.backup_dir / rel).read_bytes() == b'{"lastOpenedPage": 3}'  # post-stop copy
    assert writes_while_running(d) == []


def test_restore_also_fails_and_start_fails(pages, tmp_path):
    orig, new = pages
    d = device(orig, fail_on={"rename": DeviceError("rename always fails")},
               run_handler=lambda c: CommandResult(3, "inactive\n", "")
               if "is-active" in c else None)
    with pytest.raises(safety.SendFailed) as ei:
        send(d, orig, new, tmp_path)
    e = ei.value
    assert e.restored is False
    assert isinstance(e.restart_error, safety.RestartTimeout)
    assert "Starting xochitl also failed" in str(e)
    assert str(e.backup_dir) in str(e)


def test_restore_failure_reports_start_failure(pages, tmp_path):
    orig, _ = pages
    d = device(orig)
    bdir = safety.backup(d, NB, root=tmp_path, now=lambda: NOW)
    d.fail_on["rename"] = DeviceError("rename boom")
    d.run_handler = lambda c: CommandResult(3, "inactive\n", "") if "is-active" in c else None
    with pytest.raises(safety.RestoreError) as ei:
        safety.restore(d, bdir, sleep=lambda s: None)
    assert isinstance(ei.value.restart_error, safety.RestartTimeout)
    assert "starting xochitl also failed" in str(ei.value)


def test_restore_start_failure_after_writing_sets_restart_error(pages, tmp_path):
    orig, _ = pages
    d = device(orig)
    bdir = safety.backup(d, NB, root=tmp_path, now=lambda: NOW)
    d.files[PAGE] = b"garbage"
    d.run_handler = lambda c: CommandResult(3, "inactive\n", "") if "is-active" in c else None
    with pytest.raises(safety.RestoreError, match="files restored") as ei:
        safety.restore(d, bdir, sleep=lambda s: None)
    assert isinstance(ei.value.restart_error, safety.RestartTimeout)
    assert PAGE in ei.value.written
    assert d.files[PAGE] == orig


def test_page_deleted_since_load_aborts(pages, tmp_path):
    orig, new = pages
    d = device(orig)
    del d.files[PAGE]
    with pytest.raises(safety.NotebookChanged):
        send(d, orig, new, tmp_path)
    assert nothing_written(d)
    assert PAGE not in d.files


def test_backup_dir_error_is_backup_error(pages, tmp_path):
    orig, new = pages
    d = device(orig)
    blocker = tmp_path / "file"
    blocker.write_text("not a dir")
    with pytest.raises(safety.BackupError):
        send(d, orig, new, blocker)
    assert nothing_written(d)


def test_unreadable_manifest_is_backup_error(pages, tmp_path, monkeypatch):
    orig, new = pages
    d = device(orig)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(safety, "backup", lambda *a, **k: empty)
    with pytest.raises(safety.BackupError):
        send(d, orig, new, tmp_path)
    assert nothing_written(d)


def test_restore_also_fails(pages, tmp_path):
    orig, new = pages
    d = device(orig, fail_on={"rename": DeviceError("rename always fails")})
    with pytest.raises(safety.SendFailed) as ei:
        send(d, orig, new, tmp_path)
    e = ei.value
    assert e.restored is False
    assert "rename always fails" in str(e.cause)
    assert e.restore_error is not None
    assert e.backup_dir is not None and (e.backup_dir / "manifest.json").exists()
    assert str(e.backup_dir) in str(e)
    assert d.files[PAGE] == orig  # rename never happened
    assert not any(p.endswith(".rmdesk-tmp") for p in d.files)


def test_metadata_change_alone_does_not_abort(pages, tmp_path):
    orig, new = pages
    d = device(orig)
    d.write_bytes(META, b'{"lastOpenedPage": 1}')  # opened on the tablet: new mtime
    d.log.clear()
    res = send(d, orig, new, tmp_path)
    assert d.files[PAGE] == new
    assert (res.backup_dir / f"{NB}.metadata").read_bytes() == b'{"lastOpenedPage": 1}'
    assert not any(m == "stat_mtime" for m, _ in d.log)


def test_send_has_no_loaded_mtime_parameter():
    import inspect
    assert "loaded_mtime" not in inspect.signature(safety.send_to_page).parameters


def test_page_changed_since_load_aborts(pages, tmp_path):
    orig, new = pages
    d = device(orig)
    d.files[PAGE] = (FIX / "Lines_v2.rm").read_bytes()  # target page changed on the tablet
    with pytest.raises(safety.NotebookChanged):
        send(d, orig, new, tmp_path)
    assert nothing_written(d)


def test_backup_failure_writes_nothing(pages, tmp_path):
    orig, new = pages
    d = device(orig, fail_on={"read_bytes": FailRule(DeviceError("io"), match=PG2)})
    with pytest.raises(safety.BackupError):
        send(d, orig, new, tmp_path)
    assert nothing_written(d)
    assert not list(tmp_path.rglob("manifest.json"))


# --- undo ------------------------------------------------------------------

def test_undo_last_restores(pages, tmp_path):
    orig, new = pages
    d = device(orig)
    res = send(d, orig, new, tmp_path)
    assert d.files[PAGE] == new
    cfg = Config(last_send=res.last_send(name="nb"))
    safety.undo_last(d, cfg, root=tmp_path, sleep=lambda s: None,
                     now=lambda: NOW + timedelta(seconds=5))
    assert d.files[PAGE] == orig
    assert cfg.last_send is None
    # the state undo replaced was itself backed up first
    pre = tmp_path / NB / "20261103-120005"
    assert (pre / NB / f"{PG}.rm").read_bytes() == new


def sent(pages, tmp_path):
    orig, new = pages
    d = device(orig)
    res = send(d, orig, new, tmp_path / "b")
    last = res.last_send(name="nb")
    d.log.clear()
    return d, Config(last_send=dict(last)), last


def undo(d, cfg, root):
    return safety.undo_last(d, cfg, root=root, sleep=lambda s: None,
                            now=lambda: NOW + timedelta(seconds=5))


def test_undo_aborts_when_safety_backup_fails(pages, tmp_path):
    _, new = pages
    d, cfg, last = sent(pages, tmp_path)
    # the safety backup is page-scoped: it reads the undone page (PG), not PG2
    d.fail_on["read_bytes"] = FailRule(DeviceError("io"), match=PG)
    with pytest.raises(safety.RestoreError, match="nothing was changed"):
        undo(d, cfg, tmp_path / "b")
    assert nothing_written(d)
    assert cfg.last_send == last
    assert d.files[PAGE] == new


def test_undo_aborts_when_backup_dir_cannot_be_made(pages, tmp_path):
    _, new = pages
    d, cfg, last = sent(pages, tmp_path)
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(safety.RestoreError):
        undo(d, cfg, blocker)
    assert nothing_written(d)
    assert cfg.last_send == last


def test_undo_restore_fails_before_writing(pages, tmp_path):
    _, new = pages
    d, cfg, last = sent(pages, tmp_path)
    d.fail_on["rename"] = DeviceError("rename boom")
    with pytest.raises(safety.UndoFailed) as ei:
        undo(d, cfg, tmp_path / "b")
    e = ei.value
    assert e.files_written is False
    assert (e.safety_backup / "manifest.json").exists()
    assert str(e.safety_backup) in str(e)
    assert (e.safety_backup / NB / f"{PG}.rm").read_bytes() == new
    assert cfg.last_send == last
    assert d.xochitl_active  # started again


def test_undo_safety_backup_is_taken_after_stop(pages, tmp_path):
    # page-scoped undo: xochitl saving the sent page on stop makes undo refuse
    # (tests/test_undo_page.py); here it saves another page, which undo leaves alone
    orig, _ = pages
    d, cfg, last = sent(pages, tmp_path)
    saves_on_stop(d, PAGE2, b"drawn after the send")
    res = undo(d, cfg, tmp_path / "b")
    assert d.files[PAGE] == orig
    assert d.files[PAGE2] == b"drawn after the send"
    # the safety backup is page-scoped: the undone page as it was, never the other page
    _, new = pages
    assert (res.safety_backup / NB / f"{PG}.rm").read_bytes() == new
    assert not (res.safety_backup / NB / f"{PG2}.rm").exists()
    assert json.loads((res.safety_backup / "manifest.json").read_text())["scope"] == "page"
    assert cfg.last_send is None
    stop = index(d, ("run", "systemctl stop xochitl"))
    first_read = next(i for i, (m, _) in enumerate(d.log) if m == "read_bytes")
    assert stop < first_read
    assert writes_while_running(d) == []
    assert d.xochitl_active


def test_undo_stop_timeout_changes_nothing(pages, tmp_path):
    _, new = pages
    d, cfg, last = sent(pages, tmp_path)
    d.run_handler = lambda c: CommandResult(0, "active\n", "") if "is-active" in c else None
    with pytest.raises(safety.RestoreError, match="nothing was changed"):
        undo(d, cfg, tmp_path / "b")
    assert nothing_written(d)
    assert cfg.last_send == last
    assert d.files[PAGE] == new


def test_undo_never_writes_content_or_metadata(pages, tmp_path):
    # was test_undo_restore_fails_midway: a page-scoped undo writes one file only, so a
    # failure on .content can no longer happen midway (it is never written)
    orig, _ = pages
    d, cfg, last = sent(pages, tmp_path)
    d.fail_on["rename"] = FailRule(DeviceError("boom"), match=".content")
    d.fail_on["write_bytes"] = FailRule(DeviceError("boom"), match=".metadata")
    undo(d, cfg, tmp_path / "b")
    assert d.files[PAGE] == orig
    assert [a for m, a in d.log if m in WRITES] == [TMP, f"{TMP} -> {PAGE}"]
    assert cfg.last_send is None


def test_undo_start_timeout_after_writing(pages, tmp_path):
    orig, _ = pages
    d, cfg, last = sent(pages, tmp_path)
    d.run_handler = lambda c: CommandResult(3, "inactive\n", "") if "is-active" in c else None
    with pytest.raises(safety.UndoFailed) as ei:
        undo(d, cfg, tmp_path / "b")
    assert ei.value.files_written is True
    assert str(ei.value.safety_backup) in str(ei.value)
    assert d.files[PAGE] == orig
    assert cfg.last_send == last


def test_undo_without_last_send():
    with pytest.raises(safety.RestoreError):
        safety.undo_last(FakeDevice(), Config())


# --- prune -----------------------------------------------------------------

def mkbackup(root, name, nb=NB, manifest=True):
    p = root / nb / name
    p.mkdir(parents=True)
    if manifest:
        (p / "manifest.json").write_text("{}")
    return p


def stamp(dt):
    return dt.strftime("%Y%m%d-%H%M%S")


def test_prune_keeps_five_newest(tmp_path):
    old = [mkbackup(tmp_path, stamp(NOW - timedelta(days=40, hours=i))) for i in range(8)]
    deleted = safety.prune(tmp_path, now=NOW)
    assert sorted(deleted) == sorted(old[5:])
    assert all(p.exists() for p in old[:5])
    assert not any(p.exists() for p in old[5:])


def test_prune_recent_plus_old(tmp_path):
    recent = [mkbackup(tmp_path, stamp(NOW - timedelta(days=i + 1))) for i in range(2)]
    old = [mkbackup(tmp_path, stamp(NOW - timedelta(days=40 + i))) for i in range(6)]
    deleted = safety.prune(tmp_path, now=NOW)
    assert sorted(deleted) == sorted(old[3:])
    assert all(p.exists() for p in recent + old[:3])


def test_prune_is_per_notebook(tmp_path):
    a = [mkbackup(tmp_path, stamp(NOW - timedelta(days=40 + i)), nb="a") for i in range(5)]
    b = [mkbackup(tmp_path, stamp(NOW - timedelta(days=40 + i)), nb="b") for i in range(6)]
    assert safety.prune(tmp_path, now=NOW) == [b[5]]
    assert all(p.exists() for p in a)


def test_prune_never_deletes_young_incomplete(tmp_path):
    young = mkbackup(tmp_path, stamp(NOW - timedelta(minutes=10)), manifest=False)
    stale = mkbackup(tmp_path, stamp(NOW - timedelta(hours=2)), manifest=False)
    keep = [mkbackup(tmp_path, stamp(NOW - timedelta(days=1 + i))) for i in range(3)]
    deleted = safety.prune(tmp_path, now=NOW)
    assert young.exists()
    assert deleted == [stale]
    assert all(p.exists() for p in keep)


def test_prune_ignores_unknown_names_and_same_second_suffix(tmp_path):
    odd = mkbackup(tmp_path, "not-a-backup")
    base = stamp(NOW - timedelta(days=50))
    dirs = [mkbackup(tmp_path, base), mkbackup(tmp_path, base + "-2")]
    dirs += [mkbackup(tmp_path, stamp(NOW - timedelta(days=40, minutes=i))) for i in range(5)]
    deleted = safety.prune(tmp_path, now=NOW)
    assert odd.exists()
    assert sorted(deleted) == sorted(dirs[:2])


def test_prune_protect(tmp_path):
    old = [mkbackup(tmp_path, stamp(NOW - timedelta(days=40 + i))) for i in range(7)]
    deleted = safety.prune(tmp_path, now=NOW, protect=[old[6]])
    assert old[6].exists()
    # the protected dir does not count toward the 5 kept: old[0..4] kept, old[5] deleted
    assert deleted == [old[5]]
    assert all(p.exists() for p in old[:5])


def test_prune_missing_root(tmp_path):
    assert safety.prune(tmp_path / "nope", now=NOW) == []


def test_backup_root_default():
    assert safety.BACKUP_ROOT == Path.home() / ".local" / "share" / "rmdesk" / "backups"


# --- guard (after the post-stop backup, before any write) ----------------------

def test_send_guard_runs_after_backup_before_writes(pages, tmp_path):
    orig, new = pages
    d = device(orig)
    seen = []

    def guard(bdir):
        seen.append(((bdir / "manifest.json").exists(), device_writes(d)))

    send(d, orig, new, tmp_path / "b", guard=guard)
    # backup complete and xochitl stopped, nothing written yet
    assert seen == [(True, [("run", "systemctl stop xochitl")])]


def test_send_guard_failure_writes_nothing_and_restarts(pages, tmp_path):
    orig, new = pages
    d = device(orig)

    def guard(bdir):
        raise ValueError("not the test notebook")

    with pytest.raises(ValueError, match="not the test"):
        send(d, orig, new, tmp_path / "b", guard=guard)
    assert nothing_written(d)
    assert d.files[PAGE] == orig
    assert systemctl(d) == ["systemctl stop xochitl", "systemctl start xochitl"]


def test_undo_guard_runs_after_safety_backup_before_writes(pages, tmp_path):
    d, cfg, last = sent(pages, tmp_path)
    seen = []

    def guard(bdir):
        seen.append(((bdir / "manifest.json").exists(), bdir != Path(last["backup_dir"]),
                     device_writes(d)))

    safety.undo_last(d, cfg, root=tmp_path / "b", sleep=lambda s: None,
                     now=lambda: NOW + timedelta(seconds=5), guard=guard)
    assert seen == [(True, True, [("run", "systemctl stop xochitl")])]


def test_undo_guard_failure_writes_nothing_and_restarts(pages, tmp_path):
    _, new = pages
    d, cfg, last = sent(pages, tmp_path)

    def guard(bdir):
        raise ValueError("not the test notebook")

    with pytest.raises(ValueError, match="not the test"):
        safety.undo_last(d, cfg, root=tmp_path / "b", sleep=lambda s: None,
                         now=lambda: NOW + timedelta(seconds=5), guard=guard)
    assert nothing_written(d)
    assert cfg.last_send == last and d.files[PAGE] == new


def test_backup_dirs_and_files_are_private(pages, tmp_path):
    import os
    import stat
    orig, _ = pages
    root = tmp_path / "backups"
    bdir = safety.backup(device(orig), NB, root=root, now=lambda: NOW)
    for p in [root, *root.rglob("*")]:
        mode = stat.S_IMODE(os.stat(p).st_mode)
        assert mode == (0o700 if p.is_dir() else 0o600), (p, oct(mode))
    assert bdir.is_dir()



def test_stream_restarted_again_later_by_the_tablet():
    d = FakeDevice()
    safety.restart_xochitl(d, sleep=lambda s: None)
    runs = [a for m, a in d.log if m == "run"]
    later = runs[-1]
    assert later == safety.STREAM_LATER_CMD
    assert later.startswith("nohup sh -c 'sleep ") and later.endswith("&")  # returns at once
    assert ">/dev/null 2>&1" in later  # no output keeps the SSH channel open
