import pytest

from rmdesk.device import DeviceError, _collect


class FakeChannel:
    def __init__(self, out=b"", err=b"", code=0, finishes=True):
        self.out, self.err, self.code, self.finishes = out, err, code, finishes
        self.closed = False

    def recv_ready(self): return bool(self.out)
    def recv_stderr_ready(self): return bool(self.err)
    def exit_status_ready(self): return self.finishes and not self.out and not self.err

    def recv(self, n):
        d, self.out = self.out[:n], self.out[n:]
        return d

    def recv_stderr(self, n):
        d, self.err = self.err[:n], self.err[n:]
        return d

    def recv_exit_status(self): return self.code
    def close(self): self.closed = True


def test_collect_reads_both_streams():
    ch = FakeChannel(out=b"o" * 200000, err=b"e" * 200000, code=3)
    r = _collect(ch, 5)
    assert (r.code, len(r.out), len(r.err)) == (3, 200000, 200000)
    assert ch.closed


def test_collect_times_out_and_closes():
    ch = FakeChannel(finishes=False)
    with pytest.raises(DeviceError):
        _collect(ch, 0.1)
    assert ch.closed


class _Transport:
    def __init__(self, chan): self.chan = chan
    def open_session(self, timeout=None): return self.chan


class _Client:
    def __init__(self, chan): self.t = _Transport(chan)
    def get_transport(self): return self.t


def _device(chan):
    from rmdesk.device import SshDevice
    import threading
    dev = SshDevice.__new__(SshDevice)
    dev._lock = threading.RLock()
    dev._client = _Client(chan)
    chan.exec_command = lambda cmd: setattr(chan, "cmd", cmd)
    return dev


def test_ssh_device_run_uses_collect():
    ch = FakeChannel(out=b"hi", err=b"warn", code=2)
    r = _device(ch).run("cmd", timeout=5)
    assert (r.code, r.out, r.err) == (2, "hi", "warn")
    assert ch.cmd == "cmd" and ch.closed


def test_ssh_device_run_timeout():
    ch = FakeChannel(finishes=False)
    with pytest.raises(DeviceError):
        _device(ch).run("cmd", timeout=0.1)
    assert ch.closed
