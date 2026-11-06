"""goMarkableStream /events client: pen hover position for the Present window's red dot.

Wire format: docs/stream-protocol.md, "Pen position: /events" (Server-Sent Events,
one JSON object per `data:` line). No Qt; callbacks run on the client thread.
"""
from __future__ import annotations

import http.client
import json
import threading
from typing import Callable, Iterable, Iterator

from rmdesk.stream.client import (HEIGHT, PORT, USERNAME, WIDTH, Dial, StreamError, _Run,
                                  _tablet_context, open_connection)

ABS_X, ABS_Y, ABS_PRESSURE, ABS_DISTANCE = 0, 1, 24, 25
MAX_X, MAX_Y = 15725, 20966  # RM2 axis ranges (const.go:17-20); note the swapped axes


MAX_LINE = 1 << 16


def read_lines(resp, limit: int = MAX_LINE) -> Iterator[bytes]:
    """Lines from a file-like response; a line longer than `limit` is a StreamError."""
    while True:
        line = resp.readline(limit)
        if not line:
            return
        if len(line) >= limit and not line.endswith(b"\n"):
            raise StreamError(f"event line longer than {limit} bytes")
        yield line


def parse_sse(lines: Iterable[bytes]) -> Iterator[dict]:
    """Yield the JSON object of every `data:` line; skips comments and malformed data."""
    for raw in lines:
        line = raw.strip()
        if not line.startswith(b"data:"):
            continue
        try:
            obj = json.loads(line[5:].strip())
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


class PenTracker:
    """Folds ABS events into (x_px, y_px, visible) in the 1404x1872 frame's pixels."""

    def __init__(self, width: int = WIDTH, height: int = HEIGHT):
        self.width, self.height = width, height
        self._vx: int | None = None
        self._vy: int | None = None
        self.visible = False

    def feed(self, ev: dict) -> tuple[float, float, bool] | None:
        code, value = ev.get("Code"), ev.get("Value")
        if not isinstance(code, int) or not isinstance(value, int):
            return None
        if code == ABS_Y:  # the web client's RM2 mapping: code 1 is the horizontal axis
            self._vx = value
        elif code == ABS_X:
            self._vy = value
        elif code == ABS_DISTANCE:
            self.visible = True
        elif code == ABS_PRESSURE:
            self.visible = False
        else:
            return None
        if self._vx is None or self._vy is None:
            return None
        x = self._vx / MAX_X * self.width
        y = self.height - self._vy / MAX_Y * self.height
        return x, y, self.visible


class PenClient:
    """Reads /events on its own thread; on_pen(x_px, y_px, visible). Reconnects until stop()."""

    def __init__(self, host: str, password: str,
                 on_pen: Callable[[float, float, bool], None], *,
                 dial: Dial, port: int = PORT, username: str = USERNAME, retry_delay: float = 2.0,
                 connect_timeout: float = 5.0,
                 on_error: Callable[[Exception], None] | None = None):
        self.host, self.port = host, port
        self._username, self._password = username, password
        self.on_pen, self.on_error = on_pen, on_error
        self.retry_delay, self.connect_timeout = retry_delay, connect_timeout
        self._dial = dial
        self._ctx = _tablet_context()
        self._run: _Run | None = None

    def start(self) -> None:
        run = self._run
        if run is not None and not run.stop.is_set() and run.thread.is_alive():
            return
        if run is not None and run.thread.is_alive():
            run.thread.join(1.0)
        run = _Run()
        run.thread = threading.Thread(target=self._loop, args=(run,), name="rmdesk-pen",
                                      daemon=True)
        self._run = run
        run.thread.start()

    def stop(self, timeout: float = 0.9) -> None:
        run = self._run
        if run is None:
            return
        run.stop.set()
        run.abort()
        t = run.thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)

    def is_alive(self) -> bool:
        run = self._run
        return run is not None and run.thread is not None and run.thread.is_alive()

    def _conn(self, run: _Run) -> http.client.HTTPSConnection:
        conn = open_connection(self._dial, self.host, self.port, self.connect_timeout,
                               self._ctx)
        with run.lock:
            run.conn = conn
        if run.stop.is_set():
            raise StreamError("stopped")
        return conn

    def _track(self, run: _Run, conn) -> None:
        with run.lock:
            run.sock = conn.sock
        if run.stop.is_set():
            raise StreamError("stopped")

    def _release(self, run: _Run, conn) -> None:
        with run.lock:
            sock, run.sock = run.sock, None
            if run.conn is conn:
                run.conn = None
        conn.close()
        if sock is not None:
            sock.close()

    def _login(self, run: _Run) -> str:
        conn = self._conn(run)
        try:
            body = json.dumps({"username": self._username, "password": self._password})
            conn.request("POST", "/login", body=body,
                         headers={"Content-Type": "application/json"})
            self._track(run, conn)
            resp = conn.getresponse()
            data = resp.read(1 << 16)
            if resp.status != 200:
                raise StreamError(f"login failed: HTTP {resp.status}", resp.status)
            try:
                return json.loads(data)["token"]
            except (ValueError, KeyError, TypeError) as e:
                raise StreamError(f"login: unexpected reply: {e}") from e
        finally:
            self._release(run, conn)

    def _session(self, run: _Run) -> None:
        token = self._login(run)
        conn = self._conn(run)
        try:
            conn.request("GET", "/events", headers={"Authorization": f"Bearer {token}",
                                                    "Accept": "text/event-stream"})
            self._track(run, conn)
            sock = conn.sock
            resp = conn.getresponse()
            if resp.status != 200:
                raise StreamError(f"events: HTTP {resp.status}", resp.status)
            if sock is not None:
                sock.settimeout(None)  # a hovering-less pen means silence, not death
            tracker = PenTracker()
            for ev in parse_sse(read_lines(resp)):
                if run.stop.is_set():
                    return
                out = tracker.feed(ev)
                if out is not None:
                    self.on_pen(*out)
        finally:
            self._release(run, conn)

    def _loop(self, run: _Run) -> None:
        while not run.stop.is_set():
            try:
                self._session(run)
                raise StreamError("events ended")
            except Exception as e:
                if run.stop.is_set():
                    break
                if self.on_error is not None:
                    try:
                        self.on_error(e)
                    except Exception:
                        pass
            if run.stop.wait(self.retry_delay):
                break
