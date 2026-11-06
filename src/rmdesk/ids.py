"""Notebook and page IDs read from the tablet, checked before they go into a path.

xochitl names them with UUIDs. Anything else that is only letters, digits, "-" and "_" is
accepted too (never ".", "/" or a leading "-"), so an ID can never step out of the folder
it is joined to, on the tablet or in rmdesk's backups on this computer.
"""
from __future__ import annotations

import re

_SAFE = re.compile(r"[0-9A-Za-z][0-9A-Za-z_-]{0,63}")


def is_safe(value: object) -> bool:
    return isinstance(value, str) and _SAFE.fullmatch(value) is not None
