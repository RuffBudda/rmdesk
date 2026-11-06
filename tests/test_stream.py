"""Tests for rmdesk.stream: wire decoder, stream client, goMarkableStream install."""
from __future__ import annotations

import datetime
import hashlib
import http.server
import json
import os
import shlex
import ssl
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from rmdesk.device import CommandResult
from rmdesk.stream import install
import socket

from rmdesk.stream.client import Decoder, Frame, ProtocolError
from rmdesk.stream.client import PORT as STREAM_PORT
from rmdesk.stream.client import StreamClient as _StreamClient
from tests import stream_fixtures as sf
from tests.fakes import FakeDevice


def tcp_dial(host: str, port: int):
    """The tests' stand-in for SshDevice.open_tunnel: a plain TCP socket to the fake server."""
    return lambda timeout: socket.create_connection((host, port), timeout)


def StreamClient(host, password, on_frame, *, port=STREAM_PORT, **kw):
    kw.setdefault("dial", tcp_dial(host, port))
    return _StreamClient(host, password, on_frame, port=port, **kw)

W, H = 1404, 1872


def _chunks(data: bytes, seed: int = 1):
    """Split a byte stream at random points, like HTTP chunk boundaries would."""
    rng = np.random.default_rng(seed)
    i = 0
    while i < len(data):
        n = int(rng.integers(1, 200_000))
        yield data[i:i + n]
        i += n


# ---------------------------------------------------------------- decoder

def test_decoder_full_frame():
    images = sf.synthetic_session(n_deltas=0)
    msg = sf.encode_session(images)[0]
    assert msg[0] == 0x03
    frame = Decoder().feed(msg)
    assert isinstance(frame, Frame)
    assert (frame.width, frame.height) == (W, H)
    assert frame.gray.shape == (H, W) and frame.gray.dtype == np.uint8
    assert np.array_equal(frame.gray, images[0])


def test_decoder_deltas_match_expected():
    images = sf.synthetic_session(n_deltas=20)
    msgs = sf.encode_session(images)
    types = {m[0] for m in msgs[1:]}
    assert {0x01, 0x03, 0x04} <= types  # exercises every frame type v1.4.0 sends
    assert any(len(m) == 4 for m in msgs)  # and an empty delta
    expected = images[-1]
    dec = Decoder()
    frame = None
    for chunk in _chunks(b"".join(msgs)):
        frame = dec.feed(chunk) or frame
    assert frame is not None
    match = np.mean(frame.gray == expected)
    assert match >= 0.995
    assert np.array_equal(frame.gray, expected)


def test_decoder_uncompressed_deltas_match():
    images = sf.synthetic_session(width=300, height=200, n_deltas=10, seed=3)
    dec = Decoder(300, 200)
    for msg in sf.encode_session(images, compress=False):
        frame = dec.feed(msg)
    assert np.array_equal(frame.gray, images[-1])


def test_decoder_partial_message_returns_none():
    msg = sf.encode_session(sf.synthetic_session(n_deltas=0))[0]
    dec = Decoder()
    assert dec.feed(msg[:3]) is None
    assert dec.feed(msg[3:100]) is None
    assert dec.feed(msg[100:]) is not None


# Hand-built byte sequences copied from upstream's own tests at v1.4.0, so the
# decoder is checked against the server's layout, not only against our encoder.

def test_decoder_short_run_from_upstream_test():
    # internal/delta/delta_test.go TestShortRunEncoding: offset 100, length 10,
    # data bytes 0..39 → run header 0a 64 00, frame header 01 2b 00 00.
    data = bytes(range(40))
    msg = bytes([0x01, 43, 0x00, 0x00]) + bytes([0x0A, 0x64, 0x00]) + data
    dec = Decoder(40, 1)  # 160-byte framebuffer
    frame = dec.feed(msg)
    bgra = np.zeros(160, np.uint8)
    bgra[100:140] = np.frombuffer(data, np.uint8)
    px = bgra.reshape(-1, 4).astype(np.uint16)
    expected = ((77 * px[:, 2] + 150 * px[:, 1] + 29 * px[:, 0]) >> 8).astype(np.uint8)
    assert np.array_equal(frame.gray.ravel(), expected)
    assert frame.gray[0, 24] == 0 and frame.gray[0, 35] == 0 and frame.gray[0, 34] != 0


def test_decoder_long_run_from_upstream_test():
    # internal/delta/delta_test.go TestLongRunEncoding: offset 70000 (> 64 KiB),
    # length 200 (> 127) → run header 80 c8 70 11 01.
    data = bytes([0x10, 0x20, 0x30, 0xFF]) * 200
    payload = bytes([0x80, 0xC8, 0x70, 0x11, 0x01]) + data
    msg = bytes([0x01]) + len(payload).to_bytes(3, "little") + payload
    dec = Decoder(200, 100)  # 80 000-byte framebuffer
    frame = dec.feed(msg)
    gray = frame.gray.ravel()
    g = (77 * 0x30 + 150 * 0x20 + 29 * 0x10) >> 8
    assert (gray[17500:17700] == g).all()
    assert (gray[:17500] == 0).all() and (gray[17700:] == 0).all()


