"""Touch and key events for the tablet's own input devices (no Qt).

The reMarkable 2 (32-bit kernel) takes `struct input_event` writes on its evdev nodes: 16 bytes,
`<llHHi` (time ignored on write). Touch goes to "pt_mt" (multitouch protocol B, slot 0, no
BTN_TOUCH), typing to the Type Folio's "rM_Keyboard" (EV_KEY 1/0 + SYN; the tablet repeats held
keys itself). Devices are found by name in /proc/bus/input/devices, never by number.

Writes leave through a QueuedWriter: one long-lived channel (`cat > /dev/input/eventN`) fed by a
worker thread from a bounded queue, so the GUI never blocks on the link.
"""
from __future__ import annotations

import queue
import re
import struct
import threading
import time
from typing import Callable, Iterable, Protocol

EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT = 0
ABS_MT_TOUCH_MAJOR, ABS_MT_TOUCH_MINOR = 0x30, 0x31
ABS_MT_POSITION_X, ABS_MT_POSITION_Y = 0x35, 0x36
ABS_MT_TRACKING_ID, ABS_MT_PRESSURE = 0x39, 0x3A
ABS_MT_SLOT = 0x2F

TOUCH_NAME = "pt_mt"
KEYBOARD_NAME = "rM_Keyboard"
PROC_DEVICES = "/proc/bus/input/devices"

TOUCH_MAX_X, TOUCH_MAX_Y = 1403, 1871
# what a real one-finger touch reports (measured on fw 3.29)
PRESSURE, TOUCH_MAJOR, TOUCH_MINOR = 89, 8, 17
# fresh ids, well above what the panel itself hands out, so the evdev core never drops ours as
# duplicates of the last value
FIRST_TRACKING_ID, LAST_TRACKING_ID = 40000, 65534
SWIPE_STEP = 0.016  # seconds between swipe moves (~60 Hz)

_EVENT = struct.Struct("<llHHi")


def pack(type_: int, code: int, value: int) -> bytes:
    return _EVENT.pack(0, 0, type_, code, value)


SYN = pack(EV_SYN, SYN_REPORT, 0)
SLOT0 = pack(EV_ABS, ABS_MT_SLOT, 0)
PINCH_STEPS = 12
PINCH_STEP = 0.025  # seconds between pinch moves (as a real pinch)
PINCH_NEAR = 150  # touch px from the centre to each finger at the narrow end of a pinch
TAP_HOLD = 0.05


def find_device(proc_text: str, name: str) -> str | None:
    """The /dev/input/eventN of the input device called `name`, from /proc/bus/input/devices."""
    for block in re.split(r"\n\s*\n", proc_text):
        m = re.search(r'^N: Name="(.*)"\s*$', block, re.M)
        if not m or m.group(1) != name:
            continue
        h = re.search(r"^H: Handlers=(.*)$", block, re.M)
        if h:
            for tok in h.group(1).split():
                if re.fullmatch(r"event\d+", tok):
                    return f"/dev/input/{tok}"
    return None


def _clamp(v: float, hi: int) -> int:
    return max(0, min(hi, round(v)))


FRAME_CX, FRAME_CY = 702.0, 936.0
# Where the tablet shows its zoom label ("3.5x"; tapping it goes back to 1x), in frame pixels:
# the top centre of the screen as shown. Measured on fw 3.29.1.0 from screenshots.
ZOOM_LABEL = {"keyboard": (1349.0, 936.0), "portrait": (704.0, 56.0)}


def frame_to_touch(fx: float, fy: float) -> tuple[int, int]:
    """Frame pixels (1404x1872, x right, y down) to pt_mt coordinates (calibrated on the tablet)."""
    return _clamp(fx, TOUCH_MAX_X), TOUCH_MAX_Y - _clamp(fy, TOUCH_MAX_Y)


