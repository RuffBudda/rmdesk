#!/usr/bin/env python3
"""Draw calibration crosses on the page open in the notebook "rmdesk test".

Run:  ~/.local/share/rmdesk/venv/bin/python scripts/calibrate.py
Not imported by the app. Reads the tablet password from the keyring
(service "rmdesk", username = the configured serial); never prints it.
Landscape pages get three candidate transforms, each as five crosses plus an
"L" (down to a corner at frame (200, 200), then right): "identity" in black
fineliner, "rot90" in grey ballpoint, "rot270" in black marker (thicker).
"""
from __future__ import annotations

import sys

from rmdesk import config
from rmdesk.compose.calibration import (PEN_NAMES, NotCalibrationNotebook, build_page,
                                        confirm, guard)
from rmdesk.connection import USB_HOST, KeyringSecrets, default_factory
from rmdesk.device import DeviceError
from rmdesk.notebook.opened import NotEditable
from rmdesk.notebook.page import VerifyError
from rmdesk.safety import (BackupError, Cancelled, NotebookChanged, SendFailed,
                           send_to_open_page)


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
        device = default_factory(USB_HOST, password)
    except DeviceError as e:
        print(f"Can't connect to the tablet: {e}")
        return 1
    try:
        result, opened = send_to_open_page(device, build_page, confirm, guard=guard)
    except Cancelled:
        print("Cancelled; nothing was written.")
        return 1
    except NotEditable as e:
        print(f"Can't edit this page: {e.reason}")
        return 1
    except NotCalibrationNotebook as e:
        print(str(e))
        return 1
    except SendFailed as e:
        print(str(e))
        return 1
    except (DeviceError, BackupError, NotebookChanged, VerifyError) as e:
        print(f"Stopped, nothing written ({type(e).__name__}): {e}")
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Interrupted; nothing was written.")
        return 1
    except Exception as e:
        print(f"Unexpected error: {e}")
        return 1
    finally:
        device.close()
    print(f"Backup: {result.backup_dir}")
    print("Now look at the Screen view: the crosses that sit at the four corners "
          "and the centre of the picture show the right transform.")
    if opened.orientation == "landscape":
        print("Pens: " + "; ".join(f"{pen} = {name}" for name, pen in PEN_NAMES.items()) + ".")
        print("The right one also has its L near the top-left of the unrotated Screen view "
              "(frame 200, 200), upright: a stroke down, then a stroke right.")
        print("Please also say how each pen's L looks on the tablet itself.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
