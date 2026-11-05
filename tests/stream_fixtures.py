"""A goMarkableStream v1.4.0 wire *encoder* for tests (see docs/stream-protocol.md).

Written from the protocol doc, independently of rmdesk.stream.client: it works
per pixel (the server works in 128-byte blocks), splits long runs, and chooses
between 0x01/0x04 deltas and 0x03 full frames the way upstream does
(internal/delta/delta.go). Images are H×W uint8 gray; each becomes BGRA with
B = G = R = gray, A = 255.
"""
from __future__ import annotations

import struct

import numpy as np

try:  # Python 3.14+
    from compression import zstd as _zstd

    def zstd_compress(data: bytes) -> bytes:
        return _zstd.compress(data)
except ImportError:  # pragma: no cover
    import zstandard as _zstandard

    def zstd_compress(data: bytes) -> bytes:
        return _zstandard.ZstdCompressor().compress(data)

FULL_ZSTD = 0x03
DELTA = 0x01
DELTA_ZSTD = 0x04
MAX_SHORT_OFFSET = 0xFFFF
MAX_SHORT_LEN = 127
MAX_LONG_LEN = 0x7FFF
MAX_LONG_OFFSET = 0xFFFFFF
MIN_DELTA_COMPRESS = 1024


def to_bgra(gray: np.ndarray) -> bytes:
    h, w = gray.shape
    out = np.empty((h, w, 4), dtype=np.uint8)
    out[..., 0] = out[..., 1] = out[..., 2] = gray
    out[..., 3] = 255
    return out.tobytes()


def header(frame_type: int, payload_len: int) -> bytes:
    assert payload_len < 1 << 24
    return bytes([frame_type]) + payload_len.to_bytes(3, "little")


def full_frame(bgra: bytes) -> bytes:
    payload = zstd_compress(bgra)
    return header(FULL_ZSTD, len(payload)) + payload


def encode_run(rel_offset: int, pixels: bytes) -> bytes:
    """One run; rel_offset in bytes since the previous run's end, pixels = BGRA bytes."""
    n = len(pixels) // 4
    assert n * 4 == len(pixels) and 0 < n <= MAX_LONG_LEN and 0 <= rel_offset <= MAX_LONG_OFFSET
    if rel_offset <= MAX_SHORT_OFFSET and n <= MAX_SHORT_LEN:
        return bytes([n]) + struct.pack("<H", rel_offset) + pixels
    return (bytes([0x80 | (n >> 8), n & 0xFF]) + rel_offset.to_bytes(3, "little") + pixels)


def changed_spans(prev: bytes, cur: bytes) -> list[tuple[int, int]]:
    """(start_pixel, end_pixel) spans of pixels that differ."""
    a = np.frombuffer(prev, np.uint8).reshape(-1, 4)
    b = np.frombuffer(cur, np.uint8).reshape(-1, 4)
    diff = np.any(a != b, axis=1).astype(np.int8)
    edges = np.diff(np.concatenate(([0], diff, [0])))
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1)
    return list(zip(starts.tolist(), ends.tolist()))


def run_stream(prev: bytes, cur: bytes) -> bytes:
    out = bytearray()
    last_end = 0  # bytes
    for start, end in changed_spans(prev, cur):
        while start < end:
            n = min(end - start, MAX_LONG_LEN)
            rel = start * 4 - last_end
            while rel > MAX_LONG_OFFSET:  # bridge a huge gap with a 1-pixel unchanged run
                bridge = last_end + MAX_LONG_OFFSET
                bridge -= bridge % 4
                out += encode_run(bridge - last_end, cur[bridge:bridge + 4])
                last_end = bridge + 4
                rel = start * 4 - last_end
            out += encode_run(rel, cur[start * 4:(start + n) * 4])
            last_end = (start + n) * 4
            start += n
    return bytes(out)


def delta_frame(prev: bytes, cur: bytes, compress: bool = True) -> bytes:
    runs = run_stream(prev, cur)
    if compress and len(runs) >= MIN_DELTA_COMPRESS:
        z = zstd_compress(runs)
        if len(z) < len(runs):
            return header(DELTA_ZSTD, len(z)) + z
    return header(DELTA, len(runs)) + runs


def encode_session(images: list[np.ndarray], threshold: float = 0.30,
                   compress: bool = True) -> list[bytes]:
    """Messages for a stream: a full frame, then one message per later image."""
    msgs: list[bytes] = []
    prev: bytes | None = None
    for img in images:
        cur = to_bgra(img)
        if prev is None:
            msgs.append(full_frame(cur))
        else:
            changed = sum(e - s for s, e in changed_spans(prev, cur)) * 4
            if changed / len(cur) > threshold:
                msgs.append(full_frame(cur))
            else:
                msgs.append(delta_frame(prev, cur, compress))
        prev = cur
    return msgs


def synthetic_session(width: int = 1404, height: int = 1872, n_deltas: int = 20,
                      seed: int = 7) -> list[np.ndarray]:
    """A white page with strokes added one at a time, plus a page turn and an idle frame."""
    rng = np.random.default_rng(seed)
    img = np.full((height, width), 255, np.uint8)
    out = [img.copy()]
    for i in range(n_deltas):
        if i == n_deltas // 2:  # page turn: >30% changed → full frame
            img[: height * 2 // 3, :] = 200
        elif i == n_deltas // 2 + 1:  # idle: nothing changed → empty delta
            pass
        else:
            y = int(rng.integers(0, height - 8))
            ln = int(rng.integers(min(20, width // 4), max(width // 4, 2) + 1))
            x = int(rng.integers(0, width - ln + 1))
            img[y:y + 3, x:x + ln] = int(rng.integers(0, 80))
            if i % 5 == 0:  # a long horizontal stroke → long runs
                img[y + 4, :] = 0
        out.append(img.copy())
    return out