# Linux keycodes, US layout (xochitl applies its own keymap, US by default). Shifted symbols map
# to their key: Shift itself travels as its own key event.
_ROWS = {
    "1234567890-=": 2, "qwertyuiop[]": 16, "asdfghjkl;'`": 30, "\\zxcvbnm,./": 43,
    "!@#$%^&*()_+": 2, "QWERTYUIOP{}": 16, 'ASDFGHJKL:"~': 30, "|ZXCVBNM<>?": 43,
}
KEYCODES: dict[str, int] = {ch: start + i for row, start in _ROWS.items()
                            for i, ch in enumerate(row)}
KEYCODES.update({
    " ": 57, "space": 57, "enter": 28, "backspace": 14, "tab": 15, "delete": 111,
    "left": 105, "right": 106, "up": 103, "down": 108, "home": 102, "end": 107,
    "pageup": 104, "pagedown": 109,
    "shift": 42, "rightshift": 54, "ctrl": 29, "rightctrl": 97, "alt": 56, "rightalt": 100,
})


_SHIFTED = set("!@#$%^&*()_+QWERTYUIOP{}ASDFGHJKL:\"~|ZXCVBNM<>?")
_TEXT_KEYS = {"\n": "enter", "\t": "tab", " ": "space"}


def char_keys(ch: str) -> tuple[int, bool] | None:
    """(Linux keycode, needs Shift) to type `ch` on a US layout; None if it can't be typed."""
    name = _TEXT_KEYS.get(ch, ch)
    code = KEYCODES.get(name) if (len(name) > 1 or 0x20 < ord(name) < 0x7F) else None
    if code is None:
        return None
    return code, ch in _SHIFTED


