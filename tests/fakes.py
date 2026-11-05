"""In-memory Device for tests."""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Callable

from rmdesk import battery
from rmdesk.device import CommandResult, DeviceError


@dataclass
class FailRule:
    """A conditional failure for FakeDevice.fail_on.

    Fails a call when `match` (if given) is a substring of the call's argument,
    for the first `times` matching calls (None = every matching call).
    """
    exc: Exception
    match: str | None = None
    times: int | None = None
    hits: int = 0

    def check(self, arg: str) -> None:
        if self.match is not None and self.match not in arg:
            return
        if self.times is not None and self.hits >= self.times:
            return
        self.hits += 1
        raise self.exc


class FakeDevice:
    """In-memory device. A fail_on value is an Exception (every call fails) or
    a FailRule. A run_handler may return None to fall back to the built-in
    emulation of systemctl for xochitl (stop/start/restart/is-active, try-restart of other units as a no-op, with a
    modelled running state in `xochitl_active`), `cat <file>`, `rm -f` and
    `find <dir> -type f`, and battery.READ_CMD (from `files` under /sys/class/power_supply)."""

    def __init__(self, files: dict[str, bytes] | None = None,
                 fail_on: dict[str, Exception] | None = None,
                 run_handler: Callable[[str], CommandResult] | None = None):
        self.files: dict[str, bytes] = dict(files or {})
        self.mtimes: dict[str, float] = {}
        self.fail_on = fail_on or {}
        self.run_handler = run_handler
        self.log: list[tuple[str, str]] = []
        self._clock = 0.0
        self.xochitl_active = True

    def _call(self, method: str, arg: str) -> None:
        self.log.append((method, arg))
        rule = self.fail_on.get(method)
        if isinstance(rule, FailRule):
            rule.check(arg)
        elif rule is not None:
            raise rule

    def run(self, cmd: str, timeout: float = 30) -> CommandResult:
        self._call("run", cmd)
        if self.run_handler:
            res = self.run_handler(cmd)
            if res is not None:
                return res
        return self._builtin(cmd)

    def _battery(self) -> CommandResult:
        """battery.READ_CMD: cat the first power supply whose `type` file says Battery."""
        root = battery.POWER_SUPPLY + "/"
        dirs = sorted({p[len(root):].split("/")[0] for p in self.files if p.startswith(root)})
        for d in dirs:
            base = root + d
            if self.files.get(base + "/type", b"").decode("utf-8", "replace").strip() != "Battery":
                continue
            out, code = "", 0
            for name in ("capacity", "status"):
                data = self.files.get(f"{base}/{name}")
                if data is None:
                    code = 1
                else:
                    out += data.decode("utf-8", "replace")
            return CommandResult(code, out, "")
        return CommandResult(1, "", "")  # the loop's last test failed

    def _builtin(self, cmd: str) -> CommandResult:
        if cmd == battery.READ_CMD:
            return self._battery()
        argv = shlex.split(cmd)
        if argv[:1] == ["systemctl"] and argv[2:] == ["xochitl"]:
            verb = argv[1]
            if verb == "is-active":
                if self.xochitl_active:
                    return CommandResult(0, "active\n", "")
                return CommandResult(3, "inactive\n", "")
            if verb in ("start", "restart"):
                self.xochitl_active = True
            elif verb == "stop":
                self.xochitl_active = False
            return CommandResult(0, "", "")
        if argv[:2] == ["systemctl", "try-restart"] and len(argv) == 3:
            return CommandResult(0, "", "")  # no-op unless the unit runs
        if len(argv) == 2 and argv[0] == "cat":
            if argv[1] not in self.files:
                return CommandResult(1, "", f"cat: {argv[1]}: No such file or directory")
            return CommandResult(0, self.files[argv[1]].decode("utf-8", "replace"), "")
        if argv[:2] == ["rm", "-f"]:
            for p in argv[2:]:
                if p != "--":
                    self.files.pop(p, None)
                    self.mtimes.pop(p, None)
            return CommandResult(0, "", "")
        if len(argv) == 4 and argv[0] == "find" and argv[2:] == ["-type", "f"]:
            prefix = argv[1].rstrip("/") + "/"
            found = sorted(p for p in self.files if p.startswith(prefix))
            if not found:
                return CommandResult(1, "", f"find: {argv[1]}: No such file or directory")
            return CommandResult(0, "".join(p + "\n" for p in found), "")
        return CommandResult(0, "", "")

    def read_bytes(self, path: str) -> bytes:
        self._call("read_bytes", path)
        if path not in self.files:
            raise DeviceError(f"no such file: {path}")
        return self.files[path]

    def write_bytes(self, path: str, data: bytes) -> None:
        self._call("write_bytes", path)
        self.files[path] = data
        self._clock += 1
        self.mtimes[path] = self._clock

    def rename(self, src: str, dst: str) -> None:
        self._call("rename", f"{src} -> {dst}")
        if src not in self.files:
            raise DeviceError(f"no such file: {src}")
        self.files[dst] = self.files.pop(src)
        self.mtimes[dst] = self.mtimes.pop(src, self._clock)

    def listdir(self, path: str) -> list[str]:
        self._call("listdir", path)
        prefix = path.rstrip("/") + "/"
        names = {p[len(prefix):].split("/")[0] for p in self.files if p.startswith(prefix)}
        if not names:
            raise DeviceError(f"no such directory: {path}")
        return sorted(names)

    def exists(self, path: str) -> bool:
        self._call("exists", path)
        prefix = path.rstrip("/") + "/"
        return path in self.files or any(p.startswith(prefix) for p in self.files)

    def stat_mtime(self, path: str) -> float:
        self._call("stat_mtime", path)
        if path not in self.files:
            raise DeviceError(f"no such file: {path}")
        return self.mtimes.get(path, 0.0)

    def close(self) -> None:
        self._call("close", "")
