"""Connection manager: USB first, then Wi-Fi, with reconnect backoff."""
from __future__ import annotations

import enum
import logging
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Literal, Protocol

from rmdesk import firmware, safety
from rmdesk.config import Config
from rmdesk.device import Device, DeviceError, HostKeyChanged, SshDevice

USB_HOST = "10.11.99.1"
KEYRING_SERVICE = "rmdesk"
AUTHORIZED_KEYS = "/home/root/.ssh/authorized_keys"
BUSY_MSG = "The tablet is busy with: {}. Try again when it's done."
SCREEN_CHECK_JOB = "checking the tablet's screen"
SCREEN_RESTARTED = "The tablet's screen was stopped; rmdesk restarted it."
SCREEN_NOT_RESTARTED = ("The tablet's screen is stopped and rmdesk couldn't restart it. "
                        "The tablet's screen may need a restart (hold the power button).")

log = logging.getLogger(__name__)


def _in_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True, name="rmdesk-deferred").start()


class JobBusy(Exception):
    """Another job that stops or restarts xochitl is running."""

    def __init__(self, current: str | None):
        super().__init__(BUSY_MSG.format(current))
        self.current = current


class State(enum.Enum):
    OFFLINE = "offline"
    CONNECTING = "connecting"
    USB = "usb"
    WIFI = "wifi"


class SecretStore(Protocol):
    def get(self, device_id: str) -> str | None: ...
    def set(self, device_id: str, password: str) -> None: ...


class KeyringSecrets:
    def get(self, device_id: str) -> str | None:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, device_id)

    def set(self, device_id: str, password: str) -> None:
        import keyring
        keyring.set_password(KEYRING_SERVICE, device_id, password)


def default_factory(host: str, password: str | None) -> Device:
    key = None
    for name in ("id_ed25519", "id_rsa"):
        p = Path.home() / ".ssh" / name
        if p.exists():
            key = str(p)
            break
    return SshDevice(host, password, key_filename=key)


