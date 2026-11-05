"""rmdesk.remote.input: event packing, device lookup, mapping, keycodes, InputSession."""
import struct
import threading
import time

import pytest

from rmdesk.remote import input as ri

ABS, SYN, KEY = 3, 0, 1
TID, X, Y, P, MAJ, MIN = 0x39, 0x35, 0x36, 0x3A, 0x30, 0x31
SLOT = 0x2F


def ev(t, c, v):
    return struct.pack("<llHHi", 0, 0, t, c, v)


SYNR = ev(SYN, 0, 0)


class FakeWriter:
    def __init__(self):
        self.items: list = []
        self.closed = False

    def write(self, data: bytes) -> None:
        assert len(data) % 16 == 0
        self.items.append(data)

    def pause(self, seconds: float) -> None:
        self.items.append(("pause", seconds))

    def close(self) -> None:
        self.closed = True

    def data(self) -> bytes:
        return b"".join(i for i in self.items if isinstance(i, bytes))


def down_bytes(tid, x, y):
    return (ev(ABS, SLOT, 0) + ev(ABS, TID, tid) + ev(ABS, X, x) + ev(ABS, Y, y) + ev(ABS, P, 89)
            + ev(ABS, MAJ, 8) + ev(ABS, MIN, 17) + SYNR)


UP = ev(ABS, SLOT, 0) + ev(ABS, TID, -1) + SYNR


def move_bytes(x, y):
    return ev(ABS, SLOT, 0) + ev(ABS, X, x) + ev(ABS, Y, y) + SYNR


# ---------------------------------------------------------------- packing

def test_pack_is_16_byte_input_event():
    b = ri.pack(3, 0x35, 709)
    assert len(b) == 16
    assert b == struct.pack("<llHHi", 0, 0, 3, 0x35, 709)
    assert ri.pack(3, 0x39, -1)[-4:] == b"\xff\xff\xff\xff"


# ---------------------------------------------------------------- device lookup

PROC = """I: Bus=0000 Vendor=0000 Product=0000 Version=0000
N: Name="30370000.snvs:snvs-powerkey"
P: Phys=snvs-pwrkey/input0
H: Handlers=kbd event0
B: EV=3

I: Bus=0018 Vendor=056a Product=0000 Version=0036
N: Name="Wacom I2C Digitizer"
H: Handlers=event1
B: EV=b

I: Bus=0000 Vendor=0000 Product=0000 Version=0000
N: Name="pt_mt"
H: Handlers=event2
B: EV=b

I: Bus=0005 Vendor=2edd Product=0001 Version=0001
N: Name="rM_Keyboard"
H: Handlers=sysrq kbd leds event3
B: EV=120013
"""


def test_find_device_by_name():
    assert ri.find_device(PROC, "pt_mt") == "/dev/input/event2"
    assert ri.find_device(PROC, "rM_Keyboard") == "/dev/input/event3"
    assert ri.find_device(PROC, "nope") is None
    no_folio = PROC.split("I: Bus=0005")[0]
    assert ri.find_device(no_folio, "rM_Keyboard") is None


def test_find_device_never_guesses_by_number():
    moved = PROC.replace("event2", "event7")
    assert ri.find_device(moved, "pt_mt") == "/dev/input/event7"


# ---------------------------------------------------------------- mapping

@pytest.mark.parametrize("frame, touch", [
    ((0, 0), (0, 1871)),
    ((1404, 0), (1403, 1871)),
    ((0, 1872), (0, 0)),
    ((1404, 1872), (1403, 0)),
    ((702, 936), (702, 935)),
    ((709, 884), (709, 987)),
    ((-50, 5000), (0, 0)),  # clamped
])
def test_frame_to_touch(frame, touch):
    assert ri.frame_to_touch(*frame) == touch


# ---------------------------------------------------------------- keycodes

def test_keycodes_us_layout():
    k = ri.keycode
    assert k("a") == 30 and k("A") == 30 and k("z") == 44 and k("q") == 16
    assert k("1") == 2 and k("0") == 11 and k("!") == 2 and k(")") == 11
    assert k(" ") == 57 and k("space") == 57
    assert k("enter") == 28 and k("backspace") == 14 and k("tab") == 15
    assert k("delete") == 111 and k("left") == 105 and k("right") == 106
    assert k("up") == 103 and k("down") == 108 and k("home") == 102 and k("end") == 107
    assert k("pageup") == 104 and k("pagedown") == 109
    assert k("shift") == 42 and k("ctrl") == 29 and k("alt") == 56
    assert k("rightshift") == 54 and k("rightctrl") == 97 and k("rightalt") == 100
    assert k("-") == 12 and k("_") == 12 and k("=") == 13 and k("+") == 13
    assert k("[") == 26 and k("{") == 26 and k("]") == 27 and k(";") == 39 and k(":") == 39
    assert k("'") == 40 and k('"') == 40 and k("`") == 41 and k("~") == 41
    assert k("\\") == 43 and k("|") == 43 and k(",") == 51 and k("<") == 51
    assert k(".") == 52 and k(">") == 52 and k("/") == 53 and k("?") == 53
    assert k("@") == 3 and k("#") == 4 and k("^") == 7 and k("*") == 9