def test_decoder_empty_delta_from_upstream_test():
    # internal/delta/delta_test.go TestEncode_NoChanges_EmptyDelta: 01 00 00 00.
    images = sf.synthetic_session(width=50, height=20, n_deltas=0)
    dec = Decoder(50, 20)
    dec.feed(sf.encode_session(images)[0])
    frame = dec.feed(bytes([0x01, 0x00, 0x00, 0x00]))
    assert np.array_equal(frame.gray, images[0])


def test_decoder_rejects_wrong_full_frame_size():
    msg = sf.full_frame(b"\x00" * (10 * 10 * 4))
    with pytest.raises(ProtocolError):
        Decoder(20, 20).feed(msg)


def test_decoder_rejects_run_out_of_bounds():
    payload = bytes([0x02, 0x00, 0x00]) + b"\x00" * 8
    msg = bytes([0x01, len(payload), 0, 0]) + payload
    with pytest.raises(ProtocolError):
        Decoder(1, 1).feed(msg)


def test_decoder_rejects_unknown_type():
    with pytest.raises(ProtocolError):
        Decoder(1, 1).feed(bytes([0x09, 0, 0, 0]))


def test_decoder_two_runs_offset_is_relative_to_previous_run_end():
    # Hand-computed: 10×1 frame (40 bytes).
    # run 1: 2 px at absolute byte 8 (pixels 2-3): header 02 08 00; it ends at byte 16.
    # run 2: 1 px at absolute byte 28 (pixel 7): relative offset 28 - 16 = 12 → 01 0c 00.
    p1 = bytes([0x10, 0x10, 0x10, 0xFF]) * 2
    p2 = bytes([0x40, 0x40, 0x40, 0xFF])
    payload = bytes([0x02, 0x08, 0x00]) + p1 + bytes([0x01, 0x0C, 0x00]) + p2
    assert len(payload) == 18
    msg = bytes([0x01, 18, 0x00, 0x00]) + payload
    gray = Decoder(10, 1).feed(msg).gray.ravel()
    assert gray.tolist() == [0, 0, 0x10, 0x10, 0, 0, 0, 0x40, 0, 0]


def test_decoder_rejects_zero_length_run_storm():
    payload = bytes([0x00, 0x00, 0x00]) * 10_000
    msg = bytes([0x01]) + len(payload).to_bytes(3, "little") + payload
    with pytest.raises(ProtocolError, match="zero-length"):
        Decoder(100, 100).feed(msg)
    # a storm bigger than any legitimate run stream is refused before parsing
    big = bytes(3) * 2_000_000
    with pytest.raises(ProtocolError, match="too large"):
        Decoder(100, 100).feed(bytes([0x01]) + len(big).to_bytes(3, "little") + big)


def _overlong_run_message(pixels=40_000):
    # What upstream's assembleDeltaFrame (delta.go:680-681) emits for a run longer than
    # 15 bits: the length is masked to 40000 & 0x7FFF = 7232, but all 40000 pixels follow.
    n = pixels & 0x7FFF
    payload = bytes([0x80 | (n >> 8), n & 0xFF, 0, 0, 0]) + b"\xff" * (pixels * 4)
    return bytes([0x01]) + len(payload).to_bytes(3, "little") + payload


def test_decoder_overlong_run_is_a_protocol_error():
    with pytest.raises(ProtocolError):
        Decoder(400, 100).feed(_overlong_run_message())


def _zstd_bomb(total=64 * 1024 * 1024):
    from compression import zstd
    c = zstd.ZstdCompressor()
    zero = bytes(1 << 20)
    out = b"".join(c.compress(zero) for _ in range(total >> 20)) + c.flush()
    assert len(out) < 1 << 24
    return out


@pytest.mark.parametrize("ftype", [0x03, 0x04])
def test_decoder_zstd_bomb_is_rejected_with_bounded_memory(ftype):
    import tracemalloc
    bomb = _zstd_bomb()
    msg = bytes([ftype]) + len(bomb).to_bytes(3, "little") + bomb
    dec = Decoder()
    tracemalloc.start()
    t0 = time.monotonic()
    try:
        with pytest.raises(ProtocolError, match="expands beyond"):
            dec.feed(msg)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert time.monotonic() - t0 < 2.0
    assert peak < 80 * 1024 * 1024  # vs 64 MiB+ if fully expanded, never the 400 MB class


def test_decoder_gzip_bomb_is_rejected():
    import zlib
    c = zlib.compressobj(wbits=31)
    zero = bytes(1 << 20)
    bomb = b"".join(c.compress(zero) for _ in range(64)) + c.flush()
    msg = bytes([0x02]) + len(bomb).to_bytes(3, "little") + bomb
    with pytest.raises(ProtocolError, match="expands beyond"):
        Decoder(100, 100).feed(msg)


def test_decoder_truncated_zstd_full_frame_is_rejected():
    msg = sf.full_frame(b"\x00" * 400)
    payload = msg[4:-3]
    with pytest.raises(ProtocolError):
        Decoder(10, 10).feed(bytes([0x03]) + len(payload).to_bytes(3, "little") + payload)


