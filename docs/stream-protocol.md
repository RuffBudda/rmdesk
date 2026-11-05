# goMarkableStream v1.4.0 — stream protocol

Pinned from the upstream source at tag **v1.4.0** (released 2026-08-21),
`https://github.com/owulveryck/goMarkableStream/tree/v1.4.0`. Every claim below
cites `file:line` at that tag. This is what `rmdesk.stream.client` implements.

> Note: the README at v1.4.0 still says full frames are *gzip*-compressed
> (README.md:341, 347) and that `/events` is a *WebSocket* (README.md:281). The
> code says otherwise: full frames are **zstd** and `/events` is **Server-Sent
> Events**. The code wins; details below.

## Server and transport

| Item | Value | Source |
|---|---|---|
| Bind address | `:2001` (env `RK_SERVER_BIND_ADDR`) | main.go:30 |
| TLS | on by default (`RK_HTTPS=true`) | main.go:33, main.go:244-266, http.go:303-314 |
| Certificate | self-signed: auto-generated per device into `/home/root/.config/goMarkableStream/certs`, falling back to the embedded `assets/cert.pem` | main.go:41-42, http.go:316-338 |
| Stream endpoint | `GET /stream?rate=<ms>` | http.go:45-46, internal/stream/handler.go:87-107 |
| Transport | one long-lived HTTP/1.1 response, `Transfer-Encoding: chunked`, `Content-Type: application/octet-stream`; messages are written back-to-back and flushed per frame | internal/stream/handler.go:169-172, 265, 274 |
| Concurrency | **one** streaming client at a time; a second gets `429 Too Many Requests` | internal/stream/mdw.go:57-62, internal/stream/handler.go:121-125 |
| `rate` | default 200 ms; adaptive between 50 and 1000 ms depending on frame size | internal/stream/handler.go:21, 281-304 |

HTTP chunk boundaries carry no meaning: the client must reassemble messages
from the 4-byte headers below (client/worker_stream_processing.js:148-171 keeps a
pending buffer across chunks for exactly this reason).

### When frames are sent

The server does not send frames continuously. It starts in "writing" mode, and
stops after **2 s** without qualifying input; it resumes on a touch event or a
pen event with pressure > 100, and keeps going 300 ms after pen lift
(internal/stream/handler.go:24-27, 152-229). While paused, **nothing** is
written on the connection (no keep-alive), so a client must not treat a silent
connection as dead — no read timeout on the stream.

Every new connection resets the encoder, so the **first message of each
connection is a full frame** (internal/stream/handler.go:128-129,
internal/delta/delta.go:167-177).

## Authentication

v1.4.0 uses **JWT bearer tokens**, not HTTP Basic auth.

1. `POST /login` with body `{"username": "...", "password": "..."}`
   (`Content-Type: application/json`). Credentials are `RK_SERVER_USERNAME`
   (default `admin`) and `RK_SERVER_PASSWORD` (default `password`)
   (main.go:31-32, http.go:208-258, auth.go:80-90; client/auth.js:43-60).
2. Response `200` `{"token": "<jwt>", "expiresIn": <seconds>}`
   (http.go:252-256). `401 {"error": "Invalid credentials"}` on bad creds
   (http.go:234-239). Default lifetime 24 h (main.go:60).
3. Every protected request sends `Authorization: Bearer <jwt>`
   (auth.go:51-60; client/worker_stream_processing.js:50-57). SSE endpoints can
   instead pass `?token=<jwt>` (auth.go:62-69; client/worker_event_processing.js:47-53).
4. Anything else on a protected path gets `401 Unauthorized` (auth.go:71-73).
   Public paths: `/`, `/login`, static assets, `/lib/` (auth.go:13-38). The
   middleware wraps the whole mux (main.go:233).

`tools/raw_client/client.go:29` still calls `SetBasicAuth`, but nothing in the
v1.4.0 server reads Basic credentials (auth.go:43-75 checks only the bearer
header and the `token` query parameter); the "falling back to Basic Auth" log
line (main.go:166) is stale — with JWT disabled every protected path is 401.

The env file `/home/root/.config/goMarkableStream/env` is created by
`goMarkableStream install` with every variable commented out
(install.go:14, 18-62, 132-143) and is loaded by the unit through
`EnvironmentFile=` (install.go:72).

## Message framing

Each message is a 4-byte header followed by `payload_len` bytes:

```
offset 0      : frame type (uint8)
offset 1..3   : payload_len, uint24 little-endian
offset 4..    : payload
```

(internal/delta/delta.go:586-591, 666-669; decoded at
client/worker_stream_processing.js:156-171.)

Frame types (internal/delta/delta.go:60-66, client/worker_stream_processing.js:14-18):

