"""Undo is page-scoped: only the page rmdesk wrote, and only while it still holds rmdesk's bytes."""
import hashlib
import json
from datetime import datetime, timedelta

import pytest

from rmdesk import safety
from rmdesk.config import Config
from rmdesk.device import DeviceError
from rmdesk.library.model import XOCHITL
from rmdesk.ui.compose import messages as msg
from tests.fakes import FailRule
from tests.test_safety import (CONTENT, META, NB, PAGE, PAGE2, PG, device, nothing_written,
                               pages, saves_on_stop, send, systemctl)  # noqa: F401

NOW = datetime(2026, 11, 3, 12, 0, 0)


def sha(b):
    return hashlib.sha256(b).hexdigest()


def sent(pages, tmp_path):
    orig, new = pages
    d = device(orig)
    res = send(d, orig, new, tmp_path / "b")
    d.log.clear()
    last = res.last_send(name="nb")
    return d, Config(last_send=dict(last)), last


def undo(d, cfg, root, **kw):
    return safety.undo_last(d, cfg, root=root, sleep=lambda s: None,
                            now=lambda: NOW + timedelta(seconds=5), **kw)


def test_send_result_records_page_and_sha(pages, tmp_path):
    orig, new = pages
    res = send(device(orig), orig, new, tmp_path)
    assert res.notebook_id == NB and res.page_id == PG and res.page_sha256 == sha(new)
    assert res.last_send(name="nb") == {"notebook_id": NB, "name": "nb", "page_id": PG,
                                        "page_sha256": sha(new),
                                        "backup_dir": str(res.backup_dir)}


def test_undo_restores_only_the_page(pages, tmp_path):
    orig, new = pages
    d, cfg, _ = sent(pages, tmp_path)
    # after the send the tablet changed other files of the notebook
    d.files[CONTENT] = b'{"pages": ["new page 3"]}'
    d.files[META] = b'{"visibleName": "renamed"}'
    d.files[PAGE2] = b"a week of new notes"
    d.files[f"{XOCHITL}/{NB}/pg3.rm"] = b"brand new page 3"
    res = undo(d, cfg, tmp_path / "b")
    assert d.files[PAGE] == orig
    assert d.files[CONTENT] == b'{"pages": ["new page 3"]}'
    assert d.files[META] == b'{"visibleName": "renamed"}'
    assert d.files[PAGE2] == b"a week of new notes"
    assert d.files[f"{XOCHITL}/{NB}/pg3.rm"] == b"brand new page 3"
    assert res.written == [PAGE]
    written = [a for m, a in d.log if m == "rename"]
    assert written == [f"{PAGE}.rmdesk-tmp -> {PAGE}"]
    assert cfg.last_send is None
    assert d.xochitl_active
    assert (res.safety_backup / NB / f"{PG}.rm").read_bytes() == new


def test_undo_refuses_when_page_changed_after_send(pages, tmp_path):
    d, cfg, last = sent(pages, tmp_path)
    d.files[PAGE] = b"written on after the send"
    with pytest.raises(safety.PageChangedSinceSend) as ei:
        undo(d, cfg, tmp_path / "b")
    assert str(ei.value) == ("The page changed on the tablet after the last send, "
                             "so rmdesk can't undo it safely.")
    assert nothing_written(d)
    assert d.files[PAGE] == b"written on after the send"
    assert cfg.last_send == last
    text, _ = msg.describe_undo_error(ei.value)
    assert text.startswith("The page changed on the tablet after the last send, "
                           "so rmdesk can't undo it safely.")
    assert msg.NOTHING_CHANGED in text


def test_undo_refuses_when_xochitl_saves_the_page_on_stop(pages, tmp_path):
    d, cfg, last = sent(pages, tmp_path)
    saves_on_stop(d, PAGE, b"drawn after the send")
    with pytest.raises(safety.PageChangedSinceSend):
        undo(d, cfg, tmp_path / "b")
    assert nothing_written(d)
    assert d.files[PAGE] == b"drawn after the send"
    assert cfg.last_send == last


def test_undo_refuses_when_page_removed(pages, tmp_path):
    d, cfg, last = sent(pages, tmp_path)
    del d.files[PAGE]
    with pytest.raises(safety.PageChangedSinceSend):
        undo(d, cfg, tmp_path / "b")
    assert nothing_written(d) and PAGE not in d.files
    assert cfg.last_send == last