def _capture_dir() -> Path:
    env = os.environ.get("RMDESK_CAPTURES")
    return Path(env) if env else Path.home() / ".local/share/rmdesk/test-captures/stream"


def _real_capture_files():
    d = _capture_dir()
    return sorted(d.glob("msg_*.bin")) if d.is_dir() else []


@pytest.mark.skipif(not _real_capture_files() or not (_capture_dir() / "expected_final.png").exists(),
                    reason="no local stream capture (kept outside git for privacy)")
def test_decoder_real_capture():
    import cv2
    expected = cv2.imread(str(_capture_dir() / "expected_final.png"), cv2.IMREAD_GRAYSCALE)
    dec = Decoder()
    frames = [dec.feed(p.read_bytes()) for p in _real_capture_files()]
    assert (frames[0].width, frames[0].height) == (W, H)
    final = [f for f in frames if f is not None][-1]
    assert np.mean(np.abs(final.gray.astype(int) - expected.astype(int)) <= 2) >= 0.995


# ---------------------------------------------------------------- pick_release

def test_pick_release_table():
    assert install.pick_release("3.24.0.149") == install.LATEST
    assert install.LATEST == "v1.4.0"
    assert install.pick_release("3.26.1.2") == install.LATEST
    assert install.pick_release("4.0.0.1") == install.LATEST
    # 3.6 – <3.24: ">= 0.11.0 (may work, not tested)" → newest release satisfying it
    assert install.pick_release("3.20.0.92") == install.LATEST
    # 3.4 – <3.6: ">= 0.8.6 and < 0.11.0" → newest release in range
    assert install.pick_release("3.5.2.1807") == "v0.10.0"
    # < 3.4: "< 0.8.6"
    assert install.pick_release("3.3.2.1666") == "v0.8.5"
    with pytest.raises(ValueError):
        install.pick_release("")


def test_stream_supported_only_on_3_24_plus():
    assert install.stream_supported("3.24.0.149")
    assert not install.stream_supported("3.23.0.64")
    assert not install.stream_supported("garbage")


# ---------------------------------------------------------------- install

SERIAL = "RM110-123-45678"
ENV = install.ENV_PATH


class FakeSecrets:
    def __init__(self):
        self.store = {}

    def get(self, serial):
        return self.store.get(serial)

    def set(self, serial, password):
        self.store[serial] = password


class GmsTablet(FakeDevice):
    """FakeDevice that emulates goMarkableStream's install subcommand and systemd unit."""

    def __init__(self, files=None):
        super().__init__(files=dict(files or {}))
        self.active = False
        self.chmodded: set[str] = set()
        self.restarts = 0
        self.run_handler = self._handle

    def _handle(self, cmd):
        argv = shlex.split(cmd)
        if argv[:2] == ["chmod", "+x"]:
            self.chmodded.update(a for a in argv[2:] if a != "--")
            return CommandResult(0, "", "")
        if argv == [install.GMS_PATH, "install"]:
            assert install.GMS_PATH in self.chmodded
            self.files.setdefault(ENV, b"# RK_SERVER_PASSWORD=password\n")
            self.files.setdefault(install.UNIT_PATH, b"[Unit]\n")
            self.active = True
            return CommandResult(0, "Installation complete!\n", "")
        if argv[:1] == ["systemctl"] and argv[-1] == "goMarkableStream":
            verb = argv[1]
            if verb == "is-active":
                return CommandResult(0, "active\n", "") if self.active else CommandResult(3, "inactive\n", "")
            if verb == "restart":
                self.restarts += 1
                self.active = True
            if verb == "disable":
                self.active = False
            return CommandResult(0, "", "")
        return None

    def rename(self, src, dst):
        super().rename(src, dst)
        if src in self.chmodded:
            self.chmodded.discard(src)
            self.chmodded.add(dst)

    def cmds(self):
        return [a for m, a in self.log if m == "run"]


BINARY = b"\x7fELF fake arm binary"


def _downloader(binary=BINARY, checksum=None):
    calls = []
    sha = checksum or hashlib.sha256(binary).hexdigest()

    def dl(url):
        calls.append(url)
        if url.endswith("/checksums.txt"):
            return (f"{'0' * 64}  gomarkablestream-RM2\n"
                    f"{sha}  {install.ASSET_NAME}\n").encode()
        return binary

    dl.calls = calls
    return dl


@pytest.fixture
def no_pin(monkeypatch):
    monkeypatch.setattr(install, "KNOWN_SHA256", {})


def test_asset_url_is_pinned():
    assert install.ASSET_NAME == "gomarkablestream-RM2-lite"
    assert install.asset_url("v1.4.0") == (
        "https://github.com/owulveryck/goMarkableStream/releases/download/"
        "v1.4.0/gomarkablestream-RM2-lite")
    assert install.KNOWN_SHA256["v1.4.0"] == (
        "35b3f17ee96995344954fb754fd108f1c10a68580291c3dcb3f8c4998f1aba5b")


