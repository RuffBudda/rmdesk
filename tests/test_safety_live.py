"""safety.send_to_open_page: one stop/start cycle around identify, confirm, build and write."""
import json
from datetime import datetime
from pathlib import Path

import pytest
from rmscene import scene_items as si

from rmdesk import safety
from rmdesk.device import CommandResult, DeviceError
from rmdesk.library.model import XOCHITL
from rmdesk.notebook import page
from rmdesk.notebook.opened import ZOOM_MSG, NotEditable, OpenedPage
from rmdesk.notebook.page import VerifyError
from tests.fakes import FailRule, FakeDevice

FIX = Path(__file__).parent / "fixtures" / "rm"
NB = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"
PG1 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PG2 = "ffffffff-0000-1111-2222-333333333333"
PAGE = f"{XOCHITL}/{NB}/{PG2}.rm"  # the open page (lastOpenedPage = 1)
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
def orig():
    return (FIX / "Normal_AB.rm").read_bytes()


def appended(data: bytes, n=2) -> bytes:
    p = page.load(data)
    p.append_layer("rmdesk", _lines(n))
    return p.to_bytes()


def _nb(files, uid, name, opened_at, pages, page_index, page_bytes, **content_extra):
    meta = {"visibleName": name, "type": "DocumentType", "parent": "",
            "lastOpened": opened_at, "lastOpenedPage": page_index, "lastModified": "1"}
    content = {"fileType": "notebook", "orientation": "portrait", "pageCount": len(pages),
               "cPages": {"pages": [{"id": p} for p in pages]}, **content_extra}
    files[f"{XOCHITL}/{uid}.metadata"] = json.dumps(meta).encode()
    files[f"{XOCHITL}/{uid}.content"] = json.dumps(content).encode()
    for p in pages:
        files[f"{XOCHITL}/{uid}/{p}.rm"] = page_bytes[p]


def device(orig, **content_extra):
    files = {}
    _nb(files, NB, "Physics notes", "2000", [PG1, PG2], 1,
        {PG1: b"first page", PG2: orig}, **content_extra)
    _nb(files, OTHER, "Older", "1000", ["o1"], 0, {"o1": b"older page"})
    return FakeDevice(files=files)


def systemctl(d):
    return [a for m, a in d.log if m == "run" and a.startswith("systemctl")
            and "is-active" not in a and "try-restart" not in a]


def nothing_written(d):
    return (not any(m in WRITES for m, _ in d.log) and d.xochitl_active
            and systemctl(d) in ([], ["systemctl stop xochitl", "systemctl start xochitl"]))


def writes_while_running(d):
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


def send(d, root, build=None, confirm=None, **kw):
    if build is None:
        def build(op):
            return appended(op.data), 2
    if confirm is None:
        def confirm(op):
            return True
    args = dict(root=root, sleep=lambda s: None, now=lambda: NOW)
    args.update(kw)
    return safety.send_to_open_page(d, build, confirm, **args)


# --- order -----------------------------------------------------------------

def test_stop_before_identify_and_start_after_write(orig, tmp_path):
    d = device(orig)
    events = []

    def confirm(op):
        events.append(("confirm", d.xochitl_active, len(d.log)))
        return True

    def build(op):
        events.append(("build", d.xochitl_active, len(d.log)))
        return appended(op.data), 2

    res, op = send(d, tmp_path, build=build, confirm=confirm)
    new = appended(orig)
    assert d.files[PAGE] == new and TMP not in d.files
    assert isinstance(res, safety.SendResult) and (res.backup_dir / "manifest.json").exists()
    assert op.notebook_id == NB and op.page_id == PG2
    stop = index(d, ("run", "systemctl stop xochitl"))
    identify = index(d, ("listdir", XOCHITL))
    first_write = next(i for i, (m, _) in enumerate(d.log) if m == "write_bytes")
    rename = index(d, ("rename", f"{TMP} -> {PAGE}"))
    start = index(d, ("run", "systemctl start xochitl"))
    # confirm and build run while stopped, after identify, before the backup
    assert [(e[0], e[1]) for e in events] == [("confirm", False), ("build", False)]
    assert stop < identify < events[0][2] <= events[1][2]
    backup_reads = [i for i, (m, a) in enumerate(d.log)
                    if m == "read_bytes" and a == f"{XOCHITL}/{NB}.metadata" and i > events[1][2]]
    assert backup_reads and backup_reads[0] < first_write  # backup after build, before write
    assert first_write < rename < start
    assert d.log[first_write] == ("write_bytes", TMP)
    assert systemctl(d) == ["systemctl stop xochitl", "systemctl start xochitl"]
    assert writes_while_running(d) == []
    assert d.xochitl_active


def test_stop_failure_starts_and_identifies_nothing(orig, tmp_path):
    d = device(orig)
    d.run_handler = lambda c: CommandResult(0, "active\n", "") if "is-active" in c else None
    called = []
    with pytest.raises(safety.StopTimeout):
        send(d, tmp_path, confirm=lambda op: called.append(op) or True)
    assert called == []
    assert ("listdir", XOCHITL) not in d.log
    assert not any(m in WRITES for m, _ in d.log)
    assert systemctl(d) == ["systemctl stop xochitl", "systemctl start xochitl"]


# --- refusal / confirm -------------------------------------------------------

def test_confirm_no_writes_nothing_and_starts(orig, tmp_path):
    d = device(orig)
    built = []
    with pytest.raises(safety.Cancelled):
        send(d, tmp_path, confirm=lambda op: False, build=lambda op: built.append(op))
    assert built == []
    assert nothing_written(d)
    assert systemctl(d) == ["systemctl stop xochitl", "systemctl start xochitl"]
    assert d.files[PAGE] == orig
    assert not tmp_path.exists() or not any(tmp_path.rglob("manifest.json"))


