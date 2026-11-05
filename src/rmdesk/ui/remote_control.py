"""Control the tablet from the PC: the mouse on the Screen view's picture becomes touch, the PC
keyboard types through the Type Folio's input device (rmdesk.remote.input does the bytes).

Only the Screen view's picture sends, and only while its pointer is Control; Place and the
Present window never do. The one other sender is rmdesk itself: Send borrows touch
(borrow_touch) to swipe a typed-only page to its top and measure its scroll
(ui.compose.scroll_probe), never while a tablet job holds the slot. Everything held (the finger, keys) is released whenever Control turns
off, the picture loses focus, or a tablet job takes the Connection's job slot. Input only: no
files are touched.
"""
from __future__ import annotations

import shlex
import time
from typing import Callable

from PySide6.QtCore import QEvent, QObject, QPointF, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget

from rmdesk.connection import Connection, State
from rmdesk.device import DeviceError
from rmdesk.remote import input as ri
from rmdesk.ui.workers import run_in_thread

CONTROL_CHIP = "Controlling the tablet"
NO_FOLIO_MSG = "Typing needs the Type Folio attached"
CONTROL_TIP = ("Use the mouse and keyboard on the tablet: click = tap, drag = swipe, "
               "wheel = scroll, Ctrl+0 = back to 1x. Ctrl+V types the PC clipboard. Typing "
               "needs the Type Folio attached.")
OFFLINE_MSG = "Connect the tablet to control it."
BUSY_MSG = "The tablet is busy with: {}. Control is off."
LOST_MSG = "Lost the tablet's touch input ({}). Control is off."
NO_TOUCH_MSG = "Couldn't find the tablet's touch screen. Control is off."
LOOKUP_MSG = "Couldn't reach the tablet's input ({}). Control is off."
DISCONNECTED_MSG = "The tablet disconnected. Control is off."
PASTE_MSG = "Typed {n} characters from the clipboard."
PASTE_SKIPPED_MSG = "Typed {n} characters from the clipboard; skipped what a US keyboard can't type: {chars}"
ZOOM_STEP = 1.25  # zoom() pinches by this factor (unused by the UI: only 1x is offered)
PASTE_INTERVAL_MS = 15  # one character per tick: the tablet keeps up, the queue never fills

FRAME_W, FRAME_H = ri.TOUCH_MAX_X + 1, ri.TOUCH_MAX_Y + 1
MOVE_INTERVAL_MS = 16  # drag moves go out at up to ~60 Hz, coalesced
SWIPE_LENGTH = 400  # frame pixels a wheel notch swipes
SWIPE_STEPS = 8
SWIPE_COOLDOWN = 0.3  # seconds: one swipe per wheel burst

_K = Qt.Key
_SPECIAL = {
    _K.Key_Return: "enter", _K.Key_Enter: "enter", _K.Key_Backspace: "backspace",
    _K.Key_Tab: "tab", _K.Key_Backtab: "tab", _K.Key_Delete: "delete", _K.Key_Space: "space",
    _K.Key_Left: "left", _K.Key_Right: "right", _K.Key_Up: "up", _K.Key_Down: "down",
    _K.Key_Home: "home", _K.Key_End: "end", _K.Key_PageUp: "pageup", _K.Key_PageDown: "pagedown",
    _K.Key_Shift: "shift", _K.Key_Control: "ctrl", _K.Key_Alt: "alt", _K.Key_AltGr: "rightalt",
}
# X11/Wayland native scan codes are evdev codes + 8: tells the right-hand modifiers apart
_RIGHT = {54 + 8: "rightshift", 97 + 8: "rightctrl", 100 + 8: "rightalt"}
_APP_CTRL_KEYS = (_K.Key_1, _K.Key_2, _K.Key_3, _K.Key_B)


_MODIFIER_NAMES = {"shift", "ctrl", "alt", "rightshift", "rightctrl", "rightalt"}
_MOD_CODES = ((Qt.KeyboardModifier.ShiftModifier, 42), (Qt.KeyboardModifier.ControlModifier, 29),
              (Qt.KeyboardModifier.AltModifier, 56))


def is_reserved(key: int, mods) -> bool:
    """Esc and rmdesk's own window shortcuts (Ctrl+1/2/3, Ctrl+B, F11) stay with rmdesk."""
    if key in (_K.Key_Escape, _K.Key_F11):
        return True
    return bool(mods & Qt.KeyboardModifier.ControlModifier) and key in _APP_CTRL_KEYS