class Connection:
    def __init__(self, cfg: Config,
                 factory: Callable[[str, str | None], Device] = default_factory,
                 secrets: SecretStore | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 persist: Callable[[Config], None] | None = None,
                 defer: Callable[[Callable[[], None]], None] = _in_thread):
        self.cfg = cfg
        self._defer = defer  # runs a reconnect put off until a job ended (off the GUI thread)
        self._factory = factory
        self._secrets = secrets if secrets is not None else KeyringSecrets()
        self._sleep = sleep
        self.persist = persist
        self.device: Device | None = None
        self.state = State.OFFLINE
        self.transport: Literal["usb", "wifi"] | None = None
        self.on_state: list[Callable[[State], None]] = []
        self.on_job: list[Callable[[], None]] = []  # job started/ended (any thread)
        self.last_error: Exception | None = None
        self.host_errors: dict[str, Exception] = {}
        self.info: firmware.FirmwareInfo | None = None
        self._password: str | None = None
        self._failures = 0
        self._job_lock = threading.Lock()
        self._job: str | None = None
        self.pending_refresh = 0
        self.on_pending: list[Callable[[int], None]] = []
        # while a job holds the slot its device is never closed: a reconnect or a lost
        # link reported meanwhile (by another view) waits for end_job
        self._deferred_connect = False
        self._deferred_lost: Exception | None = None
        # something the user should know about the last connect (e.g. SCREEN_RESTARTED)
        self.notice: str | None = None
        self.on_notice: list[Callable[[str], None]] = []
        # whether the last connect had a password to try (typed or from the keyring); None
        # before the first connect
        self.password_used: bool | None = None

    # -- one job at a time that stops or restarts xochitl (uploads, trash, notebook
    # edits, the web interface switch, stream install): one job's restart must not
    # land between another's stop and start --
    @property
    def current_job(self) -> str | None:
        with self._job_lock:
            return self._job

    def try_begin_job(self, name: str) -> bool:
        """Claim the device job slot; False (without waiting) if it is taken."""
        with self._job_lock:
            if self._job is not None:
                return False
            self._job = name
        self._notify_job()
        return True

    def _notify_job(self) -> None:
        for cb in list(self.on_job):
            try:
                cb()
            except Exception as e:
                self.last_error = e

    def end_job(self) -> None:
        with self._job_lock:
            self._job = None
            reconnect, self._deferred_connect = self._deferred_connect, False
            lost, self._deferred_lost = self._deferred_lost, None
        self._notify_job()
        if reconnect:
            self._defer(self.connect)  # connect closes the old device first
        elif lost is not None:
            self._mark_lost(lost)  # already reported (and seen by spies) once

    @contextmanager
    def job(self, name: str):
        if not self.try_begin_job(name):
            raise JobBusy(self.current_job)
        try:
            yield
        finally:
            self.end_job()

    # -- changes written to the tablet that its screen shows only after a restart --
    def _notify_pending(self) -> None:
        for cb in list(self.on_pending):
            try:
                cb(self.pending_refresh)
            except Exception as e:
                self.last_error = e

    def add_pending(self, n: int) -> None:
        if n > 0:
            self.pending_refresh += n
            self._notify_pending()

    def clear_pending(self) -> None:
        if self.pending_refresh:
            self.pending_refresh = 0
            self._notify_pending()

    def set_password(self, password: str) -> None:
        self._password = password

    def _set_state(self, state: State) -> None:
        self.state = state
        for cb in list(self.on_state):
            try:
                cb(state)
            except Exception as e:  # one bad listener must not break the others
                self.last_error = e

    def _lookup_password(self) -> str | None:
        if self._password is not None:
            return self._password
        if self.cfg.serial:
            try:
                return self._secrets.get(self.cfg.serial)
            except Exception as e:  # keyring backend problems must not block key auth
                self.last_error = e
        return None

    def connect(self) -> State:
        """Connect (closing any current link). While a job holds the slot and uses a device,
        the reconnect is put off until end_job: the job's device must stay open."""
        with self._job_lock:
            if self._job is not None and self.device is not None:
                self._deferred_connect = True
                return self.state
        self.notice = None
        self.disconnect(notify=False)
        self.last_error = None
        self.host_errors = {}
        self._set_state(State.CONNECTING)
        password = self._lookup_password()
        self.password_used = password is not None
        hosts: list[tuple[str, Literal["usb", "wifi"]]] = [(USB_HOST, "usb")]
        if self.cfg.wifi_ip:
            hosts.append((self.cfg.wifi_ip, "wifi"))
        for host, transport in hosts:
            try:
                device = self._factory(host, password)
            except HostKeyChanged as e:
                self.last_error = self.host_errors[host] = e
                break
            except Exception as e:
                self.last_error = self.host_errors[host] = e
                continue
            self.device = device
            self.transport = transport
            self.last_error = None
            self._failures = 0
            try:
                self._after_connect(device, password)
            except Exception as e:
                self.last_error = e
            self._check_screen(device)
            if self.device is not device:  # the link was reported lost during the check
                return self.state
            self._set_state(State.USB if transport == "usb" else State.WIFI)
            return self.state
        self._failures += 1
        self._set_state(State.OFFLINE)
        return self.state

    def _after_connect(self, device: Device, password: str | None) -> None:
        try:
            self.info = firmware.detect(device)
        except Exception as e:
            self.info = None
            self.last_error = e
            return
        serial = self.info.serial
        if not serial:
            return
        if password is not None and self._password is not None:
            try:
                self._secrets.set(serial, password)
            except Exception as e:
                self.last_error = e
        if self.cfg.serial != serial:
            self.cfg.serial = serial
            if self.persist:
                try:
                    self.persist(self.cfg)
                except Exception as e:
                    self.last_error = e

    def _check_screen(self, device: Device) -> None:
        """No job running: if xochitl is not active (e.g. a send lost its link while the
        screen was stopped), start it (with the goMarkableStream refresh) and tell the user.
        Never fails the connect."""
        if not self.try_begin_job(SCREEN_CHECK_JOB):
            return  # a job owns xochitl now (this may be its own reconnect)
        try:
            try:
                state = device.run("systemctl is-active xochitl").out.strip()
            except Exception as e:
                log.warning("could not check xochitl after connecting: %s", e)
                return
            if state == "active":
                return
            log.warning("xochitl was %r after connecting; starting it", state)
            try:
                safety.start_xochitl(device, sleep=self._sleep)
            except Exception as e:
                log.warning("starting xochitl after connecting failed: %s", e)
                self._tell(SCREEN_NOT_RESTARTED)
            else:
                self._tell(SCREEN_RESTARTED)
        finally:
            self.end_job()

    def _tell(self, notice: str) -> None:
        self.notice = notice
        for cb in list(self.on_notice):
            try:
                cb(notice)
            except Exception as e:
                self.last_error = e

    def disconnect(self, notify: bool = True) -> None:
        if self.device is not None:
            try:
                self.device.close()
            except Exception:
                pass
        self.device = None
        self.transport = None
        if notify:
            self._set_state(State.OFFLINE)
        else:
            self.state = State.OFFLINE

    def connection_lost(self, error: Exception) -> None:
        """Mark the link dead after an operation on the device failed.

        While a job holds the slot, the device it uses stays open: the close is put off
        until end_job (the job's own failures reach it anyway)."""
        with self._job_lock:
            if self._job is not None:
                if self._deferred_lost is None:
                    self._deferred_lost = error
                return
        self._mark_lost(error)

    def _mark_lost(self, error: Exception) -> None:
        if self.device is not None:
            try:
                self.device.close()
            except Exception:
                pass
        self.device = None
        self.transport = None
        self.info = None
        self._failures += 1
        self.last_error = error
        self._set_state(State.OFFLINE)

    def ensure(self) -> Device:
        """Return a live device, reconnecting with backoff if needed.

        Callers must call connection_lost(e) when an operation on the returned
        device raises DeviceError, so the state and backoff stay accurate.
        """
        if self.device is not None:
            return self.device
        if isinstance(self.last_error, HostKeyChanged):
            raise self.last_error
        if self._failures:
            self._sleep(min(2 ** (self._failures - 1), 30))
        self.connect()
        if self.device is None:
            raise DeviceError("tablet is offline") from self.last_error
        return self.device


def install_ssh_key(device: Device, pubkey_path: Path) -> None:
    key = pubkey_path.read_text(encoding="utf-8").strip()
    current = ""
    if device.exists(AUTHORIZED_KEYS):
        # a read failure must propagate: never overwrite keys we could not read
        current = device.read_bytes(AUTHORIZED_KEYS).decode("utf-8", "replace")
    if key in (line.strip() for line in current.splitlines()):
        return
    device.run("mkdir -p -m 700 /home/root/.ssh")
    if current and not current.endswith("\n"):
        current += "\n"
    device.write_bytes(AUTHORIZED_KEYS, (current + key + "\n").encode("utf-8"))
    device.run(f"chmod 600 {AUTHORIZED_KEYS}")
