"""Compose sends and undo back up only the page they write (plus .content and .metadata)."""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from rmdesk import firmware, safety
from rmdesk.compose import calibration
from rmdesk.config import Config
from rmdesk.device import DeviceError
from rmdesk.library.model import XOCHITL
from tests.fakes import FailRule, FakeDevice
from tests.test_safety_live import FIX, _nb, appended

NB = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"
PG1 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PG2 = "ffffffff-0000-1111-2222-333333333333"
PG3 = "33333333-3333-3333-3333-333333333333"
META = f"{XOCHITL}/{NB}.metadata"
CONTENT = f"{XOCHITL}/{NB}.content"
P1, P2, P3 = (f"{XOCHITL}/{NB}/{p}.rm" for p in (PG1, PG2, PG3))
SIDECARS = (f"{XOCHITL}/{NB}.pagedata", f"{XOCHITL}/{NB}.local",
            f"{XOCHITL}/{NB}/{PG2}-metadata.json", f"{XOCHITL}/{NB}.thumbnails/{PG2}.png")
NOW = datetime(2026, 11, 3, 12, 0, 0)
WRITES = ("write_bytes", "rename")
PAGE_FILES = {f"{NB}.metadata", f"{NB}.content", f"{NB}/{PG2}.rm"}


@pytest.fixture
def orig():
    return (FIX / "Normal_AB.rm").read_bytes()


def device(orig, name="Physics notes"):
    """A 3-page notebook open on page 2, with sidecars and another notebook."""
    files = {}
    _nb(files, NB, name, "2000", [PG1, PG2, PG3], 1,
        {PG1: b"first page", PG2: orig, PG3: b"third page"})
    _nb(files, OTHER, "Older", "1000", ["o1"], 0, {"o1": b"older page"})
    for p in SIDECARS:
        files[p] = b"sidecar"
    return FakeDevice(files=files)


def send(d, root, **kw):
    args = dict(root=root, sleep=lambda s: None, now=lambda: NOW)
    args.update(kw)
    return safety.send_to_open_page(d, lambda op: (appended(op.data), 2), lambda op: True,
                                    **args)


def undo(d, cfg, root, **kw):
    return safety.undo_last(d, cfg, root=root, sleep=lambda s: None,
                            now=lambda: NOW + timedelta(seconds=5), **kw)


def manifest(bdir):
    return json.loads((Path(bdir) / "manifest.json").read_text())


def reads(d, start=0):
    return [a for m, a in d.log[start:] if m == "read_bytes"]


def not_touched(d, paths, start=0):
    """No call of any kind named one of `paths` (and no `find` listing of the notebook)."""
    for m, a in d.log[start:]:
        if m == "run" and a.startswith("find "):
            return False
        if m != "run" and any(p in a for p in paths):
            return False
    return True


def without_scope(bdir):
    """Make a full backup look like one written before page-scoped backups existed."""
    m = manifest(bdir)
    m.pop("scope", None)
    m.pop("page_id", None)
    (Path(bdir) / "manifest.json").write_text(json.dumps(m))


# --- send ------------------------------------------------------------------

def test_send_backs_up_only_the_page_content_and_metadata(orig, tmp_path):
    d = device(orig)
    res, op = send(d, tmp_path)
    assert op.page_id == PG2
    man = manifest(res.backup_dir)
    assert set(man["files"]) == PAGE_FILES
    assert man["scope"] == "page" and man["page_id"] == PG2 and man["notebook_id"] == NB
    assert (res.backup_dir / NB / f"{PG2}.rm").read_bytes() == orig
    # the other pages and the sidecars are never read, listed or even looked at
    assert not_touched(d, [P1, P3, *SIDECARS])
    assert P1 not in reads(d) and P3 not in reads(d)
    assert d.files[P2] == appended(orig)
    assert d.files[P1] == b"first page" and d.files[P3] == b"third page"
    assert res.page_sha256 == safety._sha(appended(orig))


def test_page_backup_is_taken_after_the_stop(orig, tmp_path):
    d = device(orig)
    send(d, tmp_path)
    stop = d.log.index(("run", "systemctl stop xochitl"))
    first_write = next(i for i, (m, _) in enumerate(d.log) if m == "write_bytes")
    # find_opened_page reads the page before the backup does: the backup's read is the last
    page_reads = [i for i, e in enumerate(d.log) if e == ("read_bytes", P2)]
    assert len(page_reads) == 2 and stop < page_reads[0] < page_reads[1] < first_write