def normalise_paste(text: str) -> str:
    """Clipboard text as typed keys: CRLF/CR become Enter, nothing else changes."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def keycode(name: str) -> int | None:
    """Linux keycode for a character or key name; None for keys rmdesk never sends."""
    return KEYCODES.get(name)


MODIFIER_CODES = (42, 54, 29, 97, 56, 100, 125, 126)  # shift, ctrl, alt (left/right), meta


class Writer(Protocol):
    def write(self, data: bytes) -> None: ...
    def pause(self, seconds: float) -> None: ...
    def close(self) -> None: ...


class InputSession:
    """Turns touches and key presses into event bytes for the tablet's touch and keyboard writers.

    Remembers what it holds down (the finger, keys) so release_all can always let go."""

    def __init__(self, touch: Writer | None, keys: Writer | None = None,
                 first_id: int = FIRST_TRACKING_ID):
        self.touch, self.keys = touch, keys
        self._next_id = first_id
        self.touching = False
        self._last: tuple[int, int] | None = None
        self.pressed: list[int] = []

    @property
    def typing(self) -> bool:
        return self.keys is not None

    def next_tracking_id(self) -> int:
        tid = self._next_id
        self._next_id = tid + 1 if tid < LAST_TRACKING_ID else FIRST_TRACKING_ID
        return tid

    def _touch(self, data: bytes) -> None:
        if self.touch is not None:
            self.touch.write(data)

    # -- touch (frame pixels in) --
    def touch_down(self, fx: float, fy: float) -> None:
        if self.touching:
            self.touch_up()
        x, y = frame_to_touch(fx, fy)
        self.touching, self._last = True, (x, y)
        self._touch(SLOT0 + pack(EV_ABS, ABS_MT_TRACKING_ID, self.next_tracking_id())
                    + pack(EV_ABS, ABS_MT_POSITION_X, x) + pack(EV_ABS, ABS_MT_POSITION_Y, y)
                    + pack(EV_ABS, ABS_MT_PRESSURE, PRESSURE)
                    + pack(EV_ABS, ABS_MT_TOUCH_MAJOR, TOUCH_MAJOR)
                    + pack(EV_ABS, ABS_MT_TOUCH_MINOR, TOUCH_MINOR) + SYN)

    def touch_move(self, fx: float, fy: float) -> None:
        if not self.touching:
            return
        x, y = frame_to_touch(fx, fy)
        if (x, y) == self._last:
            return
        self._last = (x, y)
        self._touch(SLOT0 + pack(EV_ABS, ABS_MT_POSITION_X, x)
                    + pack(EV_ABS, ABS_MT_POSITION_Y, y) + SYN)

    def touch_up(self) -> None:
        if not self.touching:
            return
        self.touching = False
        self._touch(SLOT0 + pack(EV_ABS, ABS_MT_TRACKING_ID, -1) + SYN)

    def tap(self, fx: float, fy: float) -> None:
        """Finger down and up at a frame point. Not while touching."""
        if self.touching or self.touch is None:
            return
        self.touch_down(fx, fy)
        self.touch.pause(TAP_HOLD)
        self.touch_up()

    def pinch(self, factor: float, fx: float = FRAME_CX, fy: float = FRAME_CY) -> None:
        """Two fingers moving apart (factor > 1, zoom in) or together (factor < 1) around a frame
        point. The tablet zooms by the change in their distance (measured: 240 -> 840 px gave
        3.5x). Ends with both fingers up and slot 0 selected again. Not while touching."""
        if self.touching or self.touch is None or factor <= 0 or factor == 1:
            return
        cx, cy = frame_to_touch(fx, fy)
        # the narrow end is PINCH_NEAR either way; the wide end is factor (or 1/factor) times it
        start, end = ((PINCH_NEAR, PINCH_NEAR * factor) if factor > 1
                      else (PINCH_NEAR / factor, PINCH_NEAR))
        a, b = self.next_tracking_id(), self.next_tracking_id()

        def fingers(d: float, down: bool) -> bytes:
            out = b""
            for slot, tid, sign in ((0, a, -1), (1, b, 1)):
                y = min(max(int(round(cy + sign * d)), 0), TOUCH_MAX_Y)
                out += pack(EV_ABS, ABS_MT_SLOT, slot)
                if down:
                    out += pack(EV_ABS, ABS_MT_TRACKING_ID, tid)
                out += pack(EV_ABS, ABS_MT_POSITION_X, cx) + pack(EV_ABS, ABS_MT_POSITION_Y, y)
                if down:
                    out += (pack(EV_ABS, ABS_MT_PRESSURE, PRESSURE)
                            + pack(EV_ABS, ABS_MT_TOUCH_MAJOR, TOUCH_MAJOR)
                            + pack(EV_ABS, ABS_MT_TOUCH_MINOR, TOUCH_MINOR))
            return out + SYN

        self.touch.write(fingers(start, True))
        for i in range(1, PINCH_STEPS + 1):
            self.touch.pause(PINCH_STEP)
            self.touch.write(fingers(start + (end - start) * i / PINCH_STEPS, False))
        self.touch.pause(PINCH_STEP * 2)
        self.touch.write(pack(EV_ABS, ABS_MT_SLOT, 0) + pack(EV_ABS, ABS_MT_TRACKING_ID, -1)
                         + pack(EV_ABS, ABS_MT_SLOT, 1) + pack(EV_ABS, ABS_MT_TRACKING_ID, -1)
                         + SYN + SLOT0 + SYN)

    def swipe(self, points: Iterable[tuple[float, float]], step: float = SWIPE_STEP,
              hold: float = 0.0) -> None:
        """Down at the first point, paced moves through the rest, then up. Not while touching.
        `hold`: seconds the finger stays still before lifting, so xochitl doesn't fling the
        page on (a 600 px swipe released moving scrolled it 1123 px on fw 3.29.1.0)."""
        pts = list(points)
        if self.touching or not pts or self.touch is None:
            return
        self.touch_down(*pts[0])
        for p in pts[1:]:
            self.touch.pause(step)
            self.touch_move(*p)
        self.touch.pause(max(step, hold))
        self.touch_up()

    # -- keys (Linux keycodes) --
    def key(self, code: int, down: bool) -> bool:
        """Press or release; False if nothing was sent (no keyboard, held, or never pressed)."""
        if self.keys is None:
            return False
        if down:
            if code in self.pressed:
                return False
            self.pressed.append(code)
        else:
            if code not in self.pressed:
                return False
            self.pressed.remove(code)
        self.keys.write(pack(EV_KEY, code, 1 if down else 0) + SYN)
        return True

    def type_char(self, ch: str) -> bool:
        """Type one character (press and release, with Shift if needed) as a single write.
        False if there's no keyboard or the character can't be typed on a US layout."""
        if self.keys is None:
            return False
        k = char_keys(ch)
        if k is None:
            return False
        code, shift = k
        data = pack(EV_KEY, code, 1) + SYN + pack(EV_KEY, code, 0) + SYN
        if shift:
            data = pack(EV_KEY, 42, 1) + SYN + data + pack(EV_KEY, 42, 0) + SYN
        self.keys.write(data)
        return True

    def release_modifiers(self) -> None:
        """Key-up for every modifier on the keyboard device, whether or not rmdesk pressed it:
        clears a modifier the tablet still thinks is held (the kernel drops an up for a key
        that isn't down, so this is harmless)."""
        if self.keys is None:
            return
        self.keys.write(b"".join(pack(EV_KEY, c, 0) + SYN for c in MODIFIER_CODES))

    def drop_keyboard(self) -> None:
        """The keyboard device went away: forget its keys (nothing left to release them on)."""
        self.keys = None
        self.pressed = []

    def release_keys(self) -> None:
        for code in reversed(list(self.pressed)):
            self.key(code, False)

    def release_all(self) -> None:
        """Lift the finger, send key-up for every key rmdesk pressed, then for every modifier."""
        self.touch_up()
        self.release_keys()
        self.release_modifiers()


_CLOSE = object()


class QueuedWriter:
    """Writes queued bytes to a channel opened by `opener` (called on the worker thread).

    The channel needs write(bytes) and close(); an optional check() raises once it died.
    Any failure (open, write, check, a full queue) calls on_dead(error) once, from whichever
    thread found it, and drops everything after. close() lets queued bytes out first."""

    def __init__(self, opener: Callable[[], object], on_dead: Callable[[Exception], None],
                 maxsize: int = 512, poll: float = 0.5):
        self._opener, self._on_dead, self._poll = opener, on_dead, poll
        self._q: queue.Queue = queue.Queue(maxsize)
        self._dead_lock = threading.Lock()
        self.dead = False
        self._closing = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="rmdesk-input")
        self._thread.start()

    def _die(self, e: Exception) -> None:
        with self._dead_lock:
            if self.dead:
                return
            self.dead = True
            closing = self._closing
        if not closing:
            try:
                self._on_dead(e)
            except Exception:
                pass

    def _put(self, item) -> None:
        if self.dead or self._closing:
            return
        try:
            self._q.put_nowait(item)
        except queue.Full:
            self._die(OverflowError("the tablet is not keeping up with the input"))

    def write(self, data: bytes) -> None:
        self._put(bytes(data))

    def pause(self, seconds: float) -> None:
        self._put(float(seconds))

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        while True:  # the close marker must get in even if the queue is full
            try:
                self._q.put_nowait(_CLOSE)
                return
            except queue.Full:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    pass

    def join(self, timeout: float) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        chan = None
        try:
            try:
                chan = self._opener()
            except Exception as e:
                self._die(e)
                return
            check = getattr(chan, "check", None)
            while True:
                try:
                    item = self._q.get(timeout=self._poll)
                except queue.Empty:
                    if check is not None and not self.dead:
                        try:
                            check()
                        except Exception as e:
                            self._die(e)
                    if self.dead:
                        return
                    continue
                if item is _CLOSE or self.dead:
                    return
                try:
                    if isinstance(item, float):
                        time.sleep(item)
                    else:
                        chan.write(item)
                except Exception as e:
                    self._die(e)
        finally:
            if chan is not None:
                try:
                    chan.close()
                except Exception:
                    pass
