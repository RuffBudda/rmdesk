"""Install, remove and query goMarkableStream on the tablet.

Facts pinned from upstream v1.4.0 (see docs/stream-protocol.md, "Install facts").
"""
from __future__ import annotations

import hashlib
import re
import secrets as _secrets
import shlex
from typing import Callable, Literal, Protocol

from rmdesk.device import Device, write_private_remote
from rmdesk.firmware import read_serial

GMS_PATH = "/home/root/goMarkableStream"
PORT = 2001
UNIT = "goMarkableStream"
UNIT_PATH = "/etc/systemd/system/goMarkableStream.service"
ENV_PATH = "/home/root/.config/goMarkableStream/env"
TMP_SUFFIX = ".rmdesk-tmp"
KEYRING_SERVICE = "rmdesk-stream"
# goMarkableStream listens on the tablet's loopback only; rmdesk reaches it through its SSH
# connection (SshDevice.open_tunnel), so nothing on the network can see the service
BIND_ADDR = f"127.0.0.1:{PORT}"
BIND_LINE = f"RK_SERVER_BIND_ADDR={BIND_ADDR}"

REPO = "owulveryck/goMarkableStream"
LATEST = "v1.4.0"
# reMarkable 2 build (GOARCH=arm GOARM=7) without Tailscale. Plain binary, not an archive.
ASSET_NAME = "gomarkablestream-RM2-lite"
# Tags whose release ships ASSET_NAME (the v1.x line); older tags ship tar.gz archives
# and speak an older stream protocol, so rmdesk does not install them.
SUPPORTED_TAGS = {"v1.4.0", "v1.3.1", "v1.3.0", "v1.2.1", "v1.2.0"}
# sha256 of ASSET_NAME per tag, from the release's checksums.txt / GitHub asset digest.
KNOWN_SHA256 = {
    "v1.4.0": "35b3f17ee96995344954fb754fd108f1c10a68580291c3dcb3f8c4998f1aba5b",
}

# goMarkableStream's unit lists this in ReadWritePaths (install.go:84-88); systemd refuses
# to start the unit (status=226/NAMESPACE) when the directory does not exist.
TAILSCALE_DIR = "/home/root/.tailscale"

# "failing": the unit is installed but crash-looping (`activating`) or `failed`.
Status = Literal["missing", "stopped", "failing", "running"]


class InstallError(Exception):
    pass


class SecretStore(Protocol):
    def get(self, serial: str) -> str | None: ...
    def set(self, serial: str, password: str) -> None: ...


class StreamSecrets:
    """Keyring entry service "rmdesk-stream", username = tablet serial.

    The stored value is "<stream username>:<stream password>" (see join_credentials);
    the generated password never contains ":".
    """

    def get(self, serial: str) -> str | None:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, serial)

    def set(self, serial: str, password: str) -> None:
        import keyring
        keyring.set_password(KEYRING_SERVICE, serial, password)


DEFAULT_USERNAME = "admin"  # goMarkableStream's default RK_SERVER_USERNAME (main.go:31)


def join_credentials(username: str, password: str) -> str:
    return f"{username}:{password}"


def split_credentials(value: str) -> tuple[str, str]:
    """Inverse of join_credentials; the password is after the last ":"."""
    username, sep, password = value.rpartition(":")
    if not sep:
        return DEFAULT_USERNAME, value
    return username, password


def stream_credentials(serial: str, secrets: SecretStore | None = None) -> tuple[str, str] | None:
    """(username, password) for the tablet's goMarkableStream, or None if not installed by rmdesk."""
    secrets = secrets if secrets is not None else StreamSecrets()
    value = secrets.get(serial)
    return split_credentials(value) if value else None


def _fw(version: str) -> tuple[int, int]:
    m = re.match(r"\s*(\d+)\.(\d+)", version or "")
    if not m:
        raise ValueError(f"unrecognised firmware version: {version!r}")
    return int(m.group(1)), int(m.group(2))


