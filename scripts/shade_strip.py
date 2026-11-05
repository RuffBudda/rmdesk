#!/usr/bin/env python3
"""Draw a pen test strip (three shaded gradients) on the page open in "rmdesk test".

Run:  ~/.local/share/rmdesk/venv/bin/python scripts/shade_strip.py
Not imported by the app. Reads the tablet password from the keyring
(service "rmdesk", username = the configured serial); never prints it.
"""
from __future__ import annotations

import functools
import sys

from rmdesk import config
from rmdesk.compose.calibration import NotCalibrationNotebook, confirm, guard
from rmdesk.compose.shade_strip import PROMPT, LandscapePage, build_page
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
        result, _opened = send_to_open_page(
            device, build_page,
            functools.partial(confirm, what="the pen test strip"), guard=guard)
    except Cancelled:
        print("Cancelled; nothing was written.")
        return 1
    except NotEditable as e:
        print(f"Can't edit this page: {e.reason}")
        return 1
    except (NotCalibrationNotebook, LandscapePage, SendFailed) as e:
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
    print(PROMPT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
