from datetime import datetime

import pytest

from rmdesk import webui
from rmdesk.device import CommandResult, DeviceError
from tests.fakes import FailRule, FakeDevice

CONF = webui.XOCHITL_CONF
BAK = CONF + ".rmdesk-bak"
ORIG = CONF + ".rmdesk-orig"
TMP = CONF + ".rmdesk-tmp"


def make_conf(value: bytes = b"false", eol: bytes = b"\n") -> bytes:
    """A 3.29-like xochitl.conf with the key on line 28."""
    lines = [b"[General]"]
    lines += [b"Key%02d=value %d" % (i, i) for i in range(2, 28)]
    lines.append(b"WebInterfaceEnabled=" + value)
    lines += [b"WifiNetwork=caf\xc3\xa9 \xff", b"", b"[wifinetworks]", b"x\\ssid=1"]
    return eol.join(lines) + eol


def make_dev(conf: bytes | None = None, **kw) -> FakeDevice:
    files = {} if conf is None else {CONF: conf}
    return FakeDevice(files, **kw)


def run(dev, enabled, tmp_path, check_web=lambda: True, **kw):
    webui.set_enabled(dev, enabled, backup_root=tmp_path, sleep=lambda s: None,
                      check_web=check_web, now=lambda: datetime(2026, 11, 3, 12, 0, 0), **kw)


def idx(dev, entry):
    return dev.log.index(entry)


# --- read_state ------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [(b"true", True), (b"false", False),
                                            (b"TRUE", True), (b"False", False)])
def test_read_state_values(value, expected):
    assert webui.read_state(make_dev(make_conf(value))) is expected


def test_read_state_crlf():
    assert webui.read_state(make_dev(make_conf(b"true", b"\r\n"))) is True


def test_read_state_absent_key():
    assert webui.read_state(make_dev(b"[General]\nOther=1\n")) is None


def test_read_state_duplicate_key_is_unknown():
    conf = make_conf(b"true") + b"WebInterfaceEnabled=false\n"
    assert webui.read_state(make_dev(conf)) is None


def test_read_state_ignores_key_not_at_line_start():
    assert webui.read_state(make_dev(b"[General]\nXWebInterfaceEnabled=true\n")) is None


def test_read_state_strange_value_is_unknown():
    assert webui.read_state(make_dev(b"WebInterfaceEnabled=maybe\n")) is None


# --- set_enabled -------------------------------------------------------------

def test_enable_happy_path(tmp_path):
    original = make_conf(b"false")
    dev = make_dev(original)
    run(dev, True, tmp_path)

    new = dev.files[CONF]
    old_lines, new_lines = original.split(b"\n"), new.split(b"\n")
    assert len(old_lines) == len(new_lines)
    diff = [i for i, (a, b) in enumerate(zip(old_lines, new_lines)) if a != b]
    assert diff == [27]  # line 28
    assert new_lines[27] == b"WebInterfaceEnabled=true"
    assert new == original.replace(b"WebInterfaceEnabled=false", b"WebInterfaceEnabled=true")

    # stop before read, start after the conf write
    stop = idx(dev, ("run", "systemctl stop xochitl"))
    read = idx(dev, ("read_bytes", CONF))
    write = idx(dev, ("rename", f"{TMP} -> {CONF}"))
    start = idx(dev, ("run", "systemctl start xochitl"))
    assert stop < read < write < start
    assert dev.xochitl_active
    assert ("write_bytes", CONF) not in dev.log  # only via tmp + rename

    # both backups hold the original bytes
    assert dev.files[BAK] == original
    assert ("rename", f"{BAK}.rmdesk-tmp -> {BAK}") in dev.log
    local = tmp_path / "xochitl-conf" / "20261103-120000.conf"
    assert local.read_bytes() == original
    assert TMP not in dev.files


def test_disable_crlf_preserves_bytes(tmp_path):
    original = make_conf(b"true", b"\r\n")
    dev = make_dev(original)
    checks = []
    run(dev, False, tmp_path, check_web=lambda: checks.append(1) or True)
    assert dev.files[CONF] == original.replace(b"Enabled=true\r\n", b"Enabled=false\r\n")
    assert checks == []  # no web check when disabling