def test_install_writes_password_env(no_pin):
    existing = (b"# goMarkableStream Environment Configuration\n"
                b"# RK_SERVER_PASSWORD=password\n"
                b"RK_SERVER_PASSWORD=old-one\n"
                b"RK_DELTA_THRESHOLD=0.25\n")
    dev = GmsTablet({ENV: existing, "/sys/devices/soc0/serial_number": SERIAL.encode() + b"\n"})
    secrets = FakeSecrets()
    dl = _downloader()
    install.install(dev, "v1.4.0", dl, secrets=secrets)

    assert dl.calls[0].endswith("/v1.4.0/gomarkablestream-RM2-lite")
    assert dev.files[install.GMS_PATH] == BINARY
    user, pw = install.split_credentials(secrets.store[SERIAL])
    assert user == "admin"
    assert len(pw) == 16
    lines = dev.files[ENV].decode().splitlines()
    assert lines == ["# goMarkableStream Environment Configuration",
                     "# RK_SERVER_PASSWORD=password",
                     "RK_DELTA_THRESHOLD=0.25",
                     f"RK_SERVER_PASSWORD={pw}",
                     "RK_SERVER_BIND_ADDR=127.0.0.1:2001"]
    # env written atomically: tmp file then rename onto the real path
    renames = [a for m, a in dev.log if m == "rename"]
    assert f"{ENV}{install.TMP_SUFFIX} -> {ENV}" in renames
    assert f"{install.GMS_PATH}{install.TMP_SUFFIX} -> {install.GMS_PATH}" in renames
    assert not any(p.endswith(install.TMP_SUFFIX) for p in dev.files)
    cmds = dev.cmds()
    assert cmds.index(f"{install.GMS_PATH} install") < cmds.index("systemctl restart goMarkableStream")
    assert dev.restarts == 1
    assert install.status(dev) == "running"


def test_install_writes_env_before_running_install(no_pin):
    # the service must never start with the default admin/password
    dev = GmsTablet({"/sys/devices/soc0/serial_number": SERIAL.encode()})
    secrets = FakeSecrets()
    install.install(dev, "v1.4.0", _downloader(), secrets=secrets)
    _, pw = install.split_credentials(secrets.store[SERIAL])
    assert dev.files[ENV] == (f"RK_SERVER_PASSWORD={pw}\n"
                              "RK_SERVER_BIND_ADDR=127.0.0.1:2001\n").encode()
    log = dev.log
    tmp = f"{ENV}{install.TMP_SUFFIX}"
    env_rename = log.index(("rename", f"{tmp} -> {ENV}"))
    # the file holding the password is 0600 before its first byte is written
    create600 = log.index(("run", f"umask 077 && rm -f -- {tmp} && : > {tmp}"))
    mkdir = log.index(("run", "mkdir -p -m 700 -- /home/root/.config/goMarkableStream"))
    run_install = log.index(("run", f"{install.GMS_PATH} install"))
    assert mkdir < create600 < env_rename < run_install
    assert not any(m == "run" and "chmod 600" in a for m, a in log)


def test_install_keyring_failure_never_runs_install(no_pin):
    class Broken(FakeSecrets):
        def set(self, serial, password):
            raise RuntimeError("keyring locked")
    dev = GmsTablet({"/sys/devices/soc0/serial_number": SERIAL.encode()})
    with pytest.raises(RuntimeError, match="keyring"):
        install.install(dev, "v1.4.0", _downloader(), secrets=Broken())
    assert not any(c.endswith(" install") for c in dev.cmds())
    assert install.GMS_PATH not in dev.files and ENV not in dev.files


def test_install_env_write_failure_never_runs_install(no_pin):
    from tests.fakes import FailRule
    from rmdesk.device import DeviceError
    dev = GmsTablet({"/sys/devices/soc0/serial_number": SERIAL.encode()})
    dev.fail_on["write_bytes"] = FailRule(DeviceError("disk full"), match=ENV)
    with pytest.raises(DeviceError):
        install.install(dev, "v1.4.0", _downloader(), secrets=FakeSecrets())
    assert not any(c.endswith(" install") for c in dev.cmds())


def test_install_unreadable_env_is_not_overwritten(no_pin):
    from tests.fakes import FailRule
    from rmdesk.device import DeviceError
    dev = GmsTablet({ENV: b"RK_DELTA_THRESHOLD=0.2\n",
                     "/sys/devices/soc0/serial_number": SERIAL.encode()})
    dev.fail_on["read_bytes"] = FailRule(DeviceError("io error"), match=ENV)
    with pytest.raises(DeviceError):
        install.install(dev, "v1.4.0", _downloader(), secrets=FakeSecrets())
    assert dev.files[ENV] == b"RK_DELTA_THRESHOLD=0.2\n"
    assert not any(m == "write_bytes" and a.startswith(ENV) for m, a in dev.log)
    assert not any(c.endswith(" install") for c in dev.cmds())


def test_install_keeps_custom_username(no_pin):
    dev = GmsTablet({ENV: b'RK_SERVER_USERNAME="bob"\n',
                     "/sys/devices/soc0/serial_number": SERIAL.encode()})
    secrets = FakeSecrets()
    install.install(dev, "v1.4.0", _downloader(), secrets=secrets)
    user, pw = install.stream_credentials(SERIAL, secrets)
    assert user == "bob" and len(pw) == 16
    assert b'RK_SERVER_USERNAME="bob"' in dev.files[ENV]


