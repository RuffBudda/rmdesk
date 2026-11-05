import pytest
import shlex

from rmdesk import firmware
from rmdesk.device import CommandResult, DeviceError
from rmdesk.firmware import FirmwareInfo
from tests.fakes import FailRule, FakeDevice

XO = "/home/root/.local/share/remarkable/xochitl"


def test_detect_firmware_os_release():
    d = FakeDevice(files={
        "/etc/os-release": b'ID=codex\nIMG_VERSION="3.24.0.149"\n',
        "/sys/devices/soc0/serial_number": b"RM2-123\n",
    })
    info = firmware.detect(d)
    assert info.version == "3.24.0.149"
    assert info.serial == "RM2-123"
    assert info.rm_format is None


def test_detect_version_fallback():
    d = FakeDevice(files={"/etc/os-release": b"ID=x\n", "/etc/version": b"20240101\n",
                          "/sys/devices/soc0/serial_number": b"S"})
    assert firmware.detect(d).version == "20240101"


def test_rm_format_6():
    def handler(cmd):
        if cmd.startswith("cat "):
            return None
        assert cmd == f"find {XO} -name '*.rm' -print -quit"
        return CommandResult(0, f"{XO}/a/0.rm\n", "")
    d = FakeDevice(files={
        "/etc/os-release": b'IMG_VERSION="1"', "/sys/devices/soc0/serial_number": b"S",
        f"{XO}/a/0.rm": b"reMarkable .lines file, version=6          " + b"junk",
    }, run_handler=handler)
    assert firmware.detect(d).rm_format == 6


def test_rm_format_other_and_none():
    base = {"/etc/os-release": b'IMG_VERSION="1"', "/sys/devices/soc0/serial_number": b"S"}
    d = FakeDevice(files={**base, f"{XO}/a.rm": b"reMarkable .lines file, version=5          "},
                   run_handler=lambda c: None if c.startswith("cat ") else CommandResult(0, f"{XO}/a.rm\n", ""))
    assert firmware.detect(d).rm_format == 5
    d = FakeDevice(files=base, run_handler=lambda c: None if c.startswith("cat ") else CommandResult(0, "", ""))
    assert firmware.detect(d).rm_format is None


def test_compose_not_allowed_for_untested():
    assert firmware.compose_allowed(FirmwareInfo("9.9.9", "x", 6)) is False


def test_compose_allowed_when_tested(monkeypatch):
    monkeypatch.setattr(firmware, "TESTED_VERSIONS", {"1.2.3"})
    assert firmware.compose_allowed(FirmwareInfo("1.2.3", "x", 6))
    assert not firmware.compose_allowed(FirmwareInfo("1.2.3", "x", 5))
    assert not firmware.compose_allowed(FirmwareInfo("1.2.3", "x", None))


def test_disabled_msg():
    assert firmware.COMPOSE_DISABLED_MSG == \
        "This firmware isn't tested yet. Writing to notebooks is disabled."


DT = "/proc/device-tree/serial-number"
SOC = "/sys/devices/soc0/serial_number"
CPUINFO = (b"processor\t: 0\nmodel name\t: ARMv7 Processor rev 10 (v7l)\n\n"
           b"Hardware\t: Freescale i.MX7 Dual (Device Tree)\nRevision\t: 0000\n"
           b"Serial\t\t: 3800000000001\n")


def cat_device(files, extra=None, raise_on=()):
    """FakeDevice whose files are only reachable through `cat` over run().

    read_bytes of any serial source fails, so read_serial must use run()."""
    def handler(cmd):
        argv = shlex.split(cmd)
        if argv[:1] == ["cat"]:
            path = argv[-1]
            if path in raise_on:
                raise DeviceError("channel closed")
            if path in files:
                return CommandResult(0, files[path].decode("utf-8", "replace"), "")
            return CommandResult(1, "", f"cat: {path}: No such file or directory")
        return extra(cmd) if extra else None
    return FakeDevice(files=files, run_handler=handler,
                      fail_on={"read_bytes": FailRule(DeviceError("use cat"), match="serial")})


def _cats(d):
    return [a for m, a in d.log if m == "run" and a.startswith("cat ")]


def test_read_serial_device_tree_strips_nul():
    d = cat_device({DT: b"3800000000001\x00"})
    assert firmware.read_serial(d) == "3800000000001"
    assert _cats(d) == [f"cat {DT}"]


