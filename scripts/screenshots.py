"""Make the README screenshots (docs/screenshots/*.png) without a tablet.

Runs the real window offscreen against a fake, connected tablet whose screen shows a made-up
demo page, so no real notes ever end up in a picture. Run from the repo root:

    python scripts/screenshots.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"  # never real windows, even from a desktop shell
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # tests.fakes

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QThreadPool
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication

from rmdesk.battery import Battery
from rmdesk.config import Config
from rmdesk.connection import Connection
from rmdesk.device import DeviceError
from rmdesk.firmware import XOCHITL_DIR, FirmwareInfo
from rmdesk.stream.client import Frame
from rmdesk.ui.app import MainWindow, load_theme
from tests.fakes import FakeDevice
from rmdesk import safety

safety.prune = lambda *a, **k: []  # never touch the real backups folder

OUT = ROOT / "docs" / "screenshots"
W, H = 1404, 1872
SIZE = (1280, 860)


class NoSecrets:
    def get(self, k):
        return None

    def set(self, k, v):
        pass


class Creds:
    def get(self, serial):
        return "admin:demo"

    def set(self, k, v):
        pass


class NoWeb:
    def available(self):
        return False


class StillClient:
    """A stream that shows one picture."""

    def __init__(self, host, password, on_frame, *, username, on_error=None):
        self.on_frame, self.alive = on_frame, False

    def start(self):
        self.alive = True
        self.on_frame(demo_frame())

    def stop(self):
        self.alive = False

    def is_alive(self):
        return self.alive

    def resync(self):
        self.on_frame(demo_frame())


class NullWriter:
    """Control's input writer: touches and keys go nowhere."""

    def __init__(self, *a):
        pass

    def write(self, data):
        pass

    def pause(self, s):
        pass

    def close(self):
        pass


PROC = """N: Name="pt_mt"
H: Handlers=event2

N: Name="rM_Keyboard"
H: Handlers=kbd event3
"""


DEMO_LIBRARY = [  # (name, kind, parent)
    ("Work", "folder", ""), ("Reading", "folder", ""),
    ("Weekly plan", "notebook", ""), ("Garden ideas", "notebook", ""),
    ("Meeting notes", "notebook", "Work"), ("Project brief", "pdf", "Work"),
    ("Quarterly report", "pdf", "Work"), ("A short novel", "epub", "Reading"),
    ("Field guide to birds", "pdf", "Reading"), ("Sketchbook", "notebook", ""),
]


def demo_files() -> dict[str, bytes]:
    import json
    files = {f"{XOCHITL_DIR}/.keep": b"", "/proc/bus/input/devices": PROC.encode()}
    ids = {}
    for i, (name, kind, parent) in enumerate(DEMO_LIBRARY):
        uid = f"00000000-0000-4000-8000-{i:012d}"
        ids[name] = uid
        meta = {"visibleName": name, "parent": ids.get(parent, ""),
                "type": "CollectionType" if kind == "folder" else "DocumentType",
                "lastModified": str(1_790_000_000_000 - i * 86_400_000)}
        files[f"{XOCHITL_DIR}/{uid}.metadata"] = json.dumps(meta).encode()
        if kind != "folder":
            content = {"fileType": kind, "pages": [f"p{i}-{n}" for n in range(3)]}
            files[f"{XOCHITL_DIR}/{uid}.content"] = json.dumps(content).encode()
    return files


def run_handler(cmd: str):
    from rmdesk.device import CommandResult
    if cmd.startswith("df "):
        return CommandResult(0, "Filesystem Size Used Avail Use% Mounted on\n"
                                "/dev/mmcblk2p4 6.4G 1.9G 4.5G 30% /home\n", "")
    if cmd.startswith("systemctl is-active") and "xochitl" not in cmd:
        return CommandResult(0, "active\n", "")  # the screen streaming service
    return None


class DemoConnection(Connection):
    def _after_connect(self, device, password):
        self.info = FirmwareInfo("3.29.1.0", "RM110-000-00000", 6)