def test_split_credentials():
    assert install.split_credentials(install.join_credentials("a:b", "pw")) == ("a:b", "pw")
    assert install.split_credentials("onlypw") == ("admin", "onlypw")
    assert install.stream_credentials("nope", FakeSecrets()) is None


def test_install_password_is_random(no_pin):
    pws = set()
    for _ in range(3):
        dev = GmsTablet({"/sys/devices/soc0/serial_number": SERIAL.encode()})
        s = FakeSecrets()
        install.install(dev, "v1.4.0", _downloader(), secrets=s)
        pws.add(s.store[SERIAL])
    assert len(pws) == 3


def test_install_rejects_checksum_mismatch(no_pin):
    dev = GmsTablet({"/sys/devices/soc0/serial_number": SERIAL.encode()})
    with pytest.raises(install.InstallError, match="checksum"):
        install.install(dev, "v1.4.0", _downloader(checksum="ab" * 32), secrets=FakeSecrets())
    assert install.GMS_PATH not in dev.files
    # only the read-only serial lookup ran; nothing was changed on the tablet
    assert all(c.startswith("cat ") for c in dev.cmds())


def test_install_rejects_unpinned_binary():
    # KNOWN_SHA256 pins v1.4.0's real digest; a fake binary must not get through
    dev = GmsTablet({"/sys/devices/soc0/serial_number": SERIAL.encode()})
    with pytest.raises(install.InstallError, match="checksum"):
        install.install(dev, "v1.4.0", _downloader(), secrets=FakeSecrets())
    assert install.GMS_PATH not in dev.files


def test_install_rejects_empty_download(no_pin):
    dev = GmsTablet({"/sys/devices/soc0/serial_number": SERIAL.encode()})
    with pytest.raises(install.InstallError, match="empty"):
        install.install(dev, "v1.4.0", _downloader(binary=b""), secrets=FakeSecrets())


def test_install_rejects_unsupported_tag():
    dev = GmsTablet({"/sys/devices/soc0/serial_number": SERIAL.encode()})
    with pytest.raises(install.InstallError, match="v0.8.6"):
        install.install(dev, "v0.8.6", _downloader(), secrets=FakeSecrets())


def test_install_needs_serial(no_pin):
    dev = GmsTablet()
    with pytest.raises(install.InstallError, match="serial"):
        install.install(dev, "v1.4.0", _downloader(), secrets=FakeSecrets())


def test_install_reports_failed_install_command(no_pin):
    dev = GmsTablet({"/sys/devices/soc0/serial_number": SERIAL.encode()})
    orig = dev._handle
    dev.run_handler = lambda c: (CommandResult(1, "", "boom") if c.endswith(" install")
                                 else orig(c))
    with pytest.raises(install.InstallError, match="boom"):
        install.install(dev, "v1.4.0", _downloader(), secrets=FakeSecrets())


def test_install_with_explicit_serial_and_quoted_paths(no_pin, monkeypatch):
    monkeypatch.setattr(install, "GMS_PATH", "/home/root/go Markable")
    dev = GmsTablet()
    s = FakeSecrets()
    dev.run_handler = lambda c: CommandResult(0, "", "")
    install.install(dev, "v1.4.0", _downloader(), serial="SN1", secrets=s)
    assert install.split_credentials(s.store["SN1"])[0] == "admin"
    cmds = dev.cmds()
    assert "chmod +x -- '/home/root/go Markable.rmdesk-tmp'" in cmds
    assert "'/home/root/go Markable' install" in cmds


def test_status_missing_stopped_running():
    dev = GmsTablet()
    assert install.status(dev) == "missing"
    dev.files[install.GMS_PATH] = BINARY
    assert install.status(dev) == "stopped"
    dev.active = True
    assert install.status(dev) == "running"


def test_install_creates_tailscale_dir_before_install_and_restart(no_pin):
    dev = GmsTablet({"/sys/devices/soc0/serial_number": SERIAL.encode() + b"\n"})
    install.install(dev, "v1.4.0", _downloader(), secrets=FakeSecrets())
    cmds = dev.cmds()
    mk = f"mkdir -p -- {shlex.quote(install.TAILSCALE_DIR)}"
    assert install.TAILSCALE_DIR == "/home/root/.tailscale"
    assert cmds.count(mk) == 1
    assert cmds.index(mk) < cmds.index(f"{install.GMS_PATH} install")
    assert cmds.index(mk) < cmds.index("systemctl restart goMarkableStream")


def test_status_activating_and_failed_are_failing():
    for state in ("activating", "failed"):
        dev = GmsTablet({install.GMS_PATH: BINARY})
        dev.run_handler = lambda cmd, s=state: (
            CommandResult(3, s + "\n", "") if cmd == "systemctl is-active goMarkableStream" else None)
        assert install.status(dev) == "failing"
    dev = GmsTablet()  # no binary: still missing
    dev.run_handler = lambda cmd: CommandResult(3, "failed\n", "")
    assert install.status(dev) == "missing"


