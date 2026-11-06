"""Device abstraction: command execution and file access on the tablet."""
from __future__ import annotations

import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import paramiko

KNOWN_HOSTS = Path.home() / ".config" / "rmdesk" / "known_hosts"
SFTP_TIMEOUT = 30  # seconds, per SFTP channel operation
# the tablet's address on its USB cable: a direct link, so its key is learned on first use;
# any other address (Wi-Fi) must show this same key, or the user must accept it first
USB_HOST = "10.11.99.1"


def _private_dir(path: Path) -> None:
    """Create `path` (and parents) and make it 0700: it holds known_hosts and settings."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _save_known_hosts(keys: paramiko.HostKeys, known_hosts: Path) -> None:
    _private_dir(known_hosts.parent)
    fd = os.open(known_hosts, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    os.chmod(known_hosts, 0o600)  # an older 0644 file too
    keys.save(str(known_hosts))


def fingerprint(key: paramiko.PKey) -> str:
    """OpenSSH-style SHA256 fingerprint: "SHA256:<base64 without padding>"."""
    import base64
    import hashlib
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


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


class UnknownHostKey(DeviceError):
    """A Wi-Fi address showed a key rmdesk has never seen for this tablet. Nothing was sent
    to it (no password): the user checks the fingerprint and accepts it with trust_host_key,
    or connects once over USB so the key is known."""

    def __init__(self, host: str, key: paramiko.PKey):
        self.host, self.key = host, key
        self.fingerprint = fingerprint(key)
        super().__init__(f"{host} showed an unknown security key ({key.get_name()} "
                         f"{self.fingerprint})")


def trust_host_key(err: UnknownHostKey, known_hosts: Path = KNOWN_HOSTS) -> None:
    """Save the key from an UnknownHostKey for its host (the user accepted it)."""
    keys = paramiko.HostKeys()
    if known_hosts.exists():
        keys.load(str(known_hosts))
    keys.add(err.host, err.key.get_name(), err.key)
    _save_known_hosts(keys, known_hosts)


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
    """The USB address is trusted on first use (it is the cable). Any other address is
    trusted only when it shows the key already pinned for the USB address; otherwise
    UnknownHostKey is raised before anything (a password) is sent. Changed keys are
    rejected by paramiko (HostKeyChanged)."""

    def __init__(self, known_hosts: Path):
        self._path = known_hosts

    def missing_host_key(self, client, hostname, key):
        keys = client.get_host_keys()
        if hostname != USB_HOST:
            pinned = keys.lookup(USB_HOST) or {}
            same = pinned.get(key.get_name())
            if same is None or same.asbytes() != key.asbytes():
                raise UnknownHostKey(hostname, key)
        keys.add(hostname, key.get_name(), key)
        _save_known_hosts(keys, self._path)


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


PRIVATE_TMP_SUFFIX = ".rmdesk-tmp"


def write_private_remote(device: "Device", path: str, data: bytes) -> None:
    """Write `path` on the tablet so it is 0600 from its first byte: create an empty 0600
    file beside it (umask 077), fill it over SFTP (that keeps the mode), then rename it over
    `path` in one step. For files holding secrets (passwords, keys, settings backups)."""
    import shlex
    tmp = path + PRIVATE_TMP_SUFFIX
    q = shlex.quote(tmp)
    res = device.run(f"umask 077 && rm -f -- {q} && : > {q}")
    if res.code != 0:
        raise DeviceError(f"could not create {tmp}: {(res.err or res.out).strip()}")
    device.write_bytes(tmp, data)
    device.rename(tmp, path)


def bridge_channel(chan) -> socket.socket:
    """Return a socket whose bytes go to and come from `chan` (an SSH channel, or anything
    with recv/sendall/close). Two daemon threads copy between them; when either side ends,
    both are closed."""
    mine, theirs = socket.socketpair()
    done = threading.Event()

    def finish() -> None:
        if done.is_set():
            return
        done.set()
        for close in (chan.close, theirs.close):
            try:
                close()
            except Exception:
                pass

    def down() -> None:  # tablet -> this computer
        try:
            while True:
                data = chan.recv(65536)
                if not data:
                    break
                theirs.sendall(data)
        except Exception:
            pass
        finally:
            try:
                theirs.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            finish()

    def up() -> None:  # this computer -> tablet
        try:
            while True:
                data = theirs.recv(65536)
                if not data:
                    break
                chan.sendall(data)
        except Exception:
            pass
        finally:
            finish()

    for fn, name in ((down, "rmdesk-tunnel-down"), (up, "rmdesk-tunnel-up")):
        threading.Thread(target=fn, name=name, daemon=True).start()
    return mine


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
        _private_dir(KNOWN_HOSTS.parent)
        if KNOWN_HOSTS.exists():
            try:
                os.chmod(KNOWN_HOSTS, 0o600)  # files saved by older versions were 0644
            except OSError:
                pass
            self._client.load_host_keys(str(KNOWN_HOSTS))
        self._client.set_missing_host_key_policy(_TrustOnFirstUse(KNOWN_HOSTS))
        try:
            self._client.connect(
                host, username="root", password=password, key_filename=key_filename,
                timeout=10, look_for_keys=False, allow_agent=True,
            )
        except UnknownHostKey:
            self._client.close()
            raise
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

    def open_tunnel(self, port: int, host: str = "127.0.0.1",
                    timeout: float = 10) -> socket.socket:
        """A socket connected to host:port as seen from the tablet, carried inside this SSH
        connection (whose host key was checked). Nothing listens on this computer: the
        socket is one end of a private socket pair, bridged to an SSH direct-tcpip channel."""
        with self._lock:
            try:
                chan = self._client.get_transport().open_channel(
                    "direct-tcpip", (host, port), ("127.0.0.1", 0), timeout=timeout)
            except (paramiko.SSHException, OSError, AttributeError, EOFError) as e:
                raise DeviceError(f"tunnel to {host}:{port} on the tablet: {e}") from e
        return bridge_channel(chan)

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
