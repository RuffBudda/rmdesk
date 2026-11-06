"""goMarkableStream v1.4.0 stream client. Wire format: docs/stream-protocol.md."""
from __future__ import annotations

import http.client
import json
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Callable

import zlib

import numpy as np


# ms between frames requested from goMarkableStream; upstream's minimum is 50
DEFAULT_RATE_MS = 50


class ProtocolError(Exception):
    pass


try:  # Python 3.14+
    from compression import zstd as _zstd

    def _zstd_decompress(data: bytes, limit: int) -> bytes:
        """Decompress one zstd frame, refusing to produce more than `limit` bytes."""
        d = _zstd.ZstdDecompressor()
        out = d.decompress(data, max_length=limit + 1)
        if len(out) > limit:
            raise ProtocolError(f"zstd payload expands beyond {limit} bytes")
        if not d.eof:
            raise ProtocolError("truncated zstd payload")
        return out
except ImportError:  # pragma: no cover - older Pythons need the zstandard package
    import io

    import zstandard as _zstandard

    def _zstd_decompress(data: bytes, limit: int) -> bytes:
        reader = _zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data))
        out = reader.read(limit + 1)
        if len(out) > limit:
            raise ProtocolError(f"zstd payload expands beyond {limit} bytes")
        return out


def _gzip_decompress(data: bytes, limit: int) -> bytes:
    d = zlib.decompressobj(wbits=31)
    out = d.decompress(data, limit + 1)
    if len(out) > limit:
        raise ProtocolError(f"gzip payload expands beyond {limit} bytes")
    if not d.eof:
        raise ProtocolError("truncated gzip payload")
    return out

WIDTH, HEIGHT = 1404, 1872  # reMarkable 2, firmware >= 3.24
PORT = 2001
USERNAME = "admin"

# upstream holds its one-client lock until its handler notices our old socket closed, so the
# reconnect right after resync() can get 429 from our own previous session: retry quietly
RESYNC_429_GRACE = 3.0  # seconds after a resync
RESYNC_429_RETRY = 0.3  # seconds between those quiet retries

FULL_RAW = 0x00
DELTA = 0x01
FULL_GZIP = 0x02
FULL_ZSTD = 0x03
DELTA_ZSTD = 0x04


@dataclass
class Frame:
    width: int
    height: int
    gray: np.ndarray  # H×W uint8


def _to_gray(bgra: np.ndarray) -> np.ndarray:
    px = bgra.reshape(-1, 4).astype(np.uint16)
    return ((77 * px[:, 2] + 150 * px[:, 1] + 29 * px[:, 0]) >> 8).astype(np.uint8)


class Decoder:
    """Reassembles messages from arbitrary byte chunks and applies them to a BGRA buffer."""

    def __init__(self, width: int = WIDTH, height: int = HEIGHT):
        self.width, self.height = width, height
        self._size = width * height * 4
        # A run stream never needs more than the frame's pixel bytes plus one 3-byte
        # header per pixel; anything bigger is garbage (or a decompression bomb).
        self._max_runs_bytes = self._size + 3 * (width * height)
        self._bgra = np.zeros(self._size, np.uint8)
        self._gray = np.zeros(width * height, np.uint8)
        self._pending = bytearray()

    def feed(self, data: bytes) -> Frame | None:
        """Add bytes; apply every complete message. Returns the frame if anything was applied."""
        self._pending += data
        applied = False
        pos = 0
        buf = self._pending
        while len(buf) - pos >= 4:
            ftype = buf[pos]
            n = buf[pos + 1] | buf[pos + 2] << 8 | buf[pos + 3] << 16
            if len(buf) - pos < 4 + n:
                break
            payload = bytes(buf[pos + 4:pos + 4 + n])
            pos += 4 + n
            self._apply(ftype, payload)
            applied = True
        del self._pending[:pos]
        if not applied:
            return None
        return Frame(self.width, self.height,
                     self._gray.reshape(self.height, self.width).copy())

    def _apply(self, ftype: int, payload: bytes) -> None:
        if ftype == FULL_ZSTD:
            self._full(self._decompress(_zstd_decompress, payload, self._size))
        elif ftype == DELTA:
            self._runs(payload)
        elif ftype == DELTA_ZSTD:
            self._runs(self._decompress(_zstd_decompress, payload, self._max_runs_bytes))
        elif ftype == FULL_GZIP:
            self._full(self._decompress(_gzip_decompress, payload, self._size))
        elif ftype == FULL_RAW:
            self._full(payload)
        else:
            raise ProtocolError(f"unknown frame type 0x{ftype:02x}")

    @staticmethod
    def _decompress(fn, payload: bytes, limit: int) -> bytes:
        try:
            return fn(payload, limit)
        except ProtocolError:
            raise
        except Exception as e:
            raise ProtocolError(f"bad compressed payload: {e}") from e

    def _full(self, data: bytes) -> None:
        if len(data) != self._size:
            raise ProtocolError(f"full frame is {len(data)} bytes, expected {self._size} "
                                f"({self.width}x{self.height} BGRA)")
        self._bgra[:] = np.frombuffer(data, np.uint8)
        self._gray[:] = _to_gray(self._bgra)

    def _runs(self, payload: bytes) -> None:
        if len(payload) > self._max_runs_bytes:
            raise ProtocolError(f"delta payload of {len(payload)} bytes is too large")
        pos, offset, end = 0, 0, len(payload)
        src = np.frombuffer(payload, np.uint8)
        while pos < end:
            b0 = payload[pos]
            if b0 & 0x80 == 0:
                if pos + 3 > end:
                    raise ProtocolError("truncated short run header")
                n = b0
                rel = payload[pos + 1] | payload[pos + 2] << 8
                pos += 3
            else:
                if pos + 5 > end:
                    raise ProtocolError("truncated long run header")
                n = (b0 & 0x7F) << 8 | payload[pos + 1]
                rel = payload[pos + 2] | payload[pos + 3] << 8 | payload[pos + 4] << 16
                pos += 5
            if n == 0:  # upstream never sends empty runs; a storm of them is garbage
                raise ProtocolError("zero-length delta run")
            nbytes = n * 4
            if pos + nbytes > end:
                raise ProtocolError("delta run exceeds payload")
            offset += rel
            if offset + nbytes > self._size:
                raise ProtocolError("delta run exceeds frame bounds")
            self._bgra[offset:offset + nbytes] = src[pos:pos + nbytes]
            # recompute gray for every pixel the run touched (runs need not be pixel-aligned)
            p0, p1 = offset // 4, -(-(offset + nbytes) // 4)
            if p1 > p0:
                self._gray[p0:p1] = _to_gray(self._bgra[p0 * 4:p1 * 4])
            pos += nbytes
            offset += nbytes