def test_uninstall_rmdirs_tailscale_dir_after_unit_removal():
    dev = GmsTablet({install.GMS_PATH: BINARY, install.UNIT_PATH: b"[Unit]\n"})
    install.uninstall(dev)
    cmds = dev.cmds()
    rd = f"rmdir -- {shlex.quote(install.TAILSCALE_DIR)} 2>/dev/null || true"
    assert cmds.count(rd) == 1
    rm = next(i for i, c in enumerate(cmds) if c.startswith("rm -f"))
    assert cmds.index(rd) > rm
    assert cmds.index(rd) > cmds.index("systemctl daemon-reload")


def test_uninstall_removes_only_binary_and_unit():
    dev = GmsTablet({install.GMS_PATH: BINARY, install.UNIT_PATH: b"[Unit]\n", ENV: b"x=1\n"})
    dev.active = True
    install.uninstall(dev)
    cmds = dev.cmds()
    assert cmds[0] == "systemctl disable --now goMarkableStream"
    rm = [c for c in cmds if c.startswith("rm ")]
    assert rm == [f"rm -f -- {install.GMS_PATH} {install.UNIT_PATH}"]
    assert not any("-r" in shlex.split(c) or "-rf" in c for c in cmds)
    assert "systemctl daemon-reload" in cmds
    assert ENV in dev.files
    assert install.GMS_PATH not in dev.files and install.UNIT_PATH not in dev.files
    assert install.status(dev) == "missing"


def test_stream_secrets_service_name():
    assert install.KEYRING_SERVICE == "rmdesk-stream"


# ---------------------------------------------------------------- StreamClient

def _self_signed(tmp_path: Path) -> tuple[str, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "remarkable")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    cert_p, key_p = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_p.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                        serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
    return str(cert_p), str(key_p)


class FakeGms:
    """A tiny HTTPS goMarkableStream: /login issues a token, /stream sends `messages`
    chunked, then either holds the connection open (hold=True) or closes it."""

    def __init__(self, tmp_path, messages, password="pw", hold=True):
        self.messages, self.password, self.hold = messages, password, hold
        self.logins = 0
        self.streams = 0
        self.busy = 0  # answer this many /stream requests with 429 (another viewer)
        self.rejected = 0
        self.stream_paths = []  # request paths of /stream calls
        self.release = threading.Event()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path != "/login" or body != {"username": "admin",
                                                     "password": outer.password}:
                    self._reply(401, b'{"error": "Invalid credentials"}')
                    return
                outer.logins += 1
                self._reply(200, json.dumps({"token": "tok123", "expiresIn": 86400}).encode())

            def _reply(self, code, body):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if not self.path.startswith("/stream?rate="):
                    self._reply(404, b"{}")
                    return
                if self.headers.get("Authorization") != "Bearer tok123":
                    self._reply(401, b"Unauthorized\n")
                    return
                if outer.busy > 0:
                    outer.busy -= 1
                    outer.rejected += 1
                    self._reply(429, b"Too Many Requests\n")
                    return
                outer.streams += 1
                outer.stream_paths.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                self.end_headers()
                for chunk in _chunks(b"".join(outer.messages), seed=outer.streams):
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                    self.wfile.flush()
                if outer.hold:
                    outer.release.wait(10)  # idle stream: no bytes, connection open
                try:
                    self.wfile.write(b"0\r\n\r\n")
                except OSError:
                    pass
                self.close_connection = True

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*_self_signed(tmp_path))
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