| Type | Name | Payload | Sent by v1.4.0? |
|---|---|---|---|
| `0x00` | `FrameTypeFull` | raw full frame | no (deprecated) |
| `0x01` | `FrameTypeDelta` | uncompressed run stream | yes |
| `0x02` | `FrameTypeFullCompressed` | gzip full frame | no (legacy; JS still decodes it) |
| `0x03` | `FrameTypeFullZstd` | zstd full frame | **yes — every full frame** (delta.go:586) |
| `0x04` | `FrameTypeDeltaZstd` | zstd-compressed run stream | yes |

* **Full frame (`0x03`)**: payload is one zstd frame (`EncodeAll`,
  delta.go:572) that decompresses to the whole framebuffer, exactly
  `width × height × 4` bytes. The JS rejects any other size
  (client/worker_stream_processing.js:231-234). Sent on the first frame of a
  connection, when the frame size changes, when the changed-byte ratio exceeds
  `RK_DELTA_THRESHOLD` (default 0.30), or when the delta would be ≥2× the last
  full frame and >32 KiB (delta.go:167-177, 196-205, 221-225, 81-82).
* **Delta (`0x01`)**: payload is a run stream (below). An **empty** delta
  (`01 00 00 00`, payload_len 0) is sent when nothing changed
  (delta.go:189-193, 609-611; asserted by internal/delta/delta_test.go:226-264).
* **Compressed delta (`0x04`)**: payload is a zstd frame whose decompressed
  bytes are a run stream identical to a `0x01` payload. Used only when the run
  stream is ≥1024 bytes and compression actually shrinks it
  (delta.go:74, 212-214, 616-650; client/worker_stream_processing.js:181-190).

## Delta run layout

A run stream is a sequence of runs, each a header followed by pixel bytes
(delta.go:656-691; decoder client/worker_stream_processing.js:247-302;
reference Go decoder internal/delta/roundtrip_test.go:69-103):

```
short run (bit 7 of first byte clear):
  [len: uint8, 0..127 pixels] [offset: uint16 LE] [len*4 bytes of BGRA]
long run (bit 7 of first byte set):
  [0x80 | len>>8] [len & 0xFF]  (len: 15 bits, pixels)
  [offset: uint24 LE] [len*4 bytes of BGRA]
```

* The encoder uses the short form when `offset ≤ 0xFFFF` and `len ≤ 127`
  (delta.go:87-89, 673), else the long form.
* **`offset` is in bytes**, relative to the *end of the previous run* (or to
  frame start for the first run) (delta.go:138, 355-360; JS
  `frameOffset += relativeOffset` then `frameOffset += len*4`,
  client/worker_stream_processing.js:283-294).
* **`len` is in pixels** (4 bytes each) (delta.go:139, 357).
* The server detects change in 128-byte blocks (32 pixels), so runs start on
  block boundaries and cover whole blocks (delta.go:320-404); a decoder must
  not rely on that.
* Concrete examples from upstream tests:
  * short run, offset 100, 10 pixels: run bytes `0a 64 00` + 40 bytes
    (internal/delta/delta_test.go:293-334);
  * long run, offset 70000, 200 pixels: run bytes `80 c8 70 11 01` + 800
    bytes (internal/delta/delta_test.go:336-375).

## Pixel format and frame size (reMarkable 2, firmware ≥ 3.24)

| Item | Value | Source |
|---|---|---|
| Width × height | **1404 × 1872** (portrait, row-major, row 0 = top of the screen) | internal/remarkable/detect.go:27-31, 95-105 |
| Bytes per pixel | 4 | internal/remarkable/config.go:4-6, delta.go:91-97 |
| Byte order | **B, G, R, A** | internal/stream/screenshot.go:47-58; client/glCanvas.js:54-58 (`.bgra` swizzle) |
| Frame size | 1404 × 1872 × 4 = **10 512 768** bytes | detect.go:101 |
| Orientation | as-is: the "flipped" texture coordinates used for 3.24+ map row 0 to the top of the canvas with no mirroring | client/glCanvas.js:219-233 |

The alpha byte is not meaningful for display (the screenshot handler forces
alpha 255, screenshot.go:58). rmdesk converts to gray with
`(77·R + 150·G + 29·B) >> 8`.

Firmware < 3.24 uses a gray16, 1872 × 1404 × 2 framebuffer (detect.go:17-19,
106-116); rmdesk's decoder only supports the 3.24+ layout and rejects a full
frame of any other size.

## Pen position: `/events` (for the red dot, Task 8)

* `GET /events` — **Server-Sent Events** (`Content-Type: text/event-stream`),
  despite the README calling it a WebSocket (internal/eventhttphandler/pen_handler.go:43-46).
  Auth: `Authorization: Bearer` or `?token=<jwt>` (auth.go:51-69).
* Only pen `EV_ABS` events are sent (pen_handler.go:33-39), and only while the
  pen is **hovering** — pressure ≤ 100; while drawing they are suppressed
  (pen_handler.go:12-16, 63-71).