def demo_frame() -> Frame:
    """A portrait page: a heading, typed lines, a hand-drawn sketch and a ticked list."""
    img = QImage(W, H, QImage.Format.Format_Grayscale8)
    img.fill(255)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QColor(20, 20, 20))
    f = QFont("Inter", 46, QFont.Weight.Bold)
    p.setFont(f)
    p.drawText(QPointF(110, 210), "Weekly plan")
    p.setFont(QFont("Inter", 26))
    lines = ["Draft the project brief and share it", "Sketch the new garden layout",
             "Read chapter 4 and take notes", "Book the workshop room"]
    for i, t in enumerate(lines):
        y = 330 + i * 78
        p.setPen(QPen(QColor(30, 30, 30), 3))
        p.drawRect(QRectF(110, y - 30, 34, 34))
        if i < 2:
            path = QPainterPath(QPointF(116, y - 14))
            path.lineTo(126, y - 2)
            path.lineTo(150, y - 40)
            p.setPen(QPen(QColor(10, 10, 10), 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawPath(path)
        p.setPen(QColor(30, 30, 30))
        p.drawText(QPointF(170, y), t)
    # a hand-drawn sketch: a wobbly box, a sun and a path
    pen = QPen(QColor(25, 25, 25), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    rng = np.random.default_rng(3)

    def wobble(pts):
        path = QPainterPath(QPointF(*pts[0]))
        for x, y in pts[1:]:
            path.lineTo(x + rng.normal(0, 1.6), y + rng.normal(0, 1.6))
        p.drawPath(path)

    def seg(a, b, n=30):
        return [(a[0] + (b[0] - a[0]) * t / n, a[1] + (b[1] - a[1]) * t / n) for t in range(n + 1)]

    box = [(150, 820), (1250, 820), (1250, 1500), (150, 1500), (150, 820)]
    for a, b in zip(box, box[1:]):
        wobble(seg(a, b))
    for cx, cy, r in [(420, 1070, 120), (980, 1180, 170)]:
        wobble([(cx + r * np.cos(t), cy + r * np.sin(t)) for t in np.linspace(0, 2 * np.pi, 60)])
    wobble([(250, 1440), (400, 1330), (600, 1380), (800, 1300), (1100, 1420)])
    p.setFont(QFont("Inter", 22))
    p.drawText(QPointF(330, 1080), "beds")
    p.drawText(QPointF(930, 1190), "pond")
    p.setFont(QFont("Inter", 24, QFont.Weight.Normal, True))
    p.drawText(QPointF(150, 1600), "Ideas: herbs by the door, a bench near the pond.")
    p.end()
    ptr = img.constBits()
    gray = np.frombuffer(ptr, np.uint8, img.bytesPerLine() * H).reshape(H, img.bytesPerLine())
    return Frame(W, H, gray[:, :W].copy())


def settle(app, n=30):
    for _ in range(n):
        app.processEvents()
    QThreadPool.globalInstance().waitForDone(3000)
    for _ in range(n):
        app.processEvents()


def wait(app, cond, tries=200):
    import time
    for _ in range(tries):
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def shoot(win, name):
    OUT.mkdir(parents=True, exist_ok=True)
    win.grab().save(str(OUT / f"{name}.png"))
    print("wrote", OUT / f"{name}.png")


def make_window(connected=True):
    dev = FakeDevice(demo_files(), run_handler=run_handler)
    cfg = Config()

    def factory(h, p):
        if not connected:
            raise DeviceError("not reachable")
        return dev
    conn = DemoConnection(cfg, factory=factory, secrets=NoSecrets(), sleep=lambda s: None,
                          persist=lambda c: None)
    if connected:
        conn.connect()
    sv_kwargs = dict(client_factory=StillClient, status_fn=lambda d: "running", secrets=Creds(),
                     input_writer_factory=lambda dev, path, on_dead: NullWriter())
    import rmdesk.ui.app as app_mod
    real = app_mod.ScreenView
    app_mod.ScreenView = lambda *a, **k: real(*a, **{**k, **sv_kwargs})
    try:
        win = MainWindow(conn, cfg, save_config=lambda c: None, web_factory=NoWeb)
    finally:
        app_mod.ScreenView = real
    return win, conn


def show(app, win):
    win.show()
    win.setFixedSize(*SIZE)
    settle(app)


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyleSheet(load_theme())

    # 1. the connection screen
    win, conn = make_window(connected=False)
    show(app, win)
    shoot(win, "connect")
    win.close()

    # 2. the live Screen view (Control)
    win, conn = make_window()
    show(app, win)
    sv = win.screen_view
    wait(app, lambda: sv.frame is not None)
    win._battery_changed(Battery(78, "Discharging"))
    settle(app)
    shoot(win, "screen")

    # 3. Place: a sticky note and an arrow over the page
    sv.set_pointer("place")
    settle(app)
    c = sv.compose
    ink = c.add_ink_text("Water the herbs!")
    if ink is not None:
        ink.setPos(ink.pos() + QPointF(150, -250))
    arrow = c.add_shape("arrow")
    if arrow is not None:
        arrow.setPos(arrow.pos() + QPointF(-420, -250))
        arrow.setSelected(True)
    settle(app)
    shoot(win, "place")
    sv.set_pointer("control")

    # 4. Files and Device
    win.show_view("Files")
    win.files_view.refresh()
    settle(app)
    shoot(win, "files")
    win.show_view("Device")
    win.device_view.refresh()
    win.device_view._load_free_space()
    win.device_view.set_battery(Battery(78, "Discharging"))
    settle(app)
    shoot(win, "device")
    win.close()
    settle(app)
    return 0


if __name__ == "__main__":
    sys.exit(main())
