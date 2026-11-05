"""SshDevice: per-instance serialisation (D1) and key/agent handling (D3). No network."""
import threading
import time

import paramiko
import pytest

from rmdesk import device as device_mod
from rmdesk.device import DeviceError, SshDevice


class _Overlap:
    """Records whether two fake paramiko calls were ever inside at the same time."""

    def __init__(self):
        self._mu = threading.Lock()
        self.active = 0
        self.max_active = 0

    def __call__(self, delay=0.003):
        with self._mu:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(delay)
        with self._mu:
            self.active -= 1


class _File:
    def __init__(self, guard):
        self.guard = guard

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.guard()

    def read(self):
        self.guard()
        return b"data"

    def write(self, data):
        self.guard()


class _Stat:
    st_mtime = 12.0


class _Sftp:
    def __init__(self, guard):
        self.guard = guard

    def open(self, path, mode):
        self.guard()
        return _File(self.guard)

    def posix_rename(self, src, dst):
        self.guard()

    def rename(self, src, dst):
        self.guard()

    def listdir(self, path):
        self.guard()
        return ["a"]

    def stat(self, path):
        self.guard()
        return _Stat()

    def close(self):
        self.guard()


class _Chan:
    """Channel whose output arrives over several polls, so run() spans many calls."""

    def __init__(self, guard):
        self.guard = guard
        self.polls = 3

    def exec_command(self, cmd):
        self.guard()

    def recv_ready(self):
        self.guard(0.001)
        self.polls -= 1
        return self.polls == 1

    def recv(self, n):
        self.guard()
        return b"out"

    def recv_stderr_ready(self):
        return False

    def exit_status_ready(self):
        self.guard(0.001)
        return self.polls <= 0

    def recv_exit_status(self):
        return 0

    def close(self):
        self.guard()


class _Transport:
    def __init__(self, guard):
        self.guard = guard

    def open_session(self, timeout=None):
        self.guard()
        return _Chan(self.guard)


class _Client:
    def __init__(self, guard):
        self.guard = guard

    def get_transport(self):
        return _Transport(self.guard)

    def close(self):
        self.guard()


def _device(guard):
    dev = SshDevice.__new__(SshDevice)
    dev._client = _Client(guard)
    dev._sftp = _Sftp(guard)
    dev._lock = threading.RLock()
    return dev


def test_public_methods_never_overlap_across_threads():
    guard = _Overlap()
    dev = _device(guard)
    ops = [
        lambda: dev.run("df -h /home", timeout=5),
        lambda: dev.read_bytes("/a"),
        lambda: dev.write_bytes("/b", b"x"),
        lambda: dev.rename("/b", "/c"),
        lambda: dev.listdir("/"),
        lambda: dev.exists("/a"),
        lambda: dev.stat_mtime("/a"),
    ]
    errors = []
    start = threading.Barrier(8)

    def worker(i):
        try:
            start.wait()
            for k in range(6):
                ops[(i + k) % len(ops)]()
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors
    assert guard.max_active == 1, f"{guard.max_active} paramiko calls overlapped"
    dev.close()


def test_lock_is_reentrant_and_released_after_errors():
    guard = _Overlap()
    dev = _device(guard)

    def boom(path):
        raise OSError("gone")
    dev._sftp.listdir = boom
    with pytest.raises(DeviceError):
        dev.listdir("/x")
    # the lock was released: another thread can still use the device
    out = []
    t = threading.Thread(target=lambda: out.append(dev.exists("/a")))
    t.start()
    t.join(5)
    assert out == [True]


# -- D3: ssh-agent and passphrase-protected keys --

class _RecordingClient:
    instances = []

    def __init__(self):
        self.connect_kwargs = None
        _RecordingClient.instances.append(self)

    def load_host_keys(self, path):
        pass

    def set_missing_host_key_policy(self, policy):
        pass

    def connect(self, host, **kw):
        self.connect_kwargs = kw

    def open_sftp(self):
        return _TimeoutSftp()

    def close(self):
        pass


@pytest.fixture
def recording(monkeypatch, tmp_path):
    _RecordingClient.instances = []
    monkeypatch.setattr(device_mod.paramiko, "SSHClient", _RecordingClient)
    monkeypatch.setattr(device_mod, "KNOWN_HOSTS", tmp_path / "known_hosts")
    return _RecordingClient.instances


def test_connect_uses_agent_and_no_key_discovery(recording):
    SshDevice("10.11.99.1", None)
    kw = recording[0].connect_kwargs
    assert kw["allow_agent"] is True
    assert kw["look_for_keys"] is False


def test_unencrypted_key_is_passed(recording, monkeypatch):
    monkeypatch.setattr(device_mod.paramiko.Ed25519Key, "__init__",
                        lambda self, filename=None, password=None: None)
    SshDevice("10.11.99.1", "pw", key_filename="/home/x/.ssh/id_ed25519")
    kw = recording[0].connect_kwargs
    assert kw["key_filename"] == "/home/x/.ssh/id_ed25519"
    assert kw["password"] == "pw"


def test_passphrase_protected_key_is_skipped_and_password_still_tried(recording, monkeypatch):
    def locked(*a, **k):
        raise paramiko.PasswordRequiredException("Private key file is encrypted")
    for cls in (paramiko.RSAKey, paramiko.ECDSAKey, paramiko.Ed25519Key):
        monkeypatch.setattr(cls, "__init__", locked)
    dev = SshDevice("10.11.99.1", "tablet-pw", key_filename="/home/x/.ssh/id_ed25519")
    kw = recording[0].connect_kwargs
    assert kw["key_filename"] is None
    assert kw["password"] == "tablet-pw"
    assert kw["allow_agent"] is True
    assert dev is not None


def test_real_passphrase_protected_key_is_skipped(recording, tmp_path):
    import shutil
    import subprocess
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen not available")
    key = tmp_path / "id_ed25519"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "secret", "-f", str(key)],
                   check=True)
    SshDevice("10.11.99.1", "tablet-pw", key_filename=str(key))
    assert recording[0].connect_kwargs["key_filename"] is None
    plain = tmp_path / "plain"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(plain)],
                   check=True)
    SshDevice("10.11.99.1", "tablet-pw", key_filename=str(plain))
    assert recording[1].connect_kwargs["key_filename"] == str(plain)


# -- R43: a hung SFTP call must time out instead of holding the lock forever --

class _TimeoutChannel:
    def __init__(self):
        self.timeout = None

    def settimeout(self, t):
        self.timeout = t


class _TimeoutSftp:
    def __init__(self):
        self.chan = _TimeoutChannel()

    def get_channel(self):
        return self.chan

    def stat(self, path):
        import socket
        raise socket.timeout("timed out")

    def listdir(self, path):
        return ["ok"]

    def close(self):
        pass


def test_sftp_channel_gets_a_timeout(recording, monkeypatch):
    sftp = _TimeoutSftp()
    monkeypatch.setattr(_RecordingClient, "open_sftp", lambda self: sftp)
    SshDevice("10.11.99.1", None)
    assert sftp.chan.timeout == 30


def test_sftp_timeout_is_device_error_and_releases_lock():
    dev = SshDevice.__new__(SshDevice)
    dev._lock = threading.RLock()
    dev._client = _Client(_Overlap())
    dev._sftp = _TimeoutSftp()
    with pytest.raises(DeviceError):
        dev.stat_mtime("/a")
    with pytest.raises(DeviceError):
        dev.exists("/a")
    out = []
    t = threading.Thread(target=lambda: out.append(dev.listdir("/")))
    t.start()
    t.join(5)
    assert out == [["ok"]]