def _wait(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_stream_client_receives_frames_and_stops_fast(tmp_path):
    images = sf.synthetic_session(width=300, height=200, n_deltas=8, seed=5)
    gms = FakeGms(tmp_path, sf.encode_session(images))
    frames = []
    client = StreamClient("127.0.0.1", "pw", frames.append, port=gms.port,
                          width=300, height=200)
    try:
        client.start()
        assert _wait(lambda: frames and np.array_equal(frames[-1].gray, images[-1]))
        assert client.is_alive()
        # the default context still verifies certificates: TLS-off is per-connection only
        assert ssl.create_default_context().verify_mode == ssl.CERT_REQUIRED
        t0 = time.monotonic()
        client.stop()  # stream is idle and blocked in a read
        assert time.monotonic() - t0 < 1.0
        assert not client.is_alive()
    finally:
        client.stop()
        gms.close()


def test_stream_client_idle_stream_is_not_a_timeout(tmp_path):
    # the server sends nothing while the tablet is idle; that must not count as an error
    images = sf.synthetic_session(width=60, height=40, n_deltas=1, seed=2)
    gms = FakeGms(tmp_path, sf.encode_session(images))
    frames, errors = [], []
    client = StreamClient("127.0.0.1", "pw", frames.append, port=gms.port, width=60,
                          height=40, connect_timeout=0.2, retry_delay=0.05,
                          on_error=errors.append)
    try:
        client.start()
        assert _wait(lambda: len(frames) >= 1)
        time.sleep(0.8)
        assert errors == [] and gms.streams == 1
    finally:
        client.stop()
        gms.close()


def test_stream_client_reconnects_after_error(tmp_path):
    images = sf.synthetic_session(width=60, height=40, n_deltas=2, seed=9)
    gms = FakeGms(tmp_path, sf.encode_session(images), hold=False)
    frames, errors = [], []
    client = StreamClient("127.0.0.1", "pw", frames.append, port=gms.port,
                          width=60, height=40, retry_delay=0.05, on_error=errors.append)
    try:
        client.start()
        assert _wait(lambda: gms.streams >= 3)
        assert gms.logins == 1  # token is reused across reconnects
        assert errors  # "stream ended" was reported
    finally:
        client.stop()
        gms.close()


@pytest.mark.parametrize("bad", ["overlong", "zero_storm"])
def test_stream_client_reconnects_on_protocol_error(tmp_path, bad):
    images = sf.synthetic_session(width=400, height=100, n_deltas=0)
    if bad == "overlong":
        evil = _overlong_run_message()
    else:
        payload = bytes(3) * 1000
        evil = bytes([0x01]) + len(payload).to_bytes(3, "little") + payload
    gms = FakeGms(tmp_path, sf.encode_session(images) + [evil], hold=False)
    frames, errors = [], []
    client = StreamClient("127.0.0.1", "pw", frames.append, port=gms.port, width=400,
                          height=100, retry_delay=0.05, on_error=errors.append)
    try:
        client.start()
        assert _wait(lambda: gms.streams >= 2 and
                     any(isinstance(e, ProtocolError) for e in errors))
        assert client.is_alive()  # the bad message ended the session, not the thread
    finally:
        client.stop()
        gms.close()


def test_stream_client_stop_then_start_keeps_streaming(tmp_path):
    images = sf.synthetic_session(width=60, height=40, n_deltas=1, seed=4)
    gms = FakeGms(tmp_path, sf.encode_session(images))
    frames = []
    slow = threading.Event()

    def on_frame(f):
        frames.append(f)
        if not slow.is_set():  # first frame: outlive stop()'s 0.9 s join
            slow.set()
            time.sleep(1.3)

    client = StreamClient("127.0.0.1", "pw", on_frame, port=gms.port, width=60,
                          height=40, retry_delay=0.05)
    try:
        client.start()
        assert _wait(lambda: slow.is_set())
        client.stop()
        assert client.is_alive()  # old thread still inside on_frame
        client.start()  # must not see the old thread as "running" and do nothing
        n = len(frames)
        assert _wait(lambda: gms.streams >= 2 and len(frames) > n)
        assert client.is_alive()
        time.sleep(0.2)
        assert client.is_alive()
        t0 = time.monotonic()
        client.stop()
        assert time.monotonic() - t0 < 1.0 and not client.is_alive()
    finally:
        client.stop()
        gms.close()


def test_stream_client_default_rate_requests_50ms_frames(tmp_path):
    images = sf.synthetic_session(width=60, height=40, n_deltas=1, seed=3)
    gms = FakeGms(tmp_path, sf.encode_session(images))
    frames = []
    client = StreamClient("127.0.0.1", "pw", frames.append, port=gms.port, width=60, height=40)
    try:
        client.start()
        assert _wait(lambda: len(frames) >= 1)
        assert gms.stream_paths[0] == "/stream?rate=50"
    finally:
        client.stop()
        gms.close()


def test_stream_client_default_retry_is_two_seconds():
    c = StreamClient("10.11.99.1", "pw", lambda f: None)
    assert c.retry_delay == 2.0
    assert c.port == install.PORT == 2001


def test_stream_client_bad_password_reports_and_retries(tmp_path):
    gms = FakeGms(tmp_path, [], password="right")
    errors = []
    client = StreamClient("127.0.0.1", "wrong", lambda f: None, port=gms.port,
                          retry_delay=0.05, on_error=errors.append)
    try:
        client.start()
        assert _wait(lambda: len(errors) >= 2)
        assert "401" in str(errors[0])
        t0 = time.monotonic()
        client.stop()
        assert time.monotonic() - t0 < 1.0
    finally:
        client.stop()
        gms.close()


def test_resync_reconnects_at_once_keeps_token_and_gets_a_full_frame(tmp_path):
    images = sf.synthetic_session(width=60, height=40, n_deltas=3, seed=11)
    gms = FakeGms(tmp_path, sf.encode_session(images))  # holds the connection open (idle)
    frames, errors = [], []
    client = StreamClient("127.0.0.1", "pw", frames.append, port=gms.port, width=60,
                          height=40, retry_delay=30.0, on_error=errors.append)
    try:
        client.start()
        assert _wait(lambda: frames and np.array_equal(frames[-1].gray, images[-1]))
        n = len(frames)
        # the next connection sends only a full frame of a different picture
        later = np.full((40, 60), 77, np.uint8)
        later[5:15, 10:30] = 3
        gms.messages = sf.encode_session([later])
        t0 = time.monotonic()
        client.resync()
        # a 30 s retry delay would blow this bound: the reconnect is immediate
        assert _wait(lambda: gms.streams == 2 and len(frames) > n, timeout=5.0)
        assert time.monotonic() - t0 < 5.0
        assert gms.logins == 1  # same token, no new login
        assert errors == []  # a resync is not an error
        assert np.array_equal(frames[-1].gray, later)  # the full frame was decoded
        assert client.is_alive()
    finally:
        client.stop()
        gms.close()


def test_resync_when_not_running_is_a_no_op():
    c = StreamClient("127.0.0.1", "pw", lambda f: None, port=1)
    c.resync()  # never started
    assert not c.is_alive()


def test_resync_after_stop_does_not_restart(tmp_path):
    images = sf.synthetic_session(width=60, height=40, n_deltas=0, seed=3)
    gms = FakeGms(tmp_path, sf.encode_session(images))
    frames = []
    client = StreamClient("127.0.0.1", "pw", frames.append, port=gms.port, width=60,
                          height=40, retry_delay=0.05)
    try:
        client.start()
        assert _wait(lambda: len(frames) >= 1)
        client.stop()
        client.resync()
        time.sleep(0.3)
        assert not client.is_alive() and gms.streams == 1
    finally:
        client.stop()
        gms.close()


def test_http_429_reports_status_and_keeps_retrying_with_delay(tmp_path):
    images = sf.synthetic_session(width=60, height=40, n_deltas=0, seed=6)
    gms = FakeGms(tmp_path, sf.encode_session(images))
    gms.busy = 2
    frames, errors = [], []
    client = StreamClient("127.0.0.1", "pw", frames.append, port=gms.port, width=60,
                          height=40, retry_delay=0.3, on_error=errors.append)
    try:
        t0 = time.monotonic()
        client.start()
        assert _wait(lambda: len(frames) >= 1)
        elapsed = time.monotonic() - t0
        assert gms.rejected == 2
        assert [getattr(e, "status", None) for e in errors] == [429, 429]
        assert elapsed >= 0.55  # two normal retry delays, not an immediate hammering
        assert gms.logins == 1
    finally:
        client.stop()
        gms.close()


def test_429_right_after_resync_is_retried_silently_and_fast(tmp_path):
    # upstream keeps its single-client lock until it notices our old socket closed, so the
    # reconnect right after a resync can be refused by our OWN previous session
    images = sf.synthetic_session(width=60, height=40, n_deltas=0, seed=12)
    gms = FakeGms(tmp_path, sf.encode_session(images))
    frames, errors = [], []
    client = StreamClient("127.0.0.1", "pw", frames.append, port=gms.port, width=60,
                          height=40, retry_delay=30.0, on_error=errors.append)
    try:
        client.start()
        assert _wait(lambda: len(frames) >= 1)
        later = np.full((40, 60), 99, np.uint8)
        gms.messages = sf.encode_session([later])
        gms.busy = 1
        n = len(frames)
        t0 = time.monotonic()
        client.resync()
        assert _wait(lambda: len(frames) > n, timeout=5.0)
        assert time.monotonic() - t0 < 1.0
        assert gms.rejected == 1 and gms.logins == 1
        assert errors == []  # no on_error: the UI shows no "another viewer" message
        assert np.array_equal(frames[-1].gray, later)
    finally:
        client.stop()
        gms.close()


def test_429_long_after_resync_is_reported(tmp_path, monkeypatch):
    from rmdesk.stream import client as sc
    monkeypatch.setattr(sc, "RESYNC_429_GRACE", 0.0)  # the grace window is already over
    images = sf.synthetic_session(width=60, height=40, n_deltas=0, seed=13)
    gms = FakeGms(tmp_path, sf.encode_session(images))
    frames, errors = [], []
    client = StreamClient("127.0.0.1", "pw", frames.append, port=gms.port, width=60,
                          height=40, retry_delay=0.05, on_error=errors.append)
    try:
        client.start()
        assert _wait(lambda: len(frames) >= 1)
        gms.busy = 1
        client.resync()
        assert _wait(lambda: any(getattr(e, "status", None) == 429 for e in errors))
    finally:
        client.stop()
        gms.close()


def test_stream_package_has_no_qt():
    import subprocess
    import sys
    code = ("import sys, rmdesk.stream.client, rmdesk.stream.install; "
            "sys.exit(any(m.startswith('PySide6') for m in sys.modules))")
    assert subprocess.run([sys.executable, "-c", code],
                          cwd=Path(__file__).parent.parent / "src").returncode == 0


def test_install_reads_rm2_device_tree_serial(no_pin):
    dev = GmsTablet({"/proc/device-tree/serial-number": b"3800000000001\x00"})
    secrets = FakeSecrets()
    install.install(dev, "v1.4.0", _downloader(), secrets=secrets)
    assert "3800000000001" in secrets.store
    assert "cat /proc/device-tree/serial-number" in dev.cmds()
    assert not any(m == "read_bytes" and "serial" in a for m, a in dev.log)


def test_install_has_no_own_serial_path():
    assert not hasattr(install, "SERIAL_PATH")


def test_restart_restarts_only_the_stream_service():
    dev = GmsTablet({install.GMS_PATH: BINARY, install.UNIT_PATH: b"[Unit]\n"})
    install.restart(dev)
    cmds = dev.cmds()
    assert cmds == [f"systemctl restart {install.UNIT}"]
    assert not any("xochitl" in c for c in cmds)