def pick_release(fw_version: str) -> str:
    """Release tag per the firmware table in upstream README.md (v1.4.0, lines 36-43),
    taking the newest release inside each row's range."""
    fw = _fw(fw_version)
    if fw >= (3, 6):
        return LATEST     # >=3.24: latest; 3.6-<3.24: ">= 0.11.0" (may work, not tested)
    if fw >= (3, 4):
        return "v0.10.0"  # ">= 0.8.6 and < 0.11.0"
    return "v0.8.5"       # "< 0.8.6"


def stream_supported(fw_version: str) -> bool:
    """rmdesk's decoder only understands the firmware >= 3.24 framebuffer (1404x1872 BGRA)."""
    try:
        return _fw(fw_version) >= (3, 24)
    except ValueError:
        return False


def asset_url(tag: str, name: str = ASSET_NAME) -> str:
    return f"https://github.com/{REPO}/releases/download/{tag}/{name}"


def http_download(url: str) -> bytes:
    """Default downloader for the UI (normal certificate verification)."""
    import requests
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    return r.content


def _run(device: Device, cmd: str, timeout: float = 30) -> str:
    res = device.run(cmd, timeout=timeout)
    if res.code != 0:
        raise InstallError(f"`{cmd}` failed ({res.code}): {(res.err or res.out).strip()}")
    return res.out


def status(device: Device) -> Status:
    res = device.run(f"systemctl is-active {UNIT}")
    state = res.out.strip()
    if state == "active":
        return "running"
    if not device.exists(GMS_PATH):
        return "missing"
    if state in ("activating", "failed"):
        return "failing"
    return "stopped"


def _checksum_for(text: str, name: str) -> str | None:
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            return parts[0].lower()
    return None


def _verified_binary(tag: str, downloader: Callable[[str], bytes]) -> bytes:
    if tag not in SUPPORTED_TAGS:
        raise InstallError(f"goMarkableStream {tag} is not supported by rmdesk "
                           f"(supported: {', '.join(sorted(SUPPORTED_TAGS))})")
    data = downloader(asset_url(tag))
    if not data:
        raise InstallError(f"download of {ASSET_NAME} {tag} was empty")
    sums = downloader(asset_url(tag, "checksums.txt")).decode("utf-8", "replace")
    want = _checksum_for(sums, ASSET_NAME)
    got = hashlib.sha256(data).hexdigest()
    if want is None:
        raise InstallError(f"checksums.txt for {tag} has no entry for {ASSET_NAME}")
    if got != want:
        raise InstallError(f"checksum mismatch for {ASSET_NAME} {tag}: "
                           f"got {got}, checksums.txt says {want}")
    pinned = KNOWN_SHA256.get(tag)
    if pinned is not None and got != pinned:
        raise InstallError(f"checksum mismatch for {ASSET_NAME} {tag}: "
                           f"got {got}, rmdesk pins {pinned}")
    return data


def _write_atomic(device: Device, path: str, data: bytes) -> None:
    tmp = path + TMP_SUFFIX
    device.write_bytes(tmp, data)
    device.rename(tmp, path)


def _env_username(env: str) -> str:
    name = DEFAULT_USERNAME
    for ln in env.splitlines():
        m = re.match(r"\s*(?:export\s+)?RK_SERVER_USERNAME\s*=\s*(.*?)\s*$", ln)
        if m:
            name = m.group(1).strip("'\"") or DEFAULT_USERNAME
    return name


_BIND_RE = r"\s*(export\s+)?RK_SERVER_BIND_ADDR\s*="


def _env_with_bind(current: str) -> str:
    """The env file with RK_SERVER_BIND_ADDR set to the tablet's loopback (any other value
    removed)."""
    kept = [ln for ln in current.splitlines() if not re.match(_BIND_RE, ln)]
    kept.append(BIND_LINE)
    return "\n".join(kept) + "\n"


