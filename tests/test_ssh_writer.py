"""SshDevice.open_writer: a long-lived exec channel for input events. No network."""
import threading

import pytest

from rmdesk.device import DeviceError, SshDevice


class WChan:
    def __init__(self):
        self.cmd = None
        self.sent = bytearray()
        self.timeout = None
        self.eof = False
        self.closed = False
        self.exited = False
        self.err = b""

    def exec_command(self, cmd):
        self.cmd = cmd

    def settimeout(self, t):
        self.timeout = t

    def sendall(self, data):
        if self.closed:
            raise OSError("Socket is closed")
        self.sent += data

    def exit_status_ready(self):
        return self.exited

    def recv_exit_status(self):
        return 1

    def recv_stderr_ready(self):
        return bool(self.err)

    def recv_stderr(self, n):
        e, self.err = self.err, b""
        return e

    def shutdown_write(self):
        self.eof = True

    def close(self):
        self.closed = True


class T:
    def __init__(self, chan):
        self.chan = chan

    def open_session(self, timeout=None):
        return self.chan


class C:
    def __init__(self, chan):
        self.t = T(chan)

    def get_transport(self):
        return self.t


def device(chan):
    dev = SshDevice.__new__(SshDevice)
    dev._client = C(chan)
    dev._lock = threading.RLock()
    return dev


def test_open_writer_execs_and_writes_without_the_device_lock():
    ch = WChan()
    dev = device(ch)
    w = dev.open_writer("cat > /dev/input/event2")
    assert ch.cmd == "cat > /dev/input/event2" and ch.timeout is not None
    held, release = threading.Event(), threading.Event()

    def hog():
        with dev._lock:
            held.set()
            release.wait(5)
    t = threading.Thread(target=hog)
    t.start()
    held.wait(5)
    done = threading.Event()
    threading.Thread(target=lambda: (w.write(b"x" * 16), done.set())).start()
    assert done.wait(2)  # a busy SFTP call never stalls input
    release.set()
    t.join(5)
    assert bytes(ch.sent) == b"x" * 16
    w.close()
    assert ch.eof and ch.closed


def test_writer_check_reports_exit_with_stderr():
    ch = WChan()
    w = device(ch).open_writer("cat > /dev/input/event3")
    w.check()  # alive: fine
    ch.exited, ch.err = True, b"cat: write error: No such device\n"
    with pytest.raises(DeviceError, match="No such device"):
        w.check()


def test_writer_write_failure_is_device_error():
    ch = WChan()
    w = device(ch).open_writer("cat > /dev/input/event2")
    ch.closed = True
    with pytest.raises(DeviceError):
        w.write(b"x" * 16)


def test_open_writer_failure_is_device_error():
    dev = SshDevice.__new__(SshDevice)
    dev._lock = threading.RLock()

    class Dead:
        def get_transport(self):
            return None
    dev._client = Dead()
    with pytest.raises(DeviceError):
        dev.open_writer("cat > /dev/input/event2")


# -- open_reader: the touch events coming back ----------------------------------------------

class RChan(WChan):
    def __init__(self, chunks):
        super().__init__()
        self.chunks = list(chunks)

    def recv(self, n):
        import socket
        if self.closed:
            raise OSError("Socket is closed")
        if not self.chunks:
            return b""
        c = self.chunks.pop(0)
        if c is None:
            raise socket.timeout()
        return c[:n]


def test_open_reader_execs_and_reads_with_a_poll_timeout():
    from rmdesk.device import READER_POLL
    ch = RChan([b"abc", None, b"def"])
    r = device(ch).open_reader("cat /dev/input/event2")
    assert ch.cmd == "cat /dev/input/event2" and ch.timeout == READER_POLL
    assert r.read() == b"abc"
    assert r.read() is None  # nothing yet
    assert r.read() == b"def"
    assert r.read() == b""  # the command ended
    ch.err = b"cat: can't open\n"
    assert r.error() == "cat: can't open"
    r.close()
    assert ch.closed


def test_reader_failure_is_device_error():
    ch = RChan([b"x"])
    r = device(ch).open_reader("cat /dev/input/event2")
    ch.closed = True
    with pytest.raises(DeviceError):
        r.read()


def test_open_reader_failure_is_device_error():
    dev = SshDevice.__new__(SshDevice)
    dev._lock = threading.RLock()

    class Dead:
        def get_transport(self):
            return None
    dev._client = Dead()
    with pytest.raises(DeviceError):
        dev.open_reader("cat /dev/input/event2")
