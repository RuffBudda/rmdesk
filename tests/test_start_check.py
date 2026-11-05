"""xochitl must still be active a few seconds after it started; a failed start after a
pre-write failure is never silent."""
from datetime import datetime
from pathlib import Path

import pytest

from rmdesk import safety
from rmdesk.device import CommandResult, DeviceError
from rmdesk.library.model import XOCHITL
from rmdesk.ui.compose import messages as msg
from tests.fakes import FailRule, FakeDevice
from tests.test_safety import NB, PAGE, PG, device, nothing_written, pages, send  # noqa: F401

NOW = datetime(2026, 11, 3, 12, 0, 0)


def dies_after_start(d, after=1):
    """xochitl reports `active` for the first `after` is-active polls after a start, then
    `inactive` (it crashed right after starting)."""
    state = {"since_start": None}

    def h(cmd):
        if cmd == "systemctl start xochitl":
            state["since_start"] = 0
            return None
        if cmd == "systemctl stop xochitl":
            state["since_start"] = None
            return None
        if "is-active" in cmd and state["since_start"] is not None:
            state["since_start"] += 1
            if state["since_start"] > after:
                d.xochitl_active = False
                return CommandResult(3, "inactive\n", "")
        return None
    d.run_handler = h


def test_start_rechecks_active_after_a_bounded_sleep():
    d = FakeDevice()
    sleeps = []
    safety.start_xochitl(d, sleep=sleeps.append)
    runs = [a for m, a in d.log if m == "run"]
    assert runs[:3] == ["systemctl start xochitl", "systemctl is-active xochitl",
                        "systemctl is-active xochitl"]
    assert sleeps == [safety.START_RECHECK]
    assert 2 <= safety.START_RECHECK <= 5


def test_start_fails_when_xochitl_dies_right_after_starting():
    d = FakeDevice()
    dies_after_start(d)
    with pytest.raises(safety.RestartTimeout, match="stopped again"):
        safety.start_xochitl(d, sleep=lambda s: None)
    assert "systemctl try-restart goMarkableStream" not in [a for _, a in d.log]


def test_send_restores_when_xochitl_dies_right_after_starting(pages, tmp_path):
    orig, new = pages
    d = device(orig)
    dies_after_start(d, after=1)
    with pytest.raises(safety.SendFailed) as ei:
        send(d, orig, new, tmp_path)
    assert ei.value.restored
    assert d.files[PAGE] == orig
    assert ei.value.restart_error is not None  # every start in this test dies


def test_pre_write_failure_with_failed_restart_is_annotated(pages, tmp_path):
    orig, new = pages
    d = device(orig, fail_on={"read_bytes": FailRule(DeviceError("SSH session not active"))})
    d.fail_on["run"] = FailRule(DeviceError("SSH session not active"), match="start xochitl")
    with pytest.raises(safety.BackupError) as ei:
        send(d, orig, new, tmp_path)
    assert safety.start_error_of(ei.value) is not None
    text, _ = msg.describe_send_error(ei.value)
    assert msg.SCREEN_RESTART in text


def test_pre_write_failure_with_working_restart_has_no_hint(pages, tmp_path):
    orig, new = pages
    d = device(orig, fail_on={"read_bytes": FailRule(DeviceError("io"))})
    with pytest.raises(safety.BackupError) as ei:
        send(d, orig, new, tmp_path)
    assert safety.start_error_of(ei.value) is None
    assert nothing_written(d)
    assert msg.SCREEN_RESTART not in msg.describe_send_error(ei.value)[0]


@pytest.mark.parametrize("exc", [safety.Cancelled("no"), safety.NotebookChanged("x")])
def test_any_pre_write_refusal_names_a_failed_restart(exc):
    exc.xochitl_start_error = DeviceError("down")
    assert msg.SCREEN_RESTART in msg.describe_send_error(exc)[0]