def test_read_serial_device_tree_preferred_over_soc0():
    d = cat_device({DT: b"DT\x00\n", SOC: b"SOC\n"})
    assert firmware.read_serial(d) == "DT"


def test_read_serial_soc0_fallback():
    d = cat_device({SOC: b"RM2-123\n"})
    assert firmware.read_serial(d) == "RM2-123"
    assert _cats(d) == [f"cat {DT}", f"cat {SOC}"]


def test_read_serial_cpuinfo_fallback():
    d = cat_device({DT: b"\x00\x00", "/proc/cpuinfo": CPUINFO})
    assert firmware.read_serial(d) == "3800000000001"
    assert _cats(d)[-1] == "cat /proc/cpuinfo"


def test_read_serial_device_error_moves_to_next_source():
    d = cat_device({SOC: b"RM2-123\n"}, raise_on=(DT,))
    assert firmware.read_serial(d) == "RM2-123"


def test_read_serial_none_found():
    d = cat_device({"/proc/cpuinfo": b"processor\t: 0\n"})
    assert firmware.read_serial(d) == ""


def test_detect_uses_device_tree_serial():
    d = cat_device({"/etc/os-release": b'IMG_VERSION="3.29.1.0"\n', DT: b"3800000000001\x00"},
                   extra=lambda c: CommandResult(0, "", ""))
    assert firmware.detect(d).serial == "3800000000001"


def test_compose_mode(monkeypatch):
    assert firmware.ACCEPTANCE_NOTEBOOK == "rmdesk test"
    assert firmware.compose_mode(FirmwareInfo("9.9.9", "x", 6)) == "test-only"
    assert firmware.compose_mode(FirmwareInfo("9.9.9", "x", 5)) == "off"
    assert firmware.compose_mode(FirmwareInfo("9.9.9", "x", None)) == "off"
    assert firmware.compose_mode(None) == "off"
    monkeypatch.setattr(firmware, "TESTED_VERSIONS", {"1.2.3"})
    assert firmware.compose_mode(FirmwareInfo("1.2.3", "x", 6)) == "full"
    assert firmware.compose_mode(FirmwareInfo("1.2.3", "x", 5)) == "off"


def test_acceptance_guard_fails_closed(tmp_path):
    from rmdesk.notebook.opened import OpenedPage
    opened = OpenedPage("nb", "rmdesk test", "p", 0, 1, "portrait", b"")
    g = firmware.acceptance_guard("1.0")  # send_to_open_page's guard(bdir, opened)
    u = firmware.undo_acceptance_guard("nb", "1.0")  # undo_last's guard(bdir)
    for check in (lambda: g(tmp_path, opened), lambda: u(tmp_path)):
        with pytest.raises(firmware.NotTestNotebook):
            check()  # no metadata file
    f = tmp_path / "nb.metadata"
    for bad in ("not json", "[]", '{"type": "CollectionType", "visibleName": "rmdesk test"}',
                '{"type": "DocumentType", "visibleName": "Other"}'):
        f.write_text(bad)
        for check in (lambda: g(tmp_path, opened), lambda: u(tmp_path)):
            with pytest.raises(firmware.NotTestNotebook, match="Testing mode: firmware 1.0"):
                check()
    f.write_text('{"type": "DocumentType", "visibleName": " rmdesk test "}')
    g(tmp_path, opened)
    u(tmp_path)


def test_acceptance_guard_reads_the_identified_notebook(tmp_path):
    # the guard checks the notebook identified after the stop (opened.notebook_id), from the
    # backup taken while stopped, not a name the caller remembered
    from rmdesk.notebook.opened import OpenedPage
    (tmp_path / "a.metadata").write_text('{"type": "DocumentType", "visibleName": "rmdesk test"}')
    (tmp_path / "b.metadata").write_text('{"type": "DocumentType", "visibleName": "Diary"}')
    g = firmware.acceptance_guard("1.0")
    g(tmp_path, OpenedPage("a", "x", "p", 0, 1, "portrait", b""))
    with pytest.raises(firmware.NotTestNotebook):
        g(tmp_path, OpenedPage("b", "rmdesk test", "p", 0, 1, "portrait", b""))


def test_3_29_1_0_is_tested():
    assert "3.29.1.0" in firmware.TESTED_VERSIONS
    assert firmware.compose_mode(FirmwareInfo("3.29.1.0", "x", 6)) == "full"
    assert firmware.compose_mode(FirmwareInfo("3.30.0.0", "x", 6)) == "test-only"
