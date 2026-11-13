"""Regression tests for the Omarchy marketplace security review (0.1.1, 0.1.2, 0.1.3)."""
from __future__ import annotations

import logging
import socket
import stat
import threading
from pathlib import Path

import numpy as np
import paramiko
import pytest

from rmdesk import device, ids
from rmdesk.device import USB_HOST, UnknownHostKey, bridge_channel, write_private_remote
from rmdesk.stream import install
from rmdesk.stream.client import StreamClient
from tests import stream_fixtures as sf
from tests.fakes import FakeDevice
from tests.test_stream import ENV, FakeGms, GmsTablet, _wait


# -- H1: the stream runs through SSH, and only on the tablet's loopback --

class SocketChannel:
    """A paramiko-channel stand-in backed by a real socket (recv / sendall / close)."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.closed = threading.Event()

    def recv(self, n):
        return self.sock.recv(n)

    def sendall(self, data):
        self.sock.sendall(data)

    def close(self):
        self.closed.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def test_bridge_carries_bytes_both_ways_and_closes_the_channel():
    far, near = socket.socketpair()
    chan = SocketChannel(near)
    sock = bridge_channel(chan)
    try:
        sock.sendall(b"ping")
        assert far.recv(4) == b"ping"
        far.sendall(b"pong")
        assert sock.recv(4) == b"pong"
    finally:
        sock.close()
    assert chan.closed.wait(2)  # closing our end ends the SSH channel too
    far.close()


def test_bridge_ends_when_the_tablet_side_closes():
    far, near = socket.socketpair()
    sock = bridge_channel(SocketChannel(near))
    far.close()
    sock.settimeout(2)
    assert sock.recv(1) == b""
    sock.close()


def test_stream_client_streams_through_a_bridged_channel(tmp_path):
    images = sf.synthetic_session(width=60, height=40, n_deltas=3, seed=2)
    gms = FakeGms(tmp_path, sf.encode_session(images))
    dials = []

    def dial(timeout):
        dials.append(timeout)
        return bridge_channel(SocketChannel(socket.create_connection(("127.0.0.1", gms.port),
                                                                     timeout)))

    frames = []
    client = StreamClient("127.0.0.1", "pw", frames.append, port=gms.port, width=60,
                          height=40, dial=dial)
    try:
        client.start()
        assert _wait(lambda: frames and np.array_equal(frames[-1].gray, images[-1]))
        assert len(dials) >= 2  # /login and /stream both went through the dial
    finally:
        client.stop()
        gms.close()


def test_stream_client_needs_a_dial():
    with pytest.raises(TypeError):
        StreamClient("10.11.99.1", "pw", lambda f: None)  # no direct LAN connection


def test_install_binds_the_service_to_loopback(monkeypatch):
    monkeypatch.setattr(install, "KNOWN_SHA256", {})
    from tests.test_stream import FakeSecrets, _downloader
    dev = GmsTablet({"/sys/devices/soc0/serial_number": b"RM110-123-45678"})
    install.install(dev, "v1.4.0", _downloader(), secrets=FakeSecrets())
    env = dev.files[ENV].decode()
    assert install.is_local_only(env)
    assert "RK_SERVER_BIND_ADDR=127.0.0.1:2001" in env.splitlines()


def test_old_install_is_moved_to_loopback_and_restarted():
    dev = GmsTablet({ENV: b"RK_SERVER_PASSWORD=x\nRK_SERVER_BIND_ADDR=:2001\n",
                     install.GMS_PATH: b"bin"})
    assert install.ensure_local_only(dev) is True
    assert dev.files[ENV] == b"RK_SERVER_PASSWORD=x\nRK_SERVER_BIND_ADDR=127.0.0.1:2001\n"
    assert "systemctl restart goMarkableStream" in dev.cmds()
    # the rewrite is 0600 before the password goes in
    tmp = ENV + device.PRIVATE_TMP_SUFFIX
    assert ("run", f"umask 077 && rm -f -- {tmp} && : > {tmp}") in dev.log
    # second time: nothing to do, no restart
    before = dev.cmds().count("systemctl restart goMarkableStream")
    assert install.ensure_local_only(dev) is False
    assert dev.cmds().count("systemctl restart goMarkableStream") == before


def test_ensure_local_only_without_a_stream_does_nothing():
    dev = FakeDevice({})
    assert install.ensure_local_only(dev) is False
    assert not [a for m, a in dev.log if m in ("write", "rename")]


@pytest.mark.parametrize("env,local", [
    ("RK_SERVER_BIND_ADDR=127.0.0.1:2001\n", True),
    ("export RK_SERVER_BIND_ADDR='127.0.0.1:2001'\n", True),
    ("RK_SERVER_BIND_ADDR=127.0.0.1:2001\nRK_SERVER_BIND_ADDR=:2001\n", False),  # last wins
    ("RK_SERVER_PASSWORD=x\n", False),
    ("RK_SERVER_BIND_ADDR=0.0.0.0:2001\n", False),
])
def test_is_local_only(env, local):
    assert install.is_local_only(env) is local


def test_write_private_remote_creates_0600_before_writing():
    dev = FakeDevice({})
    write_private_remote(dev, "/home/root/secret", b"s3cret")
    assert dev.files["/home/root/secret"] == b"s3cret"
    tmp = "/home/root/secret" + device.PRIVATE_TMP_SUFFIX
    create = dev.log.index(("run", f"umask 077 && rm -f -- {tmp} && : > {tmp}"))
    rename = dev.log.index(("rename", f"{tmp} -> /home/root/secret"))
    assert create < rename


# -- M1: a Wi-Fi address must show the key pinned for the USB address --

class FakeClient:
    def __init__(self, keys: paramiko.HostKeys):
        self._keys = keys

    def get_host_keys(self):
        return self._keys


@pytest.fixture(scope="module")
def two_keys():
    return paramiko.RSAKey.generate(1024), paramiko.RSAKey.generate(1024)


def test_usb_address_is_trusted_on_first_use(tmp_path, two_keys):
    kh = tmp_path / "cfg" / "known_hosts"
    keys = paramiko.HostKeys()
    device._TrustOnFirstUse(kh, usb_verified=True).missing_host_key(
        FakeClient(keys), USB_HOST, two_keys[0])
    assert keys.lookup(USB_HOST)
    assert stat.S_IMODE(kh.stat().st_mode) == 0o600
    assert stat.S_IMODE(kh.parent.stat().st_mode) == 0o700


# -- H1: the USB address is trusted on first use only over the real USB link --

@pytest.mark.parametrize("policy", [
    lambda kh: device._TrustOnFirstUse(kh),
    lambda kh: device._TrustOnFirstUse(kh, usb_verified=False),
])
def test_usb_address_not_over_the_cable_is_refused(tmp_path, two_keys, policy):
    """10.11.99.1 reached some other way (e.g. a Wi-Fi network that routes it): an unknown
    key is not trusted and nothing (no password) is sent."""
    kh = tmp_path / "known_hosts"
    keys = paramiko.HostKeys()
    with pytest.raises(UnknownHostKey) as ei:
        policy(kh).missing_host_key(FakeClient(keys), USB_HOST, two_keys[0])
    assert ei.value.host == USB_HOST
    assert keys.lookup(USB_HOST) is None
    assert not kh.exists()


def _fake_sys_net(tmp_path, name, devpath, driver):
    """/sys/class/net/<name>/device -> devpath, with devpath/driver -> .../<driver>."""
    root = tmp_path / "sys"
    dev = root / "devices" / devpath
    dev.mkdir(parents=True)
    drv = root / "bus" / "drivers" / driver
    drv.mkdir(parents=True)
    (dev / "driver").symlink_to(drv)
    net = root / "class" / "net" / name
    net.mkdir(parents=True)
    (net / "device").symlink_to(dev)
    return root / "class" / "net"


def test_usb_link_accepts_the_tablet_gadget(tmp_path):
    sys_net = _fake_sys_net(tmp_path, "usb0", "pci0000:00/0000:00:14.0/usb1/1-2/1-2:1.0",
                            "rndis_host")
    assert device.usb_link("10.11.99.2", {"usb0": "10.11.99.2"}, sys_net) == "usb0"


@pytest.mark.parametrize("local_ip,ifaces,devpath,driver", [
    # the PC's end of the connection is on Wi-Fi (the route to 10.11.99.1 goes elsewhere)
    ("192.168.1.20", {"wlan0": "192.168.1.20"}, "pci0000:00/0000:00:14.3", "iwlwifi"),
    # an attacker's network hands out 10.11.99.x over Wi-Fi
    ("10.11.99.5", {"wlan0": "10.11.99.5"}, "pci0000:00/0000:00:14.3", "iwlwifi"),
    # a USB Wi-Fi dongle on a 10.11.99.x network is not the tablet's cable
    ("10.11.99.5", {"wlan1": "10.11.99.5"}, "pci0000:00/usb1/1-3/1-3:1.0", "rtl8xxxu"),
    # the address belongs to no interface (e.g. a VPN or tunnel)
    ("10.11.99.5", {"usb0": "10.11.99.2"}, "pci0000:00/usb1/1-2/1-2:1.0", "rndis_host"),
    ("not-an-ip", {"usb0": "10.11.99.2"}, "pci0000:00/usb1/1-2/1-2:1.0", "rndis_host"),
])
def test_usb_link_refuses_other_paths(tmp_path, local_ip, ifaces, devpath, driver):
    name = next(iter(ifaces))
    sys_net = _fake_sys_net(tmp_path, name, devpath, driver)
    assert device.usb_link(local_ip, ifaces, sys_net) is None


def test_usb_link_refuses_a_virtual_interface(tmp_path):
    sys_net = tmp_path / "net"
    (sys_net / "br0").mkdir(parents=True)  # no device link
    assert device.usb_link("10.11.99.2", {"br0": "10.11.99.2"}, sys_net) is None


def test_route_usb_link_requires_the_route_to_use_the_usb_gadget(tmp_path):
    sys_net = _fake_sys_net(tmp_path, "usb0", "pci0000:00/usb1/1-2/1-2:1.0", "rndis_host")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(device, "_iface_ipv4", lambda name: "10.11.99.2" if name == "usb0" else None)
    try:
        assert device.route_usb_link("usb0", "10.11.99.2", sys_net) == "usb0"
        # same 10.11.99.x address exists on the USB gadget, but the route to 10.11.99.1
        # goes over Wi-Fi (attacker-controlled host route): must not be trusted
        assert device.route_usb_link("wlan0", "10.11.99.2", sys_net) is None
        assert device.route_usb_link(None, "10.11.99.2", sys_net) is None
        assert device.route_usb_link("usb0", "192.168.1.20", sys_net) is None
    finally:
        monkeypatch.undo()


def test_open_usb_socket_is_not_verified_when_route_leaves_the_cable(monkeypatch):
    class Sock:
        def getsockname(self):
            return ("10.11.99.2", 54321)

        def close(self):
            pass

    monkeypatch.setattr(device.socket, "create_connection", lambda *a, **kw: Sock())
    # local address matches a USB gadget, but kernel routes 10.11.99.1 over Wi-Fi
    monkeypatch.setattr(device, "_usb_route", lambda host=device.USB_HOST: ("wlan0", "10.11.99.2"))
    sock, verified = device._open_usb_socket(device.USB_HOST)
    assert verified is False
    sock.close()


class _PolicyClient:
    """paramiko.SSHClient stand-in: runs the missing-host-key policy like a first connect."""
    seen = []

    def __init__(self):
        self._keys = paramiko.HostKeys()
        self._policy = None
        _PolicyClient.seen.append(self)

    def load_host_keys(self, path):
        pass

    def set_missing_host_key_policy(self, policy):
        self._policy = policy

    def get_host_keys(self):
        return self._keys

    def connect(self, host, **kw):
        self.kw = kw
        self._policy.missing_host_key(self, host, _PolicyClient.key)
        self.authenticated_with = kw["password"]

    def open_sftp(self):
        class _Chan:
            def settimeout(self, t):
                pass

        class _Sftp:
            def get_channel(self):
                return _Chan()
        return _Sftp()

    def close(self):
        pass


@pytest.mark.parametrize("verified", [False, True])
def test_ssh_device_sends_no_password_unless_the_usb_link_is_verified(
        monkeypatch, tmp_path, two_keys, verified):
    class Sock:
        closed = False

        def close(self):
            Sock.closed = True

    sock = Sock()
    _PolicyClient.seen, _PolicyClient.key = [], two_keys[0]
    monkeypatch.setattr(device.paramiko, "SSHClient", _PolicyClient)
    monkeypatch.setattr(device, "KNOWN_HOSTS", tmp_path / "known_hosts")
    monkeypatch.setattr(device, "_open_usb_socket", lambda host: (sock, verified))
    if verified:
        dev = device.SshDevice(USB_HOST, "root-pw")
        assert dev.usb_verified is True
        assert _PolicyClient.seen[0].authenticated_with == "root-pw"
        assert _PolicyClient.seen[0].kw["sock"] is sock  # the checked socket is the one used
    else:
        with pytest.raises(UnknownHostKey):
            device.SshDevice(USB_HOST, "root-pw")
        assert not hasattr(_PolicyClient.seen[0], "authenticated_with")
        assert Sock.closed


def test_wifi_with_the_usb_key_is_trusted(tmp_path, two_keys):
    kh = tmp_path / "known_hosts"
    keys = paramiko.HostKeys()
    keys.add(USB_HOST, two_keys[0].get_name(), two_keys[0])
    device._TrustOnFirstUse(kh).missing_host_key(FakeClient(keys), "192.168.1.50", two_keys[0])
    assert keys.lookup("192.168.1.50")


@pytest.mark.parametrize("pinned", [False, True])
def test_wifi_with_an_unknown_key_is_refused(tmp_path, two_keys, pinned):
    kh = tmp_path / "known_hosts"
    keys = paramiko.HostKeys()
    if pinned:
        keys.add(USB_HOST, two_keys[0].get_name(), two_keys[0])
    with pytest.raises(UnknownHostKey) as ei:
        device._TrustOnFirstUse(kh).missing_host_key(FakeClient(keys), "192.168.1.50",
                                                     two_keys[1])
    assert keys.lookup("192.168.1.50") is None
    assert not kh.exists()
    assert ei.value.fingerprint == device.fingerprint(two_keys[1])
    assert ei.value.fingerprint.startswith("SHA256:")


def test_trust_host_key_saves_it_privately(tmp_path, two_keys):
    kh = tmp_path / "known_hosts"
    kh.write_text("")
    kh.chmod(0o644)
    device.trust_host_key(UnknownHostKey("192.168.1.50", two_keys[1]), kh)
    saved = paramiko.HostKeys(str(kh))
    assert saved.lookup("192.168.1.50")[two_keys[1].get_name()].asbytes() == \
        two_keys[1].asbytes()
    assert stat.S_IMODE(kh.stat().st_mode) == 0o600


def test_connection_stops_retrying_on_an_unknown_key(tmp_path, two_keys):
    from rmdesk.config import Config
    from rmdesk.connection import Connection, State

    err = UnknownHostKey("192.168.1.50", two_keys[1])
    calls = []

    def factory(host, password):
        calls.append(host)
        raise err if host != USB_HOST else OSError("no route")

    class NoSecrets:
        def get(self, serial):
            return None

    conn = Connection(Config(wifi_ip="192.168.1.50"), factory=factory, secrets=NoSecrets(),
                      sleep=lambda s: None)
    assert conn.connect() == State.OFFLINE
    assert conn.host_errors["192.168.1.50"] is err
    with pytest.raises(UnknownHostKey):
        conn.ensure()


# -- M2 / L7: private log files and folders --

def test_log_files_are_private_after_rollover(tmp_path):
    from rmdesk.ui import app
    folder = tmp_path / "share"
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)
    old = folder / "rmdesk.log.1"
    old.write_text("old")
    old.chmod(0o644)
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        app.setup_logging(folder / "rmdesk.log")
        h = [x for x in root.handlers if x not in before][0]
        h.maxBytes = 50
        for i in range(10):
            logging.getLogger("rmdesk.test").warning("line %d of the log file", i)
        h.flush()
    finally:
        for x in list(root.handlers):
            if x not in before:
                root.removeHandler(x)
                x.close()
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    files = sorted(folder.glob("rmdesk.log*"))
    assert len(files) >= 2  # it rolled over
    for f in files:
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f


def test_config_folder_is_private(tmp_path):
    from rmdesk import config
    d = tmp_path / "rmdesk"
    d.mkdir(mode=0o755)
    d.chmod(0o755)
    config.save(config.Config(), d / "config.toml")
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE((d / "config.toml").stat().st_mode) == 0o600


# -- L2: ids from the tablet never become paths that leave their folder --

@pytest.mark.parametrize("value,ok", [
    ("11111111-2222-3333-4444-555555555555", True), ("pg-1", True), ("pg3", True),
    ("..", False), (".", False), ("../x", False), ("a/b", False), ("", False),
    ("-rf", False), (".hidden", False), ("a" * 65, False), (None, False), (3, False),
])
def test_safe_ids(value, ok):
    assert ids.is_safe(value) is ok


def test_library_skips_unsafe_page_ids():
    from rmdesk.library.model import _page_ids
    content = {"cPages": {"pages": [{"id": "ok-1"}, {"id": "../../etc/passwd"},
                                    {"id": "a/b"}]}}
    assert _page_ids(content) == ["ok-1"]
    assert _page_ids({"pages": ["p1", "../p2"]}) == ["p1"]


def test_backup_refuses_unsafe_ids(tmp_path):
    from rmdesk import safety
    with pytest.raises(safety.BackupError):
        safety.backup(FakeDevice({}), "../escape", root=tmp_path)
    with pytest.raises(safety.BackupError):
        safety.backup_page(FakeDevice({}), "nb", "../../x", root=tmp_path)
    assert list(tmp_path.iterdir()) == []


# -- L3: tablet names in dialogs and file names --

def test_plain_text_neutralises_markup():
    from rmdesk.ui.files_view import plain_text
    assert plain_text("Move “Notes”?") == "Move “Notes”?"
    shown = plain_text('Move “<img src="https://example.invalid/t">”?\nSure')
    assert "<img" not in shown
    assert "&lt;img" in shown
    assert shown.startswith('<p style="white-space: pre-wrap">')


@pytest.mark.parametrize("name,want", [
    ("Notes.pdf", "Notes.pdf"), ("a/b.pdf", "a-b.pdf"), ("../../.bashrc", "-..-.bashrc"),
    (".hidden.pdf", "hidden.pdf"), ("..", "untitled"), ("x\0y", "xy"),
])
def test_safe_filename(name, want):
    from rmdesk.ui.files_view import safe_filename
    out = safe_filename(name)
    assert out == want
    assert "/" not in out and not out.startswith(".")


# -- PDF export of crafted pages: typed text stays text --

def test_typed_text_is_escaped_in_page_svg():
    from rmdesk.notebook import render
    svg = ('<svg xmlns="http://www.w3.org/2000/svg">\n<g>\n'
           '\t\t\t<text x="1" y="2" class="plain">a</text><image href="file:///etc/passwd"/>'
           '<text>b & c</text>\n</g>\n</svg>\n')
    out = render._escape_text(svg)
    render._check_safe(out)  # well-formed, nothing that loads
    assert "<image" not in out and "&lt;image" in out


@pytest.mark.parametrize("bad", [
    '<svg xmlns="http://www.w3.org/2000/svg"><image href="file:///x"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
    '<rect xlink:href="file:///x"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><rect onload="x()"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><script>x()</script></svg>',
    '<svg><unclosed></svg>',
])
def test_unsafe_svg_is_refused(bad):
    from rmdesk.notebook import render
    with pytest.raises(ValueError):
        render._check_safe(bad)