def key_name(key: int, native_scan_code: int = 0) -> str | None:
    """A Qt key as a rmdesk.remote.input key name (None for keys rmdesk never sends)."""
    name = _SPECIAL.get(key)
    if name in ("shift", "ctrl", "alt"):
        return _RIGHT.get(native_scan_code, name)
    if name is not None:
        return name
    if 0x21 <= key <= 0x7E:
        return chr(key)
    return None


def view_to_frame(geom, pos: QPointF, frame_size: tuple[int, int],
                  clamp: bool = False) -> tuple[float, float] | None:
    """A widget point through the view's rotation, fit and crop to 1404x1872 frame pixels.

    None when the point is off the picture (unless clamp: then the nearest edge)."""
    if not clamp and not geom.target.contains(pos):
        return None
    p = geom.from_view(pos)
    r = geom.region
    x = min(max(p.x(), r.left()), r.left() + r.width())
    y = min(max(p.y(), r.top()), r.top() + r.height())
    fw, fh = max(frame_size[0], 1), max(frame_size[1], 1)
    return x * FRAME_W / fw, y * FRAME_H / fh


def _default_writer(device, path: str, on_dead: Callable[[Exception], None]) -> ri.Writer:
    cmd = f"cat > {shlex.quote(path)}"
    return ri.QueuedWriter(lambda: device.open_writer(cmd), on_dead)