def test_escape_and_unknown_have_no_keycode():
    assert ri.keycode("escape") is None
    assert ri.keycode("é") is None
    assert ri.keycode("f11") is None


# ---------------------------------------------------------------- touch

def test_tap_bytes():
    w = FakeWriter()
    s = ri.InputSession(w)
    s.touch_down(709, 884)
    assert w.data() == down_bytes(40000, 709, 987)
    s.touch_up()
    assert w.data() == down_bytes(40000, 709, 987) + UP
    assert not s.touching


def test_drag_bytes_and_coalesced_duplicate_move():
    w = FakeWriter()
    s = ri.InputSession(w)
    s.touch_down(100, 100)
    s.touch_move(110, 120)
    s.touch_move(110, 120)  # same spot: nothing new
    s.touch_move(110, 130)
    s.touch_up()
    want = (down_bytes(40000, 100, 1771)
            + move_bytes(110, 1751)
            + move_bytes(110, 1741)
            + UP)
    assert w.data() == want


def test_fresh_tracking_id_per_touch_and_wrap():
    w = FakeWriter()
    s = ri.InputSession(w)
    s.touch_down(1, 1)
    s.touch_up()
    s.touch_down(1, 1)
    s.touch_up()
    assert w.data() == down_bytes(40000, 1, 1870) + UP + down_bytes(40001, 1, 1870) + UP
    s2 = ri.InputSession(FakeWriter(), first_id=65534)
    assert [s2.next_tracking_id() for _ in range(3)] == [65534, 40000, 40001]


def test_move_without_touch_sends_nothing():
    w = FakeWriter()
    s = ri.InputSession(w)
    s.touch_move(5, 5)
    s.touch_up()
    assert w.items == []


def test_swipe_bytes_paced():
    w = FakeWriter()
    s = ri.InputSession(w)
    s.swipe([(100, 1000), (100, 900), (100, 800)], step=0.02)
    assert w.data() == (down_bytes(40000, 100, 871)
                        + move_bytes(100, 971)
                        + move_bytes(100, 1071) + UP)
    assert ("pause", 0.02) in w.items
    assert not s.touching


def test_swipe_ignored_while_touching():
    w = FakeWriter()
    s = ri.InputSession(w)
    s.touch_down(1, 1)
    n = len(w.items)
    s.swipe([(1, 1), (1, 100)])
    assert len(w.items) == n


# ---------------------------------------------------------------- keys

def test_key_press_release_bytes_and_shift_letter():
    t, k = FakeWriter(), FakeWriter()
    s = ri.InputSession(t, k)
    assert s.typing
    s.key(42, True)
    s.key(30, True)
    s.key(30, False)
    s.key(42, False)
    assert k.data() == (ev(KEY, 42, 1) + SYNR + ev(KEY, 30, 1) + SYNR
                        + ev(KEY, 30, 0) + SYNR + ev(KEY, 42, 0) + SYNR)
    assert t.items == []


def test_key_repeat_and_stray_release_ignored():
    k = FakeWriter()
    s = ri.InputSession(FakeWriter(), k)
    s.key(30, True)
    s.key(30, True)  # held: the tablet repeats on its own
    s.key(31, False)  # never pressed here
    assert k.data() == ev(KEY, 30, 1) + SYNR


def test_no_keyboard_means_no_typing():
    t = FakeWriter()
    s = ri.InputSession(t, None)
    assert not s.typing
    assert s.key(30, True) is False
    assert t.items == []


def test_release_all_lifts_touch_and_keys():
    t, k = FakeWriter(), FakeWriter()
    s = ri.InputSession(t, k)
    s.touch_down(10, 10)
    s.key(29, True)
    s.key(46, True)
    t.items.clear()
    k.items.clear()
    s.release_all()
    assert t.data() == UP
    sweep = b"".join(ev(KEY, c, 0) + SYNR for c in ri.MODIFIER_CODES)
    assert k.data() == ev(KEY, 46, 0) + SYNR + ev(KEY, 29, 0) + SYNR + sweep
    assert not s.touching and not s.pressed
    s.release_all()  # nothing held: only the modifier sweep again (harmless, always sent)
    assert t.data() == UP and len(k.items) == 4 and k.items[-1] == sweep