def test_send_restore_on_failure_puts_page_back_and_leaves_other_pages(orig, tmp_path):
    d = device(orig)
    d.fail_on = {"rename": FailRule(DeviceError("rename boom"), times=1)}
    before = dict(d.files)
    with pytest.raises(safety.SendFailed) as ei:
        send(d, tmp_path)
    assert ei.value.restored is True
    assert manifest(ei.value.backup_dir)["scope"] == "page"
    assert d.files == before  # page byte-identical, nothing added or removed
    written = {a for m, a in d.log if m == "write_bytes"}
    assert written <= {P2 + safety.TMP_SUFFIX, META + safety.TMP_SUFFIX,
                       CONTENT + safety.TMP_SUFFIX}
    assert not_touched(d, [P1, P3, *SIDECARS])
    assert d.xochitl_active


def test_send_to_page_still_takes_a_full_backup(orig, tmp_path):
    d = device(orig)
    res = safety.send_to_page(d, NB, PG2, appended(orig), orig, 2, None, root=tmp_path,
                              sleep=lambda s: None, now=lambda: NOW)
    man = manifest(res.backup_dir)
    assert man["scope"] == "notebook" and "page_id" not in man
    assert {f"{NB}/{PG1}.rm", f"{NB}/{PG3}.rm", f"{NB}.pagedata"} <= set(man["files"])


def test_page_backup_without_the_page_file(orig, tmp_path):
    d = device(orig)
    del d.files[P2]
    bdir = safety.backup_page(d, NB, PG2, root=tmp_path, now=lambda: NOW)
    man = manifest(bdir)
    assert set(man["files"]) == {f"{NB}.metadata", f"{NB}.content"}
    assert man["scope"] == "page" and man["page_id"] == PG2


def test_page_backup_refuses_odd_page_ids(orig, tmp_path):
    d = device(orig)
    with pytest.raises(safety.BackupError):
        safety.backup_page(d, NB, "../x", root=tmp_path, now=lambda: NOW)
    assert not list(tmp_path.rglob("manifest.json"))


def test_page_backup_failure_leaves_no_manifest(orig, tmp_path):
    d = device(orig)
    d.fail_on = {"read_bytes": FailRule(DeviceError("gone"), match=P2)}
    with pytest.raises(safety.BackupError):
        safety.backup_page(d, NB, PG2, root=tmp_path, now=lambda: NOW)
    assert not list(tmp_path.rglob("manifest.json"))


# --- restore ---------------------------------------------------------------

def test_restore_of_page_backup_writes_only_its_files_and_lists_no_extras(orig, tmp_path):
    d = device(orig)
    bdir = safety.backup_page(d, NB, PG2, root=tmp_path, now=lambda: NOW)
    d.files[P2] = b"garbage"
    d.files[P1] = b"page 1 with new notes"
    d.files[f"{XOCHITL}/{NB}/pg4.rm"] = b"brand new page 4"
    d.log.clear()
    res = safety.restore(d, bdir, sleep=lambda s: None)
    assert d.files[P2] == orig
    assert d.files[P1] == b"page 1 with new notes"
    assert d.files[P3] == b"third page"
    assert d.files[f"{XOCHITL}/{NB}/pg4.rm"] == b"brand new page 4"
    assert sorted(res.written) == sorted([P2, CONTENT, META])
    assert res.extra == []  # other pages are not "extra": the backup never had them
    assert not_touched(d, [P1, P3, *SIDECARS])


def test_restore_of_full_backup_still_lists_extras(orig, tmp_path):
    d = device(orig)
    bdir = safety.backup(d, NB, root=tmp_path, now=lambda: NOW)
    without_scope(bdir)  # as written by an older rmdesk
    d.files[f"{XOCHITL}/{NB}/pg4.rm"] = b"new"
    res = safety.restore(d, bdir, sleep=lambda s: None)
    assert res.extra == [f"{XOCHITL}/{NB}/pg4.rm"]


# --- undo ------------------------------------------------------------------

def test_undo_from_page_backup_with_page_scoped_safety_backup(orig, tmp_path):
    d = device(orig)
    res, op = send(d, tmp_path)
    cfg = Config(last_send=res.last_send(name=op.name))
    d.files[P1] = b"page 1 with new notes"
    d.log.clear()
    out = undo(d, cfg, tmp_path)
    assert d.files[P2] == orig
    assert d.files[P1] == b"page 1 with new notes" and d.files[P3] == b"third page"
    assert out.written == [P2]
    sb = manifest(out.safety_backup)
    assert sb["scope"] == "page" and sb["page_id"] == PG2 and set(sb["files"]) == PAGE_FILES
    assert (out.safety_backup / NB / f"{PG2}.rm").read_bytes() == appended(orig)
    assert not_touched(d, [P1, P3, *SIDECARS])
    assert cfg.last_send is None and d.xochitl_active