@pytest.mark.parametrize("drop", ["page_sha256", "page_id"])
def test_old_last_send_without_sha_refuses_before_touching_tablet(pages, tmp_path, drop):
    d, cfg, last = sent(pages, tmp_path)
    del cfg.last_send[drop]
    with pytest.raises(safety.UndoUnavailable):
        undo(d, cfg, tmp_path / "b")
    assert d.log == []
    assert cfg.last_send is not None
    text, _ = msg.describe_undo_error(safety.UndoUnavailable("x"))
    assert msg.NOTHING_CHANGED in text


def test_undo_refuses_when_backup_is_of_another_notebook(pages, tmp_path):
    d, cfg, last = sent(pages, tmp_path)
    cfg.last_send["notebook_id"] = "99999999-8888-7777-6666-555555555555"
    with pytest.raises(safety.RestoreError, match="another notebook"):
        undo(d, cfg, tmp_path / "b")
    assert d.log == []


def test_undo_refuses_when_backup_lacks_the_page(pages, tmp_path):
    d, cfg, last = sent(pages, tmp_path)
    cfg.last_send["page_id"] = "nope"
    with pytest.raises(safety.RestoreError, match="no copy of page nope"):
        undo(d, cfg, tmp_path / "b")
    assert d.log == []


def test_undo_page_write_failure_names_safety_backup(pages, tmp_path):
    _, new = pages
    d, cfg, last = sent(pages, tmp_path)
    d.fail_on["rename"] = DeviceError("rename boom")
    with pytest.raises(safety.UndoFailed) as ei:
        undo(d, cfg, tmp_path / "b")
    assert ei.value.files_written is False
    assert d.files[PAGE] == new and d.xochitl_active
    assert cfg.last_send == last


def test_undo_guard_failure_after_page_check_writes_nothing(pages, tmp_path):
    d, cfg, last = sent(pages, tmp_path)

    def guard(bdir):
        raise ValueError("not the test notebook")
    with pytest.raises(ValueError):
        undo(d, cfg, tmp_path / "b", guard=guard)
    assert nothing_written(d)
    assert systemctl(d) == ["systemctl stop xochitl", "systemctl start xochitl"]


def test_undo_failed_restart_on_refusal_is_reported(pages, tmp_path):
    d, cfg, last = sent(pages, tmp_path)
    d.files[PAGE] = b"changed"
    d.fail_on["run"] = FailRule(DeviceError("down"), match="start xochitl")
    with pytest.raises(safety.PageChangedSinceSend) as ei:
        undo(d, cfg, tmp_path / "b")
    assert msg.SCREEN_RESTART in msg.describe_undo_error(ei.value)[0]


def test_undo_confirm_text_names_only_the_page():
    text = msg.UNDO_CONFIRM.format(name="“nb”")
    assert "page" in text and "other pages" in text
    assert "Anything written in it" not in text


def test_readme_describes_page_scoped_undo():
    from pathlib import Path
    readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    line = next(l for l in readme.splitlines() if "**Undo**" in l)
    assert "page" in line.split("**Undo**", 1)[1]
    assert "changed" in line


def test_repro_undo_scope_other_pages_survive(tmp_path):
    """The reviewer's scenario: page 1 written and page 3 added after a send to page 2."""
    from rmdesk.notebook import page
    from tests.test_safety_live import FIX, NB as LNB, PG1, PG2, _lines
    from tests.test_safety_live import device as live_device
    orig = (FIX / "Normal_AB.rm").read_bytes()
    d = live_device(orig)

    def build(op):
        p = page.load(op.data)
        p.append_to_rmdesk_layer(_lines(2))
        return p.to_bytes(), 2
    res, opened = safety.send_to_open_page(d, build, lambda op: True, root=tmp_path,
                                           sleep=lambda s: None)
    cfg = Config(last_send=res.last_send(name=opened.name))
    d.files[f"{XOCHITL}/{LNB}/{PG1}.rm"] = b"page 1 with a week of new notes"
    c = json.loads(d.files[f"{XOCHITL}/{LNB}.content"])
    c["cPages"]["pages"].append({"id": "pg3"})
    d.files[f"{XOCHITL}/{LNB}.content"] = json.dumps(c).encode()
    d.files[f"{XOCHITL}/{LNB}/pg3.rm"] = b"brand new page 3"
    safety.undo_last(d, cfg, root=tmp_path, sleep=lambda s: None)
    assert d.files[f"{XOCHITL}/{LNB}/{PG1}.rm"] == b"page 1 with a week of new notes"
    assert [p["id"] for p in json.loads(d.files[f"{XOCHITL}/{LNB}.content"])["cPages"]["pages"]] \
        == [PG1, PG2, "pg3"]
    assert d.files[f"{XOCHITL}/{LNB}/{PG2}.rm"] == orig