* Each event is one SSE message: `data: <json>\n\n` (pen_handler.go:81-84) with
  JSON `{"Source": 1, "Type": 3, "Code": <n>, "Value": <n>}` — the `Time`
  field is omitted (`json:"-"`) (internal/events/events.go:41-61).
* Codes (Linux input ABS codes): `0` ABS_X, `1` ABS_Y, `24` ABS_PRESSURE,
  `25` ABS_DISTANCE. The web client hides the dot on code 24 and shows it on
  code 25 (client/worker_event_processing.js:57-66).
* RM2 axis ranges: `MaxXValue = 15725` (code 1), `MaxYValue = 20966` (code 0)
  (internal/remarkable/const.go:17-20). The web client's RM2 mapping for its
  default (non-rotated) view of the 1404 × 1872 frame is
  (client/worker_event_processing.js:96-101):

  ```
  x_px = Value(code 1) / 15725 * 1404
  y_px = 1872 - Value(code 0) / 20966 * 1872
  ```

  Verify this mapping against the real tablet in the device session.

## Other endpoints

`/version` (plain text, http.go:69-77), `/screenshot` (PNG of the current
framebuffer, BGRA → RGBA, internal/stream/screenshot.go:29-72 — useful for
capturing `expected_final.png`), `/gestures` (http.go:62-63).

## Install facts (for `rmdesk.stream.install`)

* Release assets at v1.4.0 (GitHub API `releases/tags/v1.4.0`):
  `gomarkablestream-RM2`, `gomarkablestream-RM2-lite`, `gomarkablestream-RMPRO`,
  `gomarkablestream-RMPRO-lite`, `checksums.txt` (sha256, `sha  name` lines).
  Plain binaries, not archives (.goreleaser.yml:59-64; README.md:79-93). The
  RM2 builds are GOARCH=arm GOARM=7 (.goreleaser.yml:25-31, 50-56). rmdesk
  uses `gomarkablestream-RM2-lite` (no Tailscale; 10.4 MB vs 28.5 MB).
* `goMarkableStream install` writes `/etc/systemd/system/goMarkableStream.service`
  with `ExecStart=<resolved path of the running binary>` and
  `EnvironmentFile=/home/root/.config/goMarkableStream/env`, creates the env
  file if missing (never overwrites either file), then runs
  `systemctl daemon-reload`, `enable`, `start` (install.go:12-16, 64-92, 94-189).
* Firmware compatibility (README.md:36-43): ≥ 3.24 → latest;
  3.6 – < 3.24 → ≥ 0.11.0 ("may work, but not actively tested");
  3.4 – < 3.6 → ≥ 0.8.6 and < 0.11.0; < 3.4 → < 0.8.6.

## Capturing real fixtures (device session)

`tests/test_stream.py::test_decoder_real_capture` runs once
`tests/fixtures/stream/` holds `msg_000.bin`, `msg_001.bin`, … (one complete
wire message each — header included — the first being the `0x03` full frame,
then ≥ 20 deltas) and `expected_final.png` (taken from `GET /screenshot` at the
end of the capture). Until then it is skipped; the decoder is tested against
`tests/stream_fixtures.py`, an encoder written from this document, and against
byte sequences copied from upstream's `internal/delta/delta_test.go`.

## rmdesk's install order and stored credentials

`rmdesk.stream.install.install` writes the env file (mkdir -p, temp file,
`chmod 600`, rename) with a fresh random `RK_SERVER_PASSWORD` **before** running
`goMarkableStream install`, because that subcommand starts the service and
skips an env file that already exists (install.go:133-135) — so the service
never runs with the default `admin`/`password`. The keyring entry (service
`rmdesk-stream`, username = tablet serial) holds `"<username>:<password>"`,
where the username is the env file's `RK_SERVER_USERNAME` (default `admin`);
read it back with `install.stream_credentials(serial)`.

## rmdesk's answers to input-driven frames and the one-client limit

* `StreamClient.resync()` shuts down the current connection's socket; the loop
  reconnects at once (no `retry_delay`, token kept) and the new connection's
  first message is a full frame.
* Auto catch-up (`rmdesk.stream.catchup.CatchUp`, Qt-free; the Screen view
  drives it with a single-shot `QTimer`): 2.5 s after the last frame, one
  resync. The full frame it produces does not schedule another; the next frame
  after it starts a new burst.
* "⟳ Refresh" (Screen toolbar, R in Present) resyncs once. "Keep refreshing"
  (toolbar toggle, K in Present, off by default) resyncs every 1 s while the
  stream runs — keyboard (Type Folio) input produces no frames at all — and
  skips a tick while the previous resync hasn't produced a frame yet.
* HTTP 429 (another `/stream` client) is shown as "Another viewer is connected
  to the tablet's stream (close other rmdesk windows or the browser stream
  page)."; the client keeps retrying with its normal delay.