def test_drop_keyboard_forgets_keys():
    k = FakeWriter()
    s = ri.InputSession(FakeWriter(), k)
    s.key(30, True)
    s.drop_keyboard()
    assert not s.typing and not s.pressed
    s.release_all()
    assert k.data() == ev(KEY, 30, 1) + SYNR


# ---------------------------------------------------------------- QueuedWriter

class FakeChannel:
    def __init__(self, fail_after=None, block: threading.Event | None = None):
        self.data = bytearray()
        self.closed = False
        self.fail_after = fail_after
        self.writes = 0
        self.dead = False
        self.block = block
        self.threads = set()

    def write(self, b):
        if self.block is not None:
            self.block.wait(5)
        self.threads.add(threading.current_thread())
        self.writes += 1
        if self.fail_after is not None and self.writes > self.fail_after:
            raise OSError("Socket is closed")
        self.data += b

    def check(self):
        if self.dead:
            raise OSError("cat exited")

    def close(self):
        self.closed = True


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def test_queued_writer_writes_in_order_off_thread_and_closes():
    ch = FakeChannel()
    w = ri.QueuedWriter(lambda: ch, on_dead=lambda e: None)
    w.write(b"a" * 16)
    w.pause(0.001)
    w.write(b"b" * 16)
    w.close()
    assert w.join(5)
    assert bytes(ch.data) == b"a" * 16 + b"b" * 16 and ch.closed
    assert threading.current_thread() not in ch.threads


def test_queued_writer_never_blocks_caller():
    gate = threading.Event()
    ch = FakeChannel(block=gate)
    w = ri.QueuedWriter(lambda: ch, on_dead=lambda e: None)
    t0 = time.monotonic()
    for _ in range(50):
        w.write(b"x" * 16)
    assert time.monotonic() - t0 < 0.5
    gate.set()
    w.close()
    assert w.join(5)


def test_queued_writer_reports_death_once():
    deaths = []
    ch = FakeChannel(fail_after=1)
    w = ri.QueuedWriter(lambda: ch, on_dead=deaths.append)
    w.write(b"a" * 16)
    w.write(b"b" * 16)
    assert wait_for(lambda: deaths)
    w.write(b"c" * 16)  # after death: dropped quietly
    w.close()
    assert w.join(5)
    assert len(deaths) == 1 and w.dead


def test_queued_writer_open_failure_is_death():
    deaths = []

    def opener():
        raise OSError("no such device")
    w = ri.QueuedWriter(opener, on_dead=deaths.append)
    assert wait_for(lambda: deaths)
    assert "no such device" in str(deaths[0])


def test_queued_writer_notices_idle_channel_death():
    deaths = []
    ch = FakeChannel()
    w = ri.QueuedWriter(lambda: ch, on_dead=deaths.append, poll=0.01)
    ch.dead = True
    assert wait_for(lambda: deaths)
    w.close()


def test_queued_writer_overflow_is_death():
    gate = threading.Event()
    deaths = []
    ch = FakeChannel(block=gate)
    w = ri.QueuedWriter(lambda: ch, on_dead=deaths.append, maxsize=4)
    for _ in range(20):
        w.write(b"x" * 16)
    assert deaths and w.dead
    gate.set()
    w.close()
    assert w.join(5)


def test_char_keys_and_normalise_paste():
    from rmdesk.remote.input import char_keys, normalise_paste
    assert char_keys("a") == (30, False) and char_keys("A") == (30, True)
    assert char_keys("!") == (2, True) and char_keys("\n") == (28, False)
    assert char_keys(" ") == (57, False) and char_keys("\t") == (15, False)
    assert char_keys("é") is None and char_keys("😀") is None
    assert normalise_paste("a\r\nb\rc") == "a\nb\nc"



def test_pinch_spreads_by_the_factor_and_ends_on_slot_0():
    w = FakeWriter()
    s = ri.InputSession(w)
    s.pinch(2.0)
    writes = [i for i in w.items if isinstance(i, bytes)]
    first, last_move = writes[0], writes[-2]

    def ys(chunk):
        out = []
        for i in range(0, len(chunk), 16):
            t, c, v = struct.unpack("<llHHi", chunk[i:i + 16])[2:]
            if t == ABS and c == Y:
                out.append(v)
        return out
    a, b = ys(first)
    c, d = ys(last_move)
    assert abs((d - c) / (b - a) - 2.0) < 0.02  # the spread doubled
    assert writes[-1].endswith(ev(ABS, SLOT, 0) + SYNR) and not s.touching
    s.pinch(0.5)
    w2 = [i for i in w.items if isinstance(i, bytes)]
    assert len(w2) > len(writes)


def test_tap_is_down_pause_up():
    w = FakeWriter()
    s = ri.InputSession(w)
    s.tap(704, 56)
    assert w.items[0] == down_bytes(40000, 704, 1871 - 56) and w.items[1][0] == "pause"
    assert w.items[2] == UP
