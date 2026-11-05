import pytest

from rmdesk import battery
from rmdesk.battery import Battery
from rmdesk.device import CommandResult
from tests.fakes import FakeDevice

PS = battery.POWER_SUPPLY


def supplies(**dirs):
    """{name: (type, capacity, status)} as files under /sys/class/power_supply."""
    files = {}
    for name, (typ, cap, st) in dirs.items():
        files[f"{PS}/{name}/type"] = (typ + "\n").encode()
        if cap is not None:
            files[f"{PS}/{name}/capacity"] = (cap + "\n").encode()
        if st is not None:
            files[f"{PS}/{name}/status"] = (st + "\n").encode()
    return files


def test_reads_the_users_tablet():
    dev = FakeDevice(supplies(max77818_battery=("Battery", "56", "Discharging"),
                              max77818_charger=("USB", "0", "Charging")))
    assert battery.read(dev) == Battery(56, "Discharging")
    assert [m for m, _ in dev.log] == ["run"]  # one command


def test_found_by_type_not_by_name():
    # the charger sorts first and has a capacity file; the battery has another name
    dev = FakeDevice(supplies(a_charger=("Mains", "99", "Charging"),
                              zz_pack=("Battery", "80", "Full")))
    assert battery.read(dev) == Battery(80, "Full")


def test_no_battery_reads_nothing():
    assert battery.read(FakeDevice(supplies(usb=("USB", "0", "Charging")))) is None
    assert battery.read(FakeDevice({})) is None


@pytest.mark.parametrize("status", ["Charging", "Discharging", "Full", "Not charging",
                                    "Unknown"])
def test_statuses(status):
    b = battery.parse(f"56\n{status}\n")
    assert b == Battery(56, status)
    assert b.charging == (status == "Charging")


@pytest.mark.parametrize("out", ["", "56\n", "abc\nDischarging\n", "56\nExploding\n",
                                 "101\nFull\n", "-1\nFull\n", "56\nCharging\nextra\n",
                                 "cat: /x: No such file\n", "\x00\x01"])
def test_garbage_reads_nothing(out):
    assert battery.parse(out) is None


def test_failed_command_reads_nothing():
    dev = FakeDevice(run_handler=lambda cmd: CommandResult(1, "56\nCharging\n", "boom"))
    assert battery.read(dev) is None


def test_missing_status_file_reads_nothing():
    dev = FakeDevice(supplies(bat=("Battery", "56", None)))
    assert battery.read(dev) is None


def test_texts_and_low():
    assert Battery(56, "Discharging").short() == "56%"
    assert Battery(56, "Charging").short() == "56% ⚡"
    assert Battery(56, "Discharging").detail() == "56% · Discharging"
    assert Battery(19, "Discharging").low
    assert not Battery(20, "Discharging").low
    assert not Battery(5, "Charging").low and not Battery(5, "Not charging").low