def test_not_editable_starts_and_raises(orig, tmp_path):
    d = device(orig, zoomMode="custom", customZoomScale=2)
    asked = []
    with pytest.raises(NotEditable) as ei:
        send(d, tmp_path, confirm=lambda op: asked.append(op) or True)
    assert ei.value.reason == ZOOM_MSG
    assert asked == []
    assert nothing_written(d)
    assert systemctl(d) == ["systemctl stop xochitl", "systemctl start xochitl"]


def test_confirm_receives_opened_page(orig, tmp_path):
    d = device(orig)
    seen = []
    send(d, tmp_path, confirm=lambda op: seen.append(op) or True)
    assert len(seen) == 1 and isinstance(seen[0], OpenedPage)
    op = seen[0]
    assert (op.notebook_id, op.page_id, op.page_index, op.page_count) == (NB, PG2, 1, 2)
    assert op.data == orig


def test_confirm_names_notebook_and_page(orig, tmp_path):
    # Review focus 1: the most recently opened notebook is named to the user
    # (the UI formats the text) so a wrong guess can be refused.
    d = device(orig)
    seen = []
    with pytest.raises(safety.Cancelled):
        send(d, tmp_path, confirm=lambda op: seen.append((op.name, op.page_index)) or False)
    assert seen == [("Physics notes", 1)]
    assert nothing_written(d)


@pytest.mark.parametrize("exc", [RuntimeError("dialog broke"), KeyboardInterrupt()])
def test_confirm_exception_starts_and_reraises(orig, tmp_path, exc):
    d = device(orig)

    def confirm(op):
        raise exc

    with pytest.raises(type(exc)):
        send(d, tmp_path, confirm=confirm)
    assert nothing_written(d)
    assert systemctl(d) == ["systemctl stop xochitl", "systemctl start xochitl"]


# --- build / verify ----------------------------------------------------------

@pytest.mark.parametrize("exc", [ValueError("bad item"), KeyboardInterrupt()])
def test_build_failure_starts(orig, tmp_path, exc):
    d = device(orig)

    def build(op):
        raise exc

    with pytest.raises(type(exc)):
        send(d, tmp_path, build=build)
    assert nothing_written(d)
    assert systemctl(d) == ["systemctl stop xochitl", "systemctl start xochitl"]
    assert d.files[PAGE] == orig


def test_verify_failure_writes_nothing(orig, tmp_path):
    d = device(orig)
    with pytest.raises(VerifyError):
        send(d, tmp_path, build=lambda op: (appended(op.data)[:-7], 2))
    assert nothing_written(d)
    assert d.files[PAGE] == orig


def test_wrong_line_count_writes_nothing(orig, tmp_path):
    d = device(orig)
    with pytest.raises(VerifyError):
        send(d, tmp_path, build=lambda op: (appended(op.data), 3))
    assert nothing_written(d)


def test_page_changed_after_identify_aborts(orig, tmp_path):
    d = device(orig)

    def confirm(op):
        d.files[PAGE] = b"changed under us"
        return True

    with pytest.raises(safety.NotebookChanged):
        send(d, tmp_path, confirm=confirm)
    assert nothing_written(d)


# --- write failures -----------------------------------------------------------

def test_rename_failure_restores(orig, tmp_path):
    d = device(orig)
    d.fail_on = {"rename": FailRule(DeviceError("rename boom"), times=1)}
    with pytest.raises(safety.SendFailed) as ei:
        send(d, tmp_path)
    assert ei.value.restored is True
    assert isinstance(ei.value.cause, DeviceError)
    assert (ei.value.backup_dir / "manifest.json").exists()
    assert d.files[PAGE] == orig
    assert TMP not in d.files
    assert writes_while_running(d) == []
    assert d.xochitl_active


def test_start_timeout_after_write_restores(orig, tmp_path):
    d = device(orig)
    d.run_handler = lambda c: CommandResult(3, "activating\n", "") if "is-active" in c else None
    with pytest.raises(safety.SendFailed) as ei:
        send(d, tmp_path)
    assert ei.value.restored is True
    assert isinstance(ei.value.cause, safety.RestartTimeout)
    assert isinstance(ei.value.restart_error, safety.RestartTimeout)
    assert d.files[PAGE] == orig
    assert writes_while_running(d) == []


# --- guard -------------------------------------------------------------------

def test_guard_called_with_backup_and_opened_page_before_write(orig, tmp_path):
    d = device(orig)
    seen = []

    def guard(bdir, op):
        seen.append(((bdir / "manifest.json").exists(), op.notebook_id, op.name,
                     any(m in WRITES for m, _ in d.log), d.xochitl_active))

    res, _ = send(d, tmp_path / "b", guard=guard)
    assert seen == [(True, NB, "Physics notes", False, False)]
    assert res.backup_dir.parent == tmp_path / "b" / NB


def test_guard_failure_writes_nothing_and_starts(orig, tmp_path):
    d = device(orig)

    def guard(bdir, op):
        raise ValueError("not the test notebook")

    with pytest.raises(ValueError, match="not the test"):
        send(d, tmp_path / "b", guard=guard)
    assert nothing_written(d)
    assert systemctl(d) == ["systemctl stop xochitl", "systemctl start xochitl"]
    assert d.files[PAGE] == orig


def test_open_page_send_succeeds_when_stream_refresh_fails(orig, tmp_path):
    d = device(orig)

    def handler(cmd):
        if "try-restart" in cmd:
            raise DeviceError("nope")
        return None
    d.run_handler = handler
    send(d, tmp_path)
    assert d.xochitl_active
    assert "systemctl try-restart goMarkableStream" in [a for m, a in d.log if m == "run"]