def test_local_backup_does_not_overwrite_existing(tmp_path):
    original = make_conf(b"false")
    (tmp_path / "xochitl-conf").mkdir()
    (tmp_path / "xochitl-conf" / "20261103-120000.conf").write_bytes(b"older")
    dev = make_dev(original)
    run(dev, True, tmp_path)
    assert (tmp_path / "xochitl-conf" / "20261103-120000.conf").read_bytes() == b"older"
    assert (tmp_path / "xochitl-conf" / "20261103-120000-2.conf").read_bytes() == original


def test_duplicate_key_writes_nothing(tmp_path):
    original = make_conf(b"false") + b"WebInterfaceEnabled=false\n"
    dev = make_dev(original)
    with pytest.raises(webui.WebUiError, match="more than once"):
        run(dev, True, tmp_path)
    assert dev.files == {CONF: original}
    assert not any(m in ("write_bytes", "rename") for m, _ in dev.log)
    assert dev.xochitl_active
    assert not (tmp_path / "xochitl-conf").exists()


def test_missing_key_writes_nothing(tmp_path):
    original = b"[General]\nOther=1\n"
    dev = make_dev(original)
    with pytest.raises(webui.WebUiError, match="isn't in"):
        run(dev, True, tmp_path)
    assert dev.files == {CONF: original}
    assert dev.xochitl_active


def test_write_failure_restores_original(tmp_path):
    original = make_conf(b"false")
    dev = make_dev(original, fail_on={
        "rename": FailRule(DeviceError("disk full"), match=f"{TMP} -> {CONF}", times=1)})
    with pytest.raises(webui.WebUiError) as ei:
        run(dev, True, tmp_path)
    local = tmp_path / "xochitl-conf" / "20261103-120000.conf"
    assert str(local) in str(ei.value)
    assert ei.value.backup == local
    assert dev.files[CONF] == original
    assert dev.xochitl_active
    # the restore happened while xochitl was stopped
    restore = len(dev.log) - 1 - dev.log[::-1].index(("rename", f"{TMP} -> {CONF}"))
    assert restore < dev.log.index(("run", "systemctl start xochitl"))


def test_start_failure_restores_original(tmp_path):
    original = make_conf(b"false")
    starts = []

    def handler(cmd):
        if cmd == "systemctl start xochitl":
            starts.append(1)
            if len(starts) == 1:
                return CommandResult(1, "", "boom")
        return None
    dev = make_dev(original, run_handler=handler)
    with pytest.raises(webui.WebUiError) as ei:
        run(dev, True, tmp_path)
    assert "20261103-120000.conf" in str(ei.value)
    assert dev.files[CONF] == original
    assert len(starts) == 2 and dev.xochitl_active


def test_restore_failure_says_so(tmp_path):
    original = make_conf(b"false")
    dev = make_dev(original, fail_on={
        "rename": FailRule(DeviceError("gone"), match=f"{TMP} -> {CONF}")})
    with pytest.raises(webui.WebUiError) as ei:
        run(dev, True, tmp_path)
    msg = str(ei.value)
    assert "restoring" in msg and "20261103-120000.conf" in msg and BAK in msg
    assert dev.xochitl_active


def test_local_backup_failure_writes_nothing(tmp_path):
    original = make_conf(b"false")
    dev = make_dev(original)
    blocker = tmp_path / "file"
    blocker.write_text("not a dir")
    with pytest.raises(webui.WebUiError, match="nothing was changed"):
        run(dev, True, blocker)
    assert dev.files == {CONF: original}
    assert dev.xochitl_active


