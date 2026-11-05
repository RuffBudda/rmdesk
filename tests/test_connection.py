import pytest

from rmdesk.config import Config
from rmdesk.connection import Connection, State, install_ssh_key
from rmdesk.device import DeviceError, HostKeyChanged
from tests.fakes import FakeDevice

FILES = {"/etc/os-release": b'IMG_VERSION="3.24.0.149"', "/sys/devices/soc0/serial_number": b"SER1"}


class Secrets:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.sets = []

    def get(self, device_id):
        return self.data.get(device_id)

    def set(self, device_id, password):
        self.sets.append((device_id, password))
        self.data[device_id] = password


def make(fail=(), errors=None, cfg=None, secrets=None, sleeps=None):
    calls = []

    def factory(host, password):
        calls.append((host, password))
        if host in fail:
            raise (errors or {}).get(host, DeviceError("down"))
        return FakeDevice(files=FILES)

    sleeps = sleeps if sleeps is not None else []
    c = Connection(cfg or Config(wifi_ip="192.168.1.5"), factory=factory,
                   secrets=secrets or Secrets(), sleep=sleeps.append)
    return c, calls


def test_connect_prefers_usb():
    c, calls = make()
    assert c.connect() == State.USB
    assert calls[0][0] == "10.11.99.1"
    assert c.transport == "usb" and c.device is not None


def test_connect_falls_back_to_wifi():
    c, calls = make(fail={"10.11.99.1"})
    assert c.connect() == State.WIFI
    assert [h for h, _ in calls] == ["10.11.99.1", "192.168.1.5"]
    assert c.transport == "wifi"


def test_connect_all_fail_offline():
    c, _ = make(fail={"10.11.99.1", "192.168.1.5"})
    assert c.connect() == State.OFFLINE
    assert c.device is None and c.transport is None
    with pytest.raises(DeviceError):
        c.ensure()


def test_no_wifi_ip_only_tries_usb():
    c, calls = make(fail={"10.11.99.1"}, cfg=Config())
    assert c.connect() == State.OFFLINE
    assert len(calls) == 1


def test_state_callbacks_fire():
    c, _ = make()
    seen = []
    c.on_state.append(seen.append)
    c.connect()
    assert seen == [State.CONNECTING, State.USB]


def test_host_key_changed_no_fallback():
    err = HostKeyChanged("changed")
    c, calls = make(fail={"10.11.99.1"}, errors={"10.11.99.1": err})
    assert c.connect() == State.OFFLINE
    assert c.last_error is err
    assert [h for h, _ in calls] == ["10.11.99.1"]


def test_host_key_changed_on_wifi():
    err = HostKeyChanged("changed")
    c, _ = make(fail={"10.11.99.1", "192.168.1.5"}, errors={"192.168.1.5": err})
    assert c.connect() == State.OFFLINE
    assert c.last_error is err


def test_other_error_recorded_and_next_host_tried():
    usb_err = DeviceError("usb down")
    c, calls = make(fail={"10.11.99.1"}, errors={"10.11.99.1": usb_err})
    assert c.connect() == State.WIFI
    assert c.host_errors == {"10.11.99.1": usb_err}
    assert [h for h, _ in calls] == ["10.11.99.1", "192.168.1.5"]
    assert c.last_error is None


def test_all_fail_records_last_error():
    w = DeviceError("wifi down")
    c, _ = make(fail={"10.11.99.1", "192.168.1.5"}, errors={"192.168.1.5": w})
    c.connect()
    assert c.last_error is w and set(c.host_errors) == {"10.11.99.1", "192.168.1.5"}


def test_connection_lost_then_ensure_reconnects():
    sleeps = []
    c, calls = make(sleeps=sleeps)
    seen = []
    c.connect()
    c.on_state.append(seen.append)
    dev = c.device
    err = DeviceError("cable pulled")
    c.connection_lost(err)
    assert c.state == State.OFFLINE and c.device is None and c.transport is None
    assert c.last_error is err and seen == [State.OFFLINE]
    assert ("close", "") in dev.log
    new = c.ensure()
    assert new is not dev and c.state == State.USB
    assert sleeps == [1] and len(calls) == 2


def test_connection_lost_swallows_close_error():
    c, _ = make()
    c.connect()
    c.device.fail_on["close"] = OSError("boom")
    c.connection_lost(DeviceError("x"))
    assert c.state == State.OFFLINE


def test_persist_error_does_not_break_connect():
    c, _ = make()
    def bad(cfg):
        raise OSError("disk full")
    c.persist = bad
    assert c.connect() == State.USB
    assert c.state == State.USB and c.device is not None
    assert isinstance(c.last_error, OSError)


def test_bad_listener_does_not_stop_others():
    c, _ = make()
    seen = []
    def bad(s):
        raise RuntimeError("listener")
    c.on_state.extend([bad, seen.append])
    assert c.connect() == State.USB
    assert seen == [State.CONNECTING, State.USB]
    assert c.state == State.USB and isinstance(c.last_error, RuntimeError)


def test_first_connect_stores_password_by_serial():
    s = Secrets()
    persisted = []
    c, calls = make(secrets=s)
    c.persist = persisted.append
    c.set_password("pw")
    c.connect()
    assert calls[0] == ("10.11.99.1", "pw")
    assert s.sets == [("SER1", "pw")]
    assert c.cfg.serial == "SER1"
    assert c.info.version == "3.24.0.149"
    assert persisted == [c.cfg]