class StreamError(Exception):
    def __init__(self, message: str = "", status: int | None = None):
        super().__init__(message)
        self.status = status  # HTTP status when the error is an HTTP rejection


def _tablet_context() -> ssl.SSLContext:
    """TLS context for goMarkableStream's self-signed certificate, used only over the SSH
    tunnel (Dial): SSH has already checked the tablet's host key, so the endpoint is
    authenticated there and TLS here only satisfies a server that speaks nothing else."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# dial(timeout) -> a socket connected to goMarkableStream on the tablet, e.g. through
# SshDevice.open_tunnel. The service listens on the tablet's loopback only.
Dial = Callable[[float], socket.socket]


class TunnelHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS over a socket from `dial` instead of a TCP connection to host:port."""

    def __init__(self, dial: Dial, host: str, port: int, *, timeout: float,
                 context: ssl.SSLContext):
        super().__init__(host, port, timeout=timeout, context=context)
        self._dial, self._tls = dial, context

    def connect(self) -> None:
        raw = self._dial(self.timeout)
        try:
            raw.settimeout(self.timeout)
            self.sock = self._tls.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def open_connection(dial: Dial, host: str, port: int, timeout: float,
                    ctx: ssl.SSLContext) -> http.client.HTTPSConnection:
    return TunnelHTTPSConnection(dial, host, port, timeout=timeout, context=ctx)


class _Run:
    """State of one start()..stop() cycle, so a stopped thread can't disturb its successor."""

    def __init__(self):
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.conn: http.client.HTTPSConnection | None = None
        # http.client drops conn.sock once a "Connection: close" response starts, so
        # keep our own reference for stop() and for clearing the read timeout
        self.sock: socket.socket | None = None
        self.thread: threading.Thread | None = None
        # resync(): `tracked` counts connections handed to _track; `resync_for` names the
        # one resync() cut, so only that connection's failure skips the retry delay
        self.tracked = 0
        self.resync_for: int | None = None
        self.wake = threading.Event()  # cuts a retry wait short (resync, stop)
        self.resync_at: float | None = None  # monotonic time of the last resync()

    def abort(self) -> None:
        with self.lock:
            sock = self.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)  # unblocks a read in the client thread
            except OSError:
                pass


