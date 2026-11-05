"""Device abstraction: command execution and file access on the tablet."""
from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import paramiko

KNOWN_HOSTS = Path.home() / ".config" / "rmdesk" / "known_hosts"
SFTP_TIMEOUT = 30  # seconds, per SFTP channel operation


def forget_host_key(host: str, known_hosts: Path = KNOWN_HOSTS) -> None:
    """Remove every known_hosts entry for host (plain, [host]:port and hashed forms)."""
    import base64
    import hashlib
    import hmac
    try:
        lines = known_hosts.read_text(encoding="utf-8").splitlines(keepends=True)
    except FileNotFoundError:
        return

    def matches(field: str) -> bool:
        if field.startswith("|1|"):
            try:
                _, _, salt, digest = field.split("|")
                mac = hmac.new(base64.b64decode(salt), host.encode(), hashlib.sha1).digest()
                return hmac.compare_digest(mac, base64.b64decode(digest))
            except ValueError:
                return False
        for name in field.split(","):
            if name == host or (name.startswith("[") and name[1:].rsplit("]:", 1)[0] == host):
                return True
        return False

    kept = [ln for ln in lines if not (ln.split() and matches(ln.split()[0]))]
    if len(kept) != len(lines):
        known_hosts.write_text("".join(kept), encoding="utf-8")


class DeviceError(Exception):
    pass


class HostKeyChanged(DeviceError):
    pass


@dataclass
class CommandResult:
    code: int
    out: str
    err: str


class Device(Protocol):
    def run(self, cmd: str, timeout: float = 30) -> CommandResult: ...
    def read_bytes(self, path: str) -> bytes: ...
    def write_bytes(self, path: str, data: bytes) -> None: ...
    def rename(self, src: str, dst: str) -> None: ...
    def listdir(self, path: str) -> list[str]: ...
    def exists(self, path: str) -> bool: ...
    def stat_mtime(self, path: str) -> float: ...
    def close(self) -> None: ...


class _TrustOnFirstUse(paramiko.MissingHostKeyPolicy):
    """Unknown hosts are added and saved; changed keys are rejected by paramiko."""

    def __init__(self, known_hosts: Path):
        self._path = known_hosts

    def missing_host_key(self, client, hostname, key):
        client.get_host_keys().add(hostname, key.get_name(), key)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        client.save_host_keys(str(self._path))


def _collect(chan, timeout: float) -> CommandResult:
    """Read stdout and stderr together until exit, within a total deadline."""
    deadline = time.monotonic() + timeout
    out, err = bytearray(), bytearray()
    try:
        while True:
            if time.monotonic() > deadline:
                raise DeviceError(f"command timed out after {timeout}s")
            progressed = False
            while chan.recv_ready():
                out += chan.recv(65536)
                progressed = True
            while chan.recv_stderr_ready():
                err += chan.recv_stderr(65536)
                progressed = True
            if chan.exit_status_ready() and not progressed:
                # drain anything that arrived alongside the exit status
                if not (chan.recv_ready() or chan.recv_stderr_ready()):
                    break
            elif not progressed:
                time.sleep(0.02)
        return CommandResult(
            chan.recv_exit_status(),
            out.decode("utf-8", "replace"),
            err.decode("utf-8", "replace"),
        )
    except (paramiko.SSHException, OSError) as e:
        raise DeviceError(str(e)) from e
    finally:
        chan.close()


WRITER_TIMEOUT = 5  # seconds a write to a writer channel may block (a stalled link)


class ChannelWriter:
    """stdin of a command started by SshDevice.open_writer."""

    def __init__(self, chan):
        self._chan = chan

    def write(self, data: bytes) -> None:
        try:
            self._chan.sendall(data)
        except (paramiko.SSHException, OSError) as e:
            raise DeviceError(str(e)) from e

    def check(self) -> None:
        """Raise DeviceError if the command has exited (e.g. its device went away)."""
        try:
            if not self._chan.exit_status_ready():
                return
            err = bytearray()
            while self._chan.recv_stderr_ready():
                err += self._chan.recv_stderr(4096)
            code = self._chan.recv_exit_status()
        except (paramiko.SSHException, OSError) as e:
            raise DeviceError(str(e)) from e
        msg = err.decode("utf-8", "replace").strip()
        raise DeviceError(msg or f"the input writer exited ({code})")

    def close(self) -> None:
        try:
            self._chan.shutdown_write()  # EOF: the command finishes on its own
        except (paramiko.SSHException, OSError, EOFError):
            pass
        try:
            self._chan.close()
        except (paramiko.SSHException, OSError, EOFError):
            pass


READER_POLL = 0.5  # seconds a read from a reader channel waits before saying "nothing yet"


class ChannelReader:
    """stdout of a command started by SshDevice.open_reader (e.g. `cat /dev/input/event2`)."""

    def __init__(self, chan):
        self._chan = chan

    def read(self, n: int = 4096) -> bytes | None:
        """Up to n bytes; None if nothing came within READER_POLL; b"" once the command has
        ended (its output is all read). Raises DeviceError on a link failure."""
        try:
            return self._chan.recv(n)
        except socket.timeout:
            return None
        except (paramiko.SSHException, OSError, EOFError) as e:
            raise DeviceError(str(e)) from e

    def error(self) -> str:
        """What the ended command said on stderr (best effort)."""
        err = bytearray()
        try:
            while self._chan.recv_stderr_ready():
                err += self._chan.recv_stderr(4096)
        except (paramiko.SSHException, OSError, EOFError):
            pass
        return err.decode("utf-8", "replace").strip()

    def close(self) -> None:
        try:
            self._chan.close()  # the command gets SIGPIPE / hangup on the tablet
        except (paramiko.SSHException, OSError, EOFError):
            pass