def test_later_connect_uses_stored_password():
    s = Secrets({"SER1": "stored"})
    c, calls = make(cfg=Config(serial="SER1"), secrets=s)
    c.connect()
    assert calls[0] == ("10.11.99.1", "stored")
    assert s.sets == []


def test_no_password_attempts_with_none():
    c, calls = make()
    c.connect()
    assert calls[0][1] is None


def test_ensure_returns_device_when_connected():
    c, _ = make()
    c.connect()
    assert c.ensure() is c.device


def test_ensure_backoff_sequence():
    sleeps = []
    c, _ = make(fail={"10.11.99.1", "192.168.1.5"}, sleeps=sleeps)
    c.connect()
    for _ in range(7):
        with pytest.raises(DeviceError):
            c.ensure()
    assert sleeps == [1, 2, 4, 8, 16, 30, 30]


def test_ensure_reconnects_and_resets_backoff():
    sleeps = []
    down = {"10.11.99.1"}
    calls = []

    def factory(host, pw):
        if host in down:
            raise DeviceError("x")
        return FakeDevice(files=FILES)

    c = Connection(Config(), factory=factory, secrets=Secrets(), sleep=sleeps.append)
    c.connect()
    down.clear()
    dev = c.ensure()
    assert dev is c.device and c.state == State.USB
    assert sleeps == [1]


def test_disconnect():
    c, _ = make()
    c.connect()
    d = c.device
    c.disconnect()
    assert c.state == State.OFFLINE and c.device is None
    assert ("close", "") in d.log


def test_install_ssh_key_idempotent(tmp_path):
    pub = tmp_path / "id.pub"
    pub.write_text("ssh-ed25519 AAAA test@host\n")
    d = FakeDevice()
    install_ssh_key(d, pub)
    install_ssh_key(d, pub)
    data = d.files["/home/root/.ssh/authorized_keys"].decode()
    assert data.count("ssh-ed25519 AAAA test@host") == 1


def test_install_ssh_key_appends_to_existing(tmp_path):
    pub = tmp_path / "id.pub"
    pub.write_text("ssh-ed25519 NEW c\n")
    d = FakeDevice(files={"/home/root/.ssh/authorized_keys": b"ssh-rsa OLD x"})
    install_ssh_key(d, pub)
    lines = d.files["/home/root/.ssh/authorized_keys"].decode().splitlines()
    assert lines == ["ssh-rsa OLD x", "ssh-ed25519 NEW c"]


def test_install_ssh_key_read_failure_never_overwrites(tmp_path):
    pub = tmp_path / "id.pub"
    pub.write_text("ssh-ed25519 NEW c\n")
    path = "/home/root/.ssh/authorized_keys"
    d = FakeDevice(files={path: b"ssh-rsa OLD x"}, fail_on={"read_bytes": DeviceError("io")})
    with pytest.raises(DeviceError):
        install_ssh_key(d, pub)
    assert not [m for m, _ in d.log if m == "write_bytes"]


def test_forget_host_key(tmp_path):
    from rmdesk.device import forget_host_key
    kh = tmp_path / "known_hosts"
    kh.write_text("10.11.99.1 ssh-ed25519 AAAA\n"
                  "192.168.1.5,other ssh-rsa BBBB\n"
                  "192.168.1.50 ssh-rsa CCCC\n"
                  "[10.11.99.1]:2222 ssh-rsa DDDD\n")
    forget_host_key("10.11.99.1", kh)
    assert kh.read_text() == ("192.168.1.5,other ssh-rsa BBBB\n"
                              "192.168.1.50 ssh-rsa CCCC\n")
    forget_host_key("192.168.1.5", kh)
    assert kh.read_text() == "192.168.1.50 ssh-rsa CCCC\n"
    forget_host_key("nothing", tmp_path / "missing")  # no file: no error


# --- shared device job guard (anything that stops/restarts xochitl) ---

def test_job_guard_is_exclusive():
    from rmdesk.connection import BUSY_MSG
    c = Connection(Config())
    assert c.current_job is None
    assert c.try_begin_job("uploading files")
    assert c.current_job == "uploading files"
    assert not c.try_begin_job("moving to Trash")
    assert c.current_job == "uploading files"
    c.end_job()
    assert c.current_job is None
    assert c.try_begin_job("moving to Trash")
    c.end_job()
    assert BUSY_MSG.format("x") == "The tablet is busy with: x. Try again when it's done."


def test_job_guard_context_manager_releases_on_error():
    from rmdesk.connection import JobBusy
    c = Connection(Config())
    with pytest.raises(RuntimeError):
        with c.job("sending to a notebook"):
            assert c.current_job == "sending to a notebook"
            raise RuntimeError("boom")
    assert c.current_job is None
    assert c.try_begin_job("a")
    with pytest.raises(JobBusy, match="busy with: a"):
        with c.job("b"):
            pass
    assert c.current_job == "a"


def test_job_guard_thread_safe():
    import threading
    c = Connection(Config())
    wins = []
    barrier = threading.Barrier(8, timeout=10)

    def go(i):
        barrier.wait()
        if c.try_begin_job(f"job {i}"):
            wins.append(i)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert len(wins) == 1


def test_pending_refresh_counts_and_notifies():
    from rmdesk.config import Config
    from rmdesk.connection import Connection
    conn = Connection(Config())
    seen = []
    conn.on_pending.append(seen.append)
    conn.add_pending(2)
    conn.add_pending(0)
    conn.add_pending(1)
    assert conn.pending_refresh == 3
    conn.clear_pending()
    conn.clear_pending()
    assert conn.pending_refresh == 0 and seen == [2, 3, 0]
