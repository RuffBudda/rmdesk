import pytest

from rmdesk.device import DeviceError
from tests.fakes import FakeDevice


def test_fake_device_roundtrip():
    d = FakeDevice()
    d.write_bytes("/a/b", b"x")
    assert d.read_bytes("/a/b") == b"x"
    assert d.listdir("/a") == ["b"]


def test_fake_device_injected_failure():
    d = FakeDevice(fail_on={"rename": DeviceError("boom")})
    with pytest.raises(DeviceError):
        d.rename("/a", "/b")