def test_undo_from_an_older_full_backup(orig, tmp_path):
    d = device(orig)
    full = safety.backup(d, NB, root=tmp_path, now=lambda: NOW)
    without_scope(full)
    new = appended(orig)
    d.files[P2] = new
    cfg = Config(last_send={"notebook_id": NB, "name": "Physics notes", "page_id": PG2,
                            "page_sha256": safety._sha(new), "backup_dir": str(full)})
    out = undo(d, cfg, tmp_path)
    assert d.files[P2] == orig and out.written == [P2]
    assert manifest(out.safety_backup)["scope"] == "page"
    assert cfg.last_send is None


def test_undo_refuses_when_page_removed_with_page_backup(orig, tmp_path):
    d = device(orig)
    res, op = send(d, tmp_path)
    cfg = Config(last_send=res.last_send(name=op.name))
    del d.files[P2]
    with pytest.raises(safety.PageChangedSinceSend):
        undo(d, cfg, tmp_path)
    assert P2 not in d.files and cfg.last_send is not None


# --- guards ------------------------------------------------------------------

@pytest.mark.parametrize("make", [lambda: firmware.acceptance_guard("9.9"),
                                  lambda: calibration.guard])
def test_send_guards_pass_on_the_page_backup(orig, tmp_path, make):
    d = device(orig, name="rmdesk test")
    send(d, tmp_path, guard=make())
    assert d.files[P2] == appended(orig)


@pytest.mark.parametrize("make,exc", [
    (lambda: firmware.acceptance_guard("9.9"), firmware.NotTestNotebook),
    (lambda: calibration.guard, calibration.NotCalibrationNotebook)])
def test_send_guards_refuse_the_wrong_notebook_name(orig, tmp_path, make, exc):
    d = device(orig, name="Physics notes")
    with pytest.raises(exc):
        send(d, tmp_path, guard=make())
    assert not any(m in WRITES for m, _ in d.log) and d.xochitl_active
    assert d.files[P2] == orig


def test_undo_guard_passes_and_refuses_on_page_scoped_safety_backup(orig, tmp_path):
    for name, ok in (("rmdesk test", True), ("Physics notes", False)):
        d = device(orig, name=name)
        res, op = send(d, tmp_path / name)
        cfg = Config(last_send=res.last_send(name=op.name))
        guard = firmware.undo_acceptance_guard(NB, "9.9")
        if ok:
            undo(d, cfg, tmp_path / name, guard=guard)
            assert d.files[P2] == orig
        else:
            d.log.clear()
            with pytest.raises(firmware.NotTestNotebook):
                undo(d, cfg, tmp_path / name, guard=guard)
            assert not any(m in WRITES for m, _ in d.log)
            assert d.files[P2] == appended(orig) and cfg.last_send is not None


# --- prune -----------------------------------------------------------------

def _mk(root, when, scope):
    p = root / NB / when.strftime("%Y%m%d-%H%M%S")
    p.mkdir(parents=True)
    m = {"notebook_id": NB, "files": {}}
    if scope == "page":
        m.update(scope="page", page_id=PG2)
    elif scope == "notebook":
        m["scope"] = "notebook"
    (p / "manifest.json").write_text(json.dumps(m))
    return p


def test_prune_handles_both_kinds(tmp_path):
    old = NOW - timedelta(days=40)
    full = [_mk(tmp_path, old - timedelta(hours=i), "notebook" if i % 2 else None)
            for i in range(7)]
    paged = [_mk(tmp_path, old + timedelta(minutes=1 + i), "page") for i in range(7)]
    deleted = safety.prune(tmp_path, now=NOW)
    # page backups are pruned like any other, and never push the newest full backups out
    assert sorted(deleted) == sorted(full[5:] + paged[:2])
    assert all(p.exists() for p in full[:5] + paged[2:])


def test_prune_young_page_backups_kept(tmp_path):
    paged = [_mk(tmp_path, NOW - timedelta(days=1, minutes=i), "page") for i in range(8)]
    assert safety.prune(tmp_path, now=NOW) == []
    assert all(p.exists() for p in paged)
