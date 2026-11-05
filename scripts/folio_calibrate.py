#!/usr/bin/env python3
"""Draw a labelled grid of crosses at known PAGE coordinates on the page open in "rmdesk test".

For working out how the tablet shows a portrait page while the Type Folio is attached
(keyboard layout: turned, scaled and scrolled). Each cross is labelled with its page
coordinates ("x,y"), so any part of the page that is on screen identifies itself.
After it runs, press Screenshot in rmdesk (Screen, View mode) and tell Claude.

Run:  ~/.local/share/rmdesk/venv/bin/python scripts/folio_calibrate.py
Uses USB if the cable is in, otherwise the saved Wi-Fi address. Not imported by the app.
Reads the tablet password from the keyring; never prints it. Only writes to a notebook
named exactly "rmdesk test", through the same safety path as the app (backup, checks, undo).
"""
from __future__ import annotations

import sys

from rmdesk import config
from rmdesk.compose.build import _page_line
from rmdesk.compose.calibration import NotCalibrationNotebook, confirm, guard
from rmdesk.compose.inktext import text_to_polylines
from rmdesk.compose.items import PenStyle
from rmdesk.connection import USB_HOST, KeyringSecrets, default_factory
from rmdesk.device import DeviceError
from rmdesk.firmware import is_acceptance_name
from rmdesk.notebook import page as pagemod
from rmdesk.notebook.opened import NotEditable, OpenedPage
from rmdesk.notebook.page import VerifyError
from rmdesk.safety import (BackupError, Cancelled, NotebookChanged, SendFailed,
                           send_to_open_page)

XS = (-600, 0, 600)
YS = (100, 400, 700, 1000, 1300, 1600, 1800)
ARM = 12
LABEL = 22  # label cap height, page px
PEN = PenStyle("fineliner", "black")


def marks() -> list:
    lines = []
    for x in XS:
        for y in YS:
            lines.append(_page_line(PEN, [(x - ARM, y), (x + ARM, y)]))
            lines.append(_page_line(PEN, [(x, y - ARM), (x, y + ARM)]))
            text = text_to_polylines(f"{x},{y}", LABEL)
            for pl in text.lines:  # label to the right of and below the cross
                lines.append(_page_line(PEN, [(x + ARM + 6 + px, y + 6 + py) for px, py in pl]))
    return lines


def build_page(opened: OpenedPage) -> tuple[bytes, int]:
    if opened.orientation != "portrait":
        raise NotEditable("Use a portrait page for this check.")
    lines = marks()
    p = pagemod.load(opened.data)
    p.append_to_rmdesk_layer(lines)
    return p.to_bytes(), len(lines)


def ask(opened: OpenedPage) -> bool:
    """Refuse a notebook that isn't "rmdesk test" before asking anything (the guard checks
    again after the backup)."""
    if not is_acceptance_name(opened.name):
        raise NotCalibrationNotebook(
            f"The tablet has \u201c{opened.name}\u201d open, not \u201crmdesk test\u201d. "
            "Open rmdesk test (any page with a dot on it) and run this again. Nothing was written.")
    if opened.orientation != "portrait":
        raise NotEditable("This rmdesk test page is landscape. Switch the notebook to portrait "
                          "on the tablet (or open a portrait rmdesk test) and run this again.")
    return confirm(opened, what="a labelled grid of crosses")


def _connect(password: str):
    try:
        return default_factory(USB_HOST, password)
    except DeviceError:
        cfg = config.load()
        if not cfg.wifi_ip:
            raise
        return default_factory(cfg.wifi_ip, password)


def main() -> int:
    cfg = config.load()
    if not cfg.serial:
        print("No tablet serial in the rmdesk config; connect once in rmdesk first.")
        return 1
    password = KeyringSecrets().get(cfg.serial)
    if password is None:
        print(f"No stored tablet password for {cfg.serial}. Connect once in rmdesk first.")
        return 1
    try:
        device = _connect(password)
    except DeviceError as e:
        print(f"Can't connect to the tablet: {e}")
        return 1
    finally:
        del password
    try:
        result, _opened = send_to_open_page(
            device, build_page, ask, guard=guard)
    except Cancelled:
        print("Cancelled; nothing was written.")
        return 1
    except NotEditable as e:
        print(f"Can't edit this page: {e.reason}")
        return 1
    except (NotCalibrationNotebook, SendFailed) as e:
        print(str(e))
        return 1
    except (DeviceError, BackupError, NotebookChanged, VerifyError) as e:
        print(f"Stopped, nothing written ({type(e).__name__}): {e}")
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Interrupted; nothing was written.")
        return 1
    finally:
        device.close()
    print(f"Backup: {result.backup_dir}")
    print("Now, without scrolling, press Screenshot in rmdesk (Screen, View mode) and tell "
          "Claude. Then scroll down a little, press Screenshot again, and tell Claude that too.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
