"""The tablet's battery: charge and charging state from /sys/class/power_supply.

The battery is found by its `type` ("Battery"), not by name (max77818_battery on the rM2).
One cheap shell command reads it; anything unexpected reads as "unknown" (None), never an
error the user sees."""
from __future__ import annotations

from dataclasses import dataclass

from rmdesk.device import Device

POWER_SUPPLY = "/sys/class/power_supply"
# prints the battery's capacity then its status, one per line (nothing when there is none)
READ_CMD = (f'for d in {POWER_SUPPLY}/*; do '
            'if [ "$(cat "$d/type" 2>/dev/null)" = Battery ]; then '
            'cat "$d/capacity" "$d/status"; break; fi; done')
STATUSES = ("Charging", "Discharging", "Full", "Not charging", "Unknown")
LOW = 20  # below this (and discharging) the charge shows in the warn colour


@dataclass(frozen=True)
class Battery:
    percent: int
    status: str

    @property
    def charging(self) -> bool:
        return self.status == "Charging"

    @property
    def low(self) -> bool:
        return self.percent < LOW and self.status == "Discharging"

    def short(self) -> str:
        """For the connection pill: "56%", "56% ⚡" while charging."""
        return f"{self.percent}% ⚡" if self.charging else f"{self.percent}%"

    def detail(self) -> str:
        """For the Device view: "56% · Discharging"."""
        return f"{self.percent}% · {self.status}"


def parse(out: str) -> Battery | None:
    """READ_CMD's output (capacity line, status line); None for anything else."""
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    if len(lines) != 2 or not lines[0].isdigit():
        return None
    percent, status = int(lines[0]), lines[1]
    if not 0 <= percent <= 100 or status not in STATUSES:
        return None
    return Battery(percent, status)


def read(device: Device) -> Battery | None:
    """The battery now, or None if it can't be read. DeviceError (a lost link) propagates."""
    res = device.run(READ_CMD, timeout=10)
    return parse(res.out) if res.code == 0 else None