def _env_with_password(current: str, password: str) -> str:
    kept = [ln for ln in current.splitlines()
            if not re.match(r"\s*(export\s+)?RK_SERVER_PASSWORD\s*=", ln)]
    kept.append(f"RK_SERVER_PASSWORD={password}")
    return _env_with_bind("\n".join(kept))


def is_local_only(env: str) -> bool:
    """True if the env file binds the service to the tablet's loopback (last line wins)."""
    value = None
    for ln in env.splitlines():
        if re.match(_BIND_RE, ln):
            value = ln.split("=", 1)[1].strip().strip("'\"")
    return value == BIND_ADDR


def ensure_local_only(device: Device) -> bool:
    """For a stream installed by an older rmdesk (listening on every interface): bind it to
    the tablet's loopback and restart it. Returns True if it changed anything. Does nothing
    when goMarkableStream isn't installed or is already local-only."""
    if not device.exists(ENV_PATH):
        return False
    current = device.read_bytes(ENV_PATH).decode("utf-8", "replace")
    if is_local_only(current):
        return False
    write_private_remote(device, ENV_PATH, _env_with_bind(current).encode("utf-8"))
    if device.exists(GMS_PATH):
        _run(device, f"systemctl restart {UNIT}", timeout=60)
    return True


def install(device: Device, tag: str, downloader: Callable[[str], bytes], *,
            serial: str | None = None, secrets: SecretStore | None = None) -> None:
    """Download `tag` on the PC, install it as a systemd service, set a random password."""
    secrets = secrets if secrets is not None else StreamSecrets()
    if not serial:
        serial = read_serial(device)
    if not serial:
        raise InstallError("could not read the tablet serial number")

    data = _verified_binary(tag, downloader)

    # 1. Password and env file first: `goMarkableStream install` starts the service, and
    #    it must never come up with the default admin/password. Upstream's install skips
    #    an env file that already exists (install.go:133-135), so ours survives it.
    if device.exists(ENV_PATH):
        # a read failure propagates: never rewrite (and lose) env lines we could not read
        current = device.read_bytes(ENV_PATH).decode("utf-8", "replace")
    else:
        current = ""
    username = _env_username(current)
    password = _secrets.token_urlsafe(12)  # 16 characters of [A-Za-z0-9_-]
    secrets.set(serial, join_credentials(username, password))
    _run(device, f"mkdir -p -m 700 -- {shlex.quote(ENV_PATH.rsplit('/', 1)[0])}")
    write_private_remote(device, ENV_PATH, _env_with_password(current, password).encode("utf-8"))

    # 2. The binary: upload beside the target, then rename (works while the old one runs).
    tmp = GMS_PATH + TMP_SUFFIX
    device.write_bytes(tmp, data)
    _run(device, f"chmod +x -- {shlex.quote(tmp)}")
    device.rename(tmp, GMS_PATH)

    # 3. Unit file + enable/start, then restart so a service that was already running
    #    picks up the new password.
    _run(device, f"mkdir -p -- {shlex.quote(TAILSCALE_DIR)}")
    _run(device, f"{shlex.quote(GMS_PATH)} install", timeout=60)
    _run(device, f"systemctl restart {UNIT}", timeout=60)


def restart(device: Device) -> None:
    """Restart the stream service only (not xochitl). Clears a viewer slot held by a dead
    connection, e.g. one left over from unplugging the USB cable while streaming."""
    _run(device, f"systemctl restart {UNIT}", timeout=60)


def uninstall(device: Device) -> None:
    """Stop and disable the service, remove the unit file and the binary.
    The env/config directory is left in place; the .tailscale directory is removed
    only if empty."""
    device.run(f"systemctl disable --now {UNIT}", timeout=60)  # fine if it was never enabled
    _run(device, f"rm -f -- {shlex.quote(GMS_PATH)} {shlex.quote(UNIT_PATH)}")
    _run(device, "systemctl daemon-reload", timeout=60)
    device.run(f"rmdir -- {shlex.quote(TAILSCALE_DIR)} 2>/dev/null || true")  # only if empty