def _key_loads_without_passphrase(path: str) -> bool:
    """True if the private key at path can be used without a passphrase.

    A passphrase-protected key is skipped (paramiko would otherwise try the tablet
    password as its passphrase); its unlocked copy can still come from ssh-agent."""
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            cls(filename=path, password=None)
            return True
        except (paramiko.SSHException, ValueError, TypeError, OSError):
            continue
    return False


class SshDevice:
    """One SSH connection to the tablet. Thread-safe: every public method holds a
    per-instance lock, because paramiko's SFTP client and the transport's packet
    stream must not be used by several threads at once (views refresh in parallel)."""

    def __init__(self, host: str, password: str | None, key_filename: str | None = None):
        self._lock = threading.RLock()
        if key_filename is not None and not _key_loads_without_passphrase(key_filename):
            key_filename = None
        self._client = paramiko.SSHClient()
        KNOWN_HOSTS.parent.mkdir(parents=True, exist_ok=True)
        if KNOWN_HOSTS.exists():
            self._client.load_host_keys(str(KNOWN_HOSTS))
        self._client.set_missing_host_key_policy(_TrustOnFirstUse(KNOWN_HOSTS))
        try:
            self._client.connect(
                host, username="root", password=password, key_filename=key_filename,
                timeout=10, look_for_keys=False, allow_agent=True,
            )
        except paramiko.BadHostKeyException as e:
            raise HostKeyChanged(f"host key for {host} has changed") from e
        except (paramiko.SSHException, OSError) as e:
            raise DeviceError(str(e)) from e
        try:
            self._sftp = self._client.open_sftp()
            # a hung SFTP call must not hold the device lock forever (R43);
            # socket.timeout is an OSError, so methods raise DeviceError
            self._sftp.get_channel().settimeout(SFTP_TIMEOUT)
        except (paramiko.SSHException, OSError) as e:
            self._client.close()
            raise DeviceError(str(e)) from e

    def run(self, cmd: str, timeout: float = 30) -> CommandResult:
        with self._lock:
            try:
                chan = self._client.get_transport().open_session(timeout=timeout)
                chan.exec_command(cmd)
            except (paramiko.SSHException, OSError, AttributeError) as e:
                raise DeviceError(str(e)) from e
            return _collect(chan, timeout)

    def open_writer(self, cmd: str, timeout: float = 10) -> "ChannelWriter":
        """Start a long-lived command (e.g. `cat > /dev/input/event2`) and return its stdin.

        Only the open holds the device lock; writes go straight to the channel, so a slow SFTP
        call never stalls them (paramiko's transport serialises packets itself)."""
        with self._lock:
            try:
                chan = self._client.get_transport().open_session(timeout=timeout)
                chan.settimeout(WRITER_TIMEOUT)
                chan.exec_command(cmd)
            except (paramiko.SSHException, OSError, AttributeError) as e:
                raise DeviceError(str(e)) from e
        return ChannelWriter(chan)

    def open_reader(self, cmd: str, timeout: float = 10) -> "ChannelReader":
        """Start a long-lived command (e.g. `cat /dev/input/event2`) and return its stdout,
        read in READER_POLL waits. Like open_writer, only the open holds the device lock."""
        with self._lock:
            try:
                chan = self._client.get_transport().open_session(timeout=timeout)
                chan.settimeout(READER_POLL)
                chan.exec_command(cmd)
            except (paramiko.SSHException, OSError, AttributeError) as e:
                raise DeviceError(str(e)) from e
        return ChannelReader(chan)

    def read_bytes(self, path: str) -> bytes:
        with self._lock:
            try:
                with self._sftp.open(path, "rb") as f:
                    return f.read()
            except (OSError, paramiko.SSHException) as e:
                raise DeviceError(f"read {path}: {e}") from e

    def write_bytes(self, path: str, data: bytes) -> None:
        with self._lock:
            try:
                with self._sftp.open(path, "wb") as f:
                    f.write(data)
            except (OSError, paramiko.SSHException) as e:
                raise DeviceError(f"write {path}: {e}") from e

    def rename(self, src: str, dst: str) -> None:
        with self._lock:
            try:
                try:
                    self._sftp.posix_rename(src, dst)  # atomic overwrite
                except IOError:
                    self._sftp.rename(src, dst)
            except (OSError, paramiko.SSHException) as e:
                raise DeviceError(f"rename {src} -> {dst}: {e}") from e

    def listdir(self, path: str) -> list[str]:
        with self._lock:
            try:
                return self._sftp.listdir(path)
            except (OSError, paramiko.SSHException) as e:
                raise DeviceError(f"listdir {path}: {e}") from e

    def exists(self, path: str) -> bool:
        with self._lock:
            try:
                self._sftp.stat(path)
                return True
            except FileNotFoundError:
                return False
            except (OSError, paramiko.SSHException) as e:
                raise DeviceError(f"stat {path}: {e}") from e

    def stat_mtime(self, path: str) -> float:
        with self._lock:
            try:
                return float(self._sftp.stat(path).st_mtime)
            except (OSError, paramiko.SSHException) as e:
                raise DeviceError(f"stat {path}: {e}") from e

    def close(self) -> None:
        with self._lock:
            try:
                self._sftp.close()
            finally:
                self._client.close()