def test_stop_failure_writes_nothing(tmp_path):
    original = make_conf(b"false")

    def handler(cmd):
        if cmd == "systemctl stop xochitl":
            return CommandResult(1, "", "nope")
        return None
    dev = make_dev(original, run_handler=handler)
    with pytest.raises(webui.WebUiError, match="nothing was changed"):
        run(dev, True, tmp_path)
    assert dev.files == {CONF: original}
    assert ("read_bytes", CONF) not in dev.log


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_web_never_answers_keeps_setting(tmp_path):
    original = make_conf(b"false")
    dev = make_dev(original)
    clock = Clock()
    checks = []
    with pytest.raises(webui.WebUiError) as ei:
        webui.set_enabled(dev, True, backup_root=tmp_path, sleep=clock.sleep, clock=clock,
                          check_web=lambda: checks.append(clock.t) or False)
    assert str(ei.value) == ("The tablet restarted but the USB web interface didn't "
                             "answer. Is the USB cable connected?")
    assert dev.files[CONF] == original.replace(b"=false", b"=true")
    assert dev.xochitl_active
    assert 10 <= len(checks) <= 17


def test_web_wait_counts_probe_time(tmp_path):
    dev = make_dev(make_conf(b"false"))
    clock = Clock()
    checks = []

    def slow_probe():  # each probe takes 3 s (UsbWeb.available's timeout)
        checks.append(clock.t)
        clock.t += 3
        return False
    start = clock.t
    with pytest.raises(webui.WebUiError):
        webui.set_enabled(dev, True, backup_root=tmp_path, sleep=clock.sleep, clock=clock,
                          check_web=slow_probe)
    # 4 s per round (3 s probe + 1 s sleep): the deadline is ~15 s, not 15 sleeps
    assert len(checks) <= 5
    assert clock.t - start <= 15 + 4 + 1  # + the xochitl stop/start polls (no waits here)


def test_web_answers_after_a_while(tmp_path):
    dev = make_dev(make_conf(b"false"))
    answers = iter([False, False, False, True])
    run(dev, True, tmp_path, check_web=lambda: next(answers))
    assert webui.read_state(dev) is True


def test_already_set_still_restarts_cleanly(tmp_path):
    original = make_conf(b"true")
    dev = make_dev(original)
    run(dev, True, tmp_path)
    assert dev.files[CONF] == original
    assert dev.xochitl_active


def test_read_state_missing_file_is_unknown():
    assert webui.read_state(make_dev(None)) is None


def test_read_state_read_error_raises():
    dev = make_dev(make_conf(), fail_on={"read_bytes": DeviceError("link down")})
    with pytest.raises(DeviceError):
        webui.read_state(dev)


def test_orig_written_once_and_bak_tracks_last_change(tmp_path):
    original = make_conf(b"false")
    dev = make_dev(original)
    run(dev, True, tmp_path)
    assert dev.files[ORIG] == original
    assert dev.files[BAK] == original
    after_first = dev.files[CONF]
    run(dev, False, tmp_path)
    assert dev.files[ORIG] == original  # the pre-rmdesk file is kept
    assert dev.files[BAK] == after_first  # "before the last change"
    assert dev.files[CONF] == original
    assert sum(1 for m, a in dev.log if m == "write_bytes" and a.startswith(ORIG)) == 1


# --- no plaintext secrets in local copies ------------------------------------

def test_local_copy_blanks_passwords_and_is_private(tmp_path):
    original = (b"[General]\nDeveloperPassword=s3cret\r\nPasscode=1234\n"
                b"WebInterfaceEnabled=false\nXPasscode=keep\nOther=1\n")
    dev = make_dev(original)
    run(dev, True, tmp_path)
    local = tmp_path / "xochitl-conf" / "20261103-120000.conf"
    data = local.read_bytes()
    assert b"s3cret" not in data and b"1234" not in data
    assert data == (b"[General]\nDeveloperPassword=<removed by rmdesk>\r\n"
                    b"Passcode=<removed by rmdesk>\nWebInterfaceEnabled=false\n"
                    b"XPasscode=keep\nOther=1\n")
    assert local.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "xochitl-conf").stat().st_mode & 0o777 == 0o700
    # the tablet-side backups keep the full original (they never leave the tablet)
    assert dev.files[BAK] == original


def test_redact_keeps_every_other_byte():
    conf = make_conf(b"true", b"\r\n")
    assert webui.redact(conf) == conf