class RemoteControl(QObject):
    """Owns the input session while Control is on. GUI thread only; `geometry` returns
    (Geometry, (frame_w, frame_h)) for the canvas as it is drawn now.

    `canvas` gets the keys and focus; mouse events are taken from it and from `mouse_widget`
    (a QGraphicsView's viewport, same coordinates as the view), if given."""

    changed = Signal()
    _dead = Signal(object, str)  # writer, error (from writer threads)
    _job = Signal()  # Connection.on_job fires on any thread

    def __init__(self, conn: Connection, canvas: QWidget, geometry: Callable[[], tuple],
                 writer_factory: Callable | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 mouse_widget: QWidget | None = None):
        super().__init__(canvas)
        self.conn, self.canvas, self._geometry = conn, canvas, geometry
        self._mouse = mouse_widget
        self._writer_factory = writer_factory or _default_writer
        self._clock = clock
        self.active = False
        self.typing = False
        self.paste_rich: Callable[[], bool] | None = None  # set by the Screen view
        self.message = ""
        self.session: ri.InputSession | None = None
        self._writers: list = []
        self._closed: list = []
        self._touch_w = self._key_w = None
        self._gen = 0
        self._pending: tuple[float, float] | None = None
        self._last_swipe = -1e9
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(MOVE_INTERVAL_MS)
        self._timer.timeout.connect(self._flush_move)
        self._paste: list[str] = []  # characters still to type
        self._pasted = 0
        self._skipped: list[str] = []
        self._paste_timer = QTimer(self)
        self._paste_timer.setInterval(PASTE_INTERVAL_MS)
        self._paste_timer.timeout.connect(self._paste_tick)
        self._lend: dict | None = None  # borrow_touch: the touch lent out for rmdesk's swipes
        self._dead.connect(self._on_dead)
        self._job.connect(self.on_job)
        job = self._job

        def on_job():
            try:
                job.emit()
            except RuntimeError:  # deleted while a worker finished a job
                pass
        conn.on_job.append(on_job)
        canvas.installEventFilter(self)
        if mouse_widget is not None:
            mouse_widget.installEventFilter(self)

    # -- on / off --
    def start(self) -> bool:
        if self.active:
            return True
        dev = self.conn.device
        if self.conn.state not in (State.USB, State.WIFI) or dev is None:
            self._set_message(OFFLINE_MSG)
            return False
        job = self.conn.current_job
        if job is not None:
            self._set_message(BUSY_MSG.format(job))
            return False
        self._gen += 1
        gen = self._gen
        self.active, self.typing, self.message = True, False, ""
        self.canvas.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.canvas.setFocus(Qt.FocusReason.OtherFocusReason)
        self.changed.emit()
        run_in_thread(lambda: dev.run(f"cat {ri.PROC_DEVICES}", timeout=10),
                      lambda res: self._found(gen, dev, res),
                      lambda e: self._lookup_failed(gen, e))
        return True

    def _found(self, gen: int, dev, res) -> None:
        if gen != self._gen or not self.active:
            return
        text = getattr(res, "out", "") or ""
        touch = ri.find_device(text, ri.TOUCH_NAME)
        if touch is None:
            self.stop(NO_TOUCH_MSG)
            return
        keys = ri.find_device(text, ri.KEYBOARD_NAME)
        self._touch_w = self._open(dev, touch)
        self._key_w = self._open(dev, keys) if keys else None
        self.session = ri.InputSession(self._touch_w, self._key_w)
        self.session.release_modifiers()  # in case an earlier session left one held
        self._chords: dict[int, list[int]] = {}
        self.typing = self._key_w is not None
        self.changed.emit()
        lend = self._lend
        if lend is not None and not lend["own"] and lend["session"] is None:
            lend["session"] = self.session  # a borrow waited for Control's lookup
            lend["ready"](self.session)

    def _open(self, dev, path: str):
        holder: list = []
        dead = self._dead

        def on_dead(e: Exception) -> None:  # writer thread
            try:
                dead.emit(holder[0] if holder else None, str(e) or type(e).__name__)
            except RuntimeError:
                pass
        w = self._writer_factory(dev, path, on_dead)
        holder.append(w)
        self._writers.append(w)
        return w

    def _lookup_failed(self, gen: int, e: Exception) -> None:
        if gen != self._gen:
            return
        if isinstance(e, DeviceError):
            self.conn.connection_lost(e)
        self.stop(LOOKUP_MSG.format(e))

    def stop(self, message: str | None = None) -> None:
        """Control off: release everything held, close the writers (queued bytes still go).
        Touch lent out (borrow_touch) is taken back too: the borrower is told why."""
        was = self.active
        if self._lend is not None:
            self._lend_failed(self._lend, message or NO_TOUCH_MSG)
        self._gen += 1
        self._timer.stop()
        self._pending = None
        self._paste_timer.stop()
        self._paste = []
        if self.session is not None:
            self.session.release_all()
        for w in self._writers:
            try:
                w.close()
            except Exception:
                pass
        self._closed, self._writers = self._writers, []
        self.session, self._touch_w, self._key_w = None, None, None
        self.active = self.typing = False
        if was:
            self.canvas.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if message is not None:
            self.message = message
        elif was:
            self.message = ""
        if was or message is not None:
            self.changed.emit()

    def shutdown(self, timeout: float = 1.0) -> None:
        """App quit: release and give the writers a moment to get the release out."""
        self.stop()
        end = time.monotonic() + timeout
        for w in self._closed:
            join = getattr(w, "join", None)
            if join is not None:
                join(max(0.0, end - time.monotonic()))

    def _set_message(self, text: str) -> None:
        self.message = text
        self.changed.emit()

    @Slot()
    def on_job(self) -> None:
        job = self.conn.current_job
        if job is not None and self._lend is not None:
            self._lend_failed(self._lend, BUSY_MSG.format(job))
        if self.active and job is not None:
            self.stop(BUSY_MSG.format(job))

    def on_state(self, state) -> None:
        if self.active and state not in (State.USB, State.WIFI):
            self.stop(DISCONNECTED_MSG)

    @Slot(object, str)
    def _on_dead(self, writer, error: str) -> None:
        lend = self._lend
        if writer is not None and lend is not None and lend["writer"] is writer:
            self._lend_failed(lend, LOST_MSG.format(error))
            return
        if writer is None or writer not in self._writers:
            return
        if writer is self._key_w:  # the folio went away: touch keeps working
            if self.session is not None:
                self.session.drop_keyboard()
            self._writers.remove(writer)
            try:
                writer.close()
            except Exception:
                pass
            self._key_w, self.typing = None, False
            self.changed.emit()
        else:
            self.stop(LOST_MSG.format(error))

    def _ready(self) -> bool:
        """May input go out now? Never while a tablet job holds the slot."""
        if not self.active or self.session is None:
            return False
        job = self.conn.current_job
        if job is not None:
            self.stop(BUSY_MSG.format(job))
            return False
        return True

    # -- touch lent to rmdesk itself (measuring a page's scroll: ui.compose.scroll_probe) --
    def borrow_touch(self, ready: Callable[[ri.InputSession], None],
                     fail: Callable[[str], None]) -> None:
        """Touch for rmdesk's own swipes, GUI thread: ready(session) now or later, or
        fail(message). While Control is on, its own session (once its lookup is done);
        otherwise a touch-only session of its own, on the tablet's touch screen. Never while a
        tablet job holds the job slot: a job that starts, Control stopping or the touch writer
        dying takes it back (fail). One borrower at a time; return_touch() when done."""
        if self._lend is not None:
            fail(NO_TOUCH_MSG)
            return
        job = self.conn.current_job
        if job is not None:
            fail(BUSY_MSG.format(job))
            return
        if self.active:
            self._lend = {"own": False, "session": self.session, "writer": None,
                          "ready": ready, "fail": fail}
            if self.session is not None:
                ready(self.session)
            return  # else _found hands it over
        dev = self.conn.device
        if self.conn.state not in (State.USB, State.WIFI) or dev is None:
            fail(OFFLINE_MSG)
            return
        lend = {"own": True, "session": None, "writer": None, "ready": ready, "fail": fail}
        self._lend = lend
        run_in_thread(lambda: dev.run(f"cat {ri.PROC_DEVICES}", timeout=10),
                      lambda res: self._lend_found(lend, dev, res),
                      lambda e: self._lend_failed(lend, LOOKUP_MSG.format(e)))

    def _lend_found(self, lend: dict, dev, res) -> None:
        if self._lend is not lend:
            return
        job = self.conn.current_job
        if job is not None:
            self._lend_failed(lend, BUSY_MSG.format(job))
            return
        touch = ri.find_device(getattr(res, "out", "") or "", ri.TOUCH_NAME)
        if touch is None:
            self._lend_failed(lend, NO_TOUCH_MSG)
            return
        holder: list = []
        dead = self._dead

        def on_dead(e: Exception) -> None:  # writer thread
            try:
                dead.emit(holder[0] if holder else None, str(e) or type(e).__name__)
            except RuntimeError:
                pass
        w = self._writer_factory(dev, touch, on_dead)
        holder.append(w)
        lend["writer"] = w
        lend["session"] = ri.InputSession(w, None)
        lend["ready"](lend["session"])

    def _lend_failed(self, lend: dict, message: str) -> None:
        if self._lend is not lend:
            return
        fail = lend["fail"]
        self._take_back(lend)
        fail(message)

    def return_touch(self) -> None:
        """The borrower is done: lift the finger (and close the borrowed writer)."""
        if self._lend is not None:
            self._take_back(self._lend)

    def _take_back(self, lend: dict) -> None:
        self._lend = None
        s = lend["session"]
        if s is not None:
            s.touch_up()
        w = lend["writer"]
        if lend["own"] and w is not None:
            try:
                w.close()
            except Exception:
                pass
            self._closed.append(w)  # shutdown() gives it a moment to get the release out

    # -- mouse --
    def _frame_point(self, pos: QPointF, clamp: bool) -> tuple[float, float] | None:
        geom, size = self._geometry()
        return view_to_frame(geom, pos, size, clamp)

    def _flush_move(self) -> None:
        p, self._pending = self._pending, None
        if p is None or self.session is None or not self.session.touching:
            return
        if not self._ready():
            return
        self.session.touch_move(*p)
        self._timer.start()  # the next move waits its turn; later ones coalesce

    def _swipe(self, pos: QPointF, up: bool) -> None:
        now = self._clock()
        if now - self._last_swipe < SWIPE_COOLDOWN or self.session.touching:
            return
        geom, size = self._geometry()
        start = view_to_frame(geom, pos, size)
        if start is None:
            return
        self._last_swipe = now
        # wheel up scrolls back: the finger moves down the screen (in the view's own direction)
        length = SWIPE_LENGTH * geom.scale * (1 if up else -1)
        t = geom.target
        pts = []
        for i in range(SWIPE_STEPS + 1):
            y = min(max(pos.y() + length * i / SWIPE_STEPS, t.top()), t.bottom())
            pts.append(view_to_frame(geom, QPointF(pos.x(), y), size, clamp=True))
        self.session.swipe(pts)

    def eventFilter(self, obj, e) -> bool:
        if obj is not self.canvas and (self._mouse is None or obj is not self._mouse):
            return False
        t = e.type()
        if obj is self.canvas and t in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            return self._key_event(e, t == QEvent.Type.KeyPress)
        if obj is self.canvas and t == QEvent.Type.FocusOut:
            if self.session is not None and (self.session.touching or self.session.pressed):
                self._timer.stop()
                self._pending = None
                self.session.release_all()
            return False
        if not self.active:
            return False
        left = Qt.MouseButton.LeftButton
        if t in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            if e.button() != left:
                return True
            if self._ready():
                p = self._frame_point(e.position(), clamp=False)
                if p is not None:
                    self.session.touch_down(*p)
            return True
        if t == QEvent.Type.MouseMove:
            s = self.session
            if s is not None and s.touching:
                self._pending = self._frame_point(e.position(), clamp=True)
                if not self._timer.isActive():
                    self._flush_move()
            return True
        if t == QEvent.Type.MouseButtonRelease:
            if e.button() != left:
                return True
            s = self.session
            if s is not None and s.touching:
                self._timer.stop()
                if self._ready():
                    p = self._frame_point(e.position(), clamp=True)
                    if p is not None:
                        s.touch_move(*p)
                    s.touch_up()
            self._pending = None
            return True
        if t == QEvent.Type.Wheel:
            dy = e.angleDelta().y()
            if dy and self._ready():
                self._swipe(e.position(), dy > 0)
            return True
        return False

    # -- zoom: pinches, and a tap on the tablet's own zoom label for 1x --
    def zoom(self, factor: float) -> bool:
        """Pinch around the middle of the screen: factor > 1 zooms in. Not offered by the UI
        any more (the pinches did not zoom reliably); 1x (zoom_reset) is."""
        if not self._ready() or self.session.touching:
            return False
        self.session.pinch(factor)
        return True

    def zoom_reset(self) -> bool:
        """Back to 1x: tap the zoom label the tablet shows at the top centre while zoomed. Where
        that is depends on the tablet's layout (Type Folio or not), read from the tablet first."""
        if not self._ready() or self.session.touching:
            return False
        dev, gen = self.conn.device, self._gen
        from rmdesk.ui.live_compose import tablet_layout  # (a GUI-free check of the tablet)

        def tap(layout: str) -> None:
            if gen != self._gen or not self._ready() or self.session.touching:
                return
            self.session.tap(*ri.ZOOM_LABEL.get(layout, ri.ZOOM_LABEL["portrait"]))
        run_in_thread(lambda: tablet_layout(dev), tap,
                      lambda e: self._set_message(f"Couldn't check the tablet's layout ({e})."))
        return True

    # -- paste: the PC clipboard typed out on the tablet --
    @staticmethod
    def _zoom_key(e) -> float | None:
        """Ctrl+0 -> 0 (back to 1x); else None (Ctrl+minus/plus are ordinary keys)."""
        if not e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            return None
        return 0 if e.key() == _K.Key_0 else None

    @staticmethod
    def _is_paste(e) -> bool:
        mods = e.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        return (ctrl and not shift and e.key() == _K.Key_V) or (shift and e.key() == _K.Key_Insert)

    def paste(self, text: str) -> None:
        """Type `text` (plain text) on the tablet, one character per tick. Ctrl/Shift held for
        the shortcut are released on the tablet first, so the characters arrive as typed."""
        if not self._ready() or not self.session.typing:
            return
        self.session.release_keys()
        self._chords = {}
        self._paste += list(ri.normalise_paste(text))
        if not self._paste_timer.isActive():
            self._pasted, self._skipped = 0, []
            self._paste_timer.start()

    def _paste_tick(self) -> None:
        if self._paste and self._ready() and self.session.typing:
            ch = self._paste.pop(0)
            if self.session.type_char(ch):
                self._pasted += 1
            else:
                self._skipped.append(ch)
            if self._paste:
                return
        # done (or Control/typing went away): stop and say what happened
        self._paste_timer.stop()
        self._paste = []
        if self._pasted or self._skipped:
            chars = " ".join(dict.fromkeys(self._skipped))
            self._set_message(PASTE_SKIPPED_MSG.format(n=self._pasted, chars=chars)
                              if self._skipped else PASTE_MSG.format(n=self._pasted))
        self._pasted, self._skipped = 0, []

    # -- keyboard --
    def _key_event(self, e, press: bool) -> bool:
        if not self.active or is_reserved(e.key(), e.modifiers()):
            return False
        if e.isAutoRepeat():
            return True  # the tablet repeats held keys on its own
        if self._zoom_key(e) is not None:
            if press:
                self.zoom_reset()
            return True
        if self._is_paste(e):
            # a picture (or copied picture files) on the clipboard is placed on the page;
            # text is typed on the tablet
            if press and self.paste_rich is not None and self.paste_rich():
                return True
            if press and self._ready() and self.session.typing:
                self.paste(QGuiApplication.clipboard().text())
            return True
        name = key_name(e.key(), e.nativeScanCode())
        if name in _MODIFIER_NAMES:
            # never sent on their own: a desktop shortcut (e.g. Super+Alt+F) can swallow the
            # release and leave the modifier held on the tablet. They go with the key they
            # modify instead (below).
            return True
        code = ri.keycode(name) if name else None
        if code is None:
            return False
        if self._ready() and self.session.typing:
            if press:
                mods = [m for flag, m in _MOD_CODES if e.modifiers() & flag]
                for m in mods:
                    self.session.key(m, True)
                if self.session.key(code, True):
                    self._chords[code] = mods
            else:
                self.session.key(code, False)
                mods = self._chords.pop(code, [])
                still = {m for ms in self._chords.values() for m in ms}
                for m in reversed(mods):
                    if m not in still:
                        self.session.key(m, False)
        return True