class StreamClient:
    """Streams frames from goMarkableStream on its own thread.

    on_frame (and on_error) are called on the client thread. On any error the
    client waits retry_delay seconds and reconnects, until stop(). resync() drops the
    current connection and reconnects at once (the server starts every connection with a
    full frame), reusing the token.
    """

    def __init__(self, host: str, password: str, on_frame: Callable[[Frame], None], *,
                 dial: Dial, port: int = PORT, username: str = USERNAME, rate: int = DEFAULT_RATE_MS,
                 width: int = WIDTH, height: int = HEIGHT, retry_delay: float = 2.0,
                 connect_timeout: float = 5.0,
                 on_error: Callable[[Exception], None] | None = None):
        self.host, self.port = host, port
        self._username, self._password = username, password
        self.on_frame, self.on_error = on_frame, on_error
        self.rate, self.width, self.height = rate, width, height
        self.retry_delay = retry_delay
        self.connect_timeout = connect_timeout
        self._dial = dial
        self._ctx = _tablet_context()
        self._token: str | None = None
        self._run: _Run | None = None

    def start(self) -> None:
        run = self._run
        if run is not None and not run.stop.is_set() and run.thread.is_alive():
            return
        if run is not None and run.thread.is_alive():
            run.thread.join(1.0)  # a just-stopped thread; its own stop event stays set
        run = _Run()
        run.thread = threading.Thread(target=self._loop, args=(run,), name="rmdesk-stream",
                                      daemon=True)
        self._run = run
        run.thread.start()

    def stop(self, timeout: float = 0.9) -> None:
        run = self._run
        if run is None:
            return
        run.stop.set()
        run.wake.set()
        run.abort()
        t = run.thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)

    def resync(self) -> None:
        """Thread-safe: drop the current connection and reconnect immediately, so the next
        message is a full frame. Keeps the token. No-op when not running.

        While a connection is still being opened there is nothing to cut — its first
        message is a full frame anyway. During a retry wait, the wait is cut short."""
        run = self._run
        if run is None or run.stop.is_set() or run.thread is None or not run.thread.is_alive():
            return
        with run.lock:
            run.resync_at = time.monotonic()
            sock = run.sock
            if sock is not None:
                run.resync_for = run.tracked
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        else:
            run.wake.set()

    def is_alive(self) -> bool:
        run = self._run
        return run is not None and run.thread is not None and run.thread.is_alive()

    # -- internals (each takes the _Run it belongs to) --
    def _open(self, run: _Run) -> http.client.HTTPSConnection:
        conn = open_connection(self._dial, self.host, self.port, self.connect_timeout,
                               self._ctx)
        with run.lock:
            run.conn = conn
        if run.stop.is_set():
            raise StreamError("stopped")
        return conn

    @staticmethod
    def _track(run: _Run, conn: http.client.HTTPSConnection) -> socket.socket | None:
        sock = conn.sock
        with run.lock:
            run.sock = sock
            run.tracked += 1
        if run.stop.is_set():
            raise StreamError("stopped")
        return sock

    @staticmethod
    def _close(run: _Run, conn: http.client.HTTPSConnection) -> None:
        with run.lock:
            if run.conn is conn:
                run.conn = None
                sock, run.sock = run.sock, None
            else:
                sock = None
        conn.close()
        if sock is not None:
            sock.close()

    def _loop(self, run: _Run) -> None:
        while not run.stop.is_set():
            try:
                self._session(run)
                raise StreamError("stream ended")
            except Exception as e:
                if run.stop.is_set():
                    break
                with run.lock:
                    resynced = run.resync_for is not None and run.resync_for == run.tracked
                    run.resync_for = None
                    at = run.resync_at
                if resynced:
                    run.wake.clear()
                    continue  # resync(): reconnect now, not an error
                if (getattr(e, "status", None) == 429 and at is not None
                        and time.monotonic() - at < RESYNC_429_GRACE):
                    # most likely our own pre-resync session still holds the lock
                    if run.stop.wait(RESYNC_429_RETRY):
                        break
                    continue
                if self.on_error is not None:
                    try:
                        self.on_error(e)
                    except Exception:
                        pass
            run.wake.clear()
            run.wake.wait(self.retry_delay)
            if run.stop.is_set():
                break

    def _login(self, run: _Run) -> str:
        conn = self._open(run)
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
            self._close(run, conn)

    def _session(self, run: _Run) -> None:
        if self._token is None:
            self._token = self._login(run)
        conn = self._open(run)
        try:
            conn.request("GET", f"/stream?rate={int(self.rate)}",
                         headers={"Authorization": f"Bearer {self._token}"})
            sock = self._track(run, conn)
            resp = conn.getresponse()
            if resp.status == 401:
                self._token = None
                raise StreamError("stream: HTTP 401 (token rejected)", 401)
            if resp.status != 200:
                raise StreamError(f"stream: HTTP {resp.status}", resp.status)
            # frames stop while the tablet is idle; no read timeout on the stream itself
            if sock is not None:
                sock.settimeout(None)
            decoder = Decoder(self.width, self.height)
            while not run.stop.is_set():
                chunk = resp.read1(1 << 16)
                if not chunk:
                    return
                frame = decoder.feed(chunk)
                if frame is not None and not run.stop.is_set():
                    self.on_frame(frame)
        finally:
            self._close(run, conn)
