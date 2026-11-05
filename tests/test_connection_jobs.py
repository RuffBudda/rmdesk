"""The device a job uses is never closed under it; a reconnect restarts a stopped screen."""
from rmdesk import safety
from rmdesk.config import Config
from rmdesk.connection import SCREEN_RESTARTED, Connection, State
from rmdesk.device import CommandResult, DeviceError
from tests.fakes import FakeDevice
from tests.test_connection import FILES, Secrets


class Closable(FakeDevice):
    closed = False

    def _call(self, m, a):
        if self.closed and m != "close":
            raise DeviceError("SSH session not active")
        super()._call(m, a)

    def close(self):
        super().close()
        self.closed = True


def make(devices=None):
    made = []
    devices = iter(devices) if devices is not None else None

    def factory(host, password):
        d = next(devices) if devices is not None else Closable(files=FILES)
        made.append(d)
        return d
    c = Connection(Config(), factory=factory, secrets=Secrets(), sleep=lambda s: None,
                   defer=lambda fn: fn())
    return c, made


def test_connection_lost_during_job_keeps_the_device_open():
    c, made = make()
    c.connect()
    d = made[0]
    assert c.try_begin_job("sending to the tablet")
    c.connection_lost(DeviceError("read X: No such file"))
    assert not d.closed and c.device is d
    c.end_job()
    assert d.closed and c.device is None and c.state == State.OFFLINE


def test_connect_during_job_is_deferred_until_end_job():
    c, made = make()
    c.connect()
    d = made[0]
    assert c.try_begin_job("sending to the tablet")
    c.connect()
    assert not d.closed and c.device is d and len(made) == 1
    c.end_job()
    assert d.closed and len(made) == 2 and c.device is made[1]
    assert c.state == State.USB


def test_job_using_the_device_is_unaffected_by_another_views_errors():
    c, made = make()
    c.connect()
    d = made[0]
    with c.job("sending"):
        c.connection_lost(DeviceError("x"))
        c.connect()
        safety.stop_xochitl(d, sleep=lambda s: None)
        safety.start_xochitl(d, sleep=lambda s: None)
    assert d.xochitl_active


def test_deferred_lost_then_connect_runs_once():
    c, made = make()
    c.connect()
    with c.job("x"):
        c.connection_lost(DeviceError("x"))
        c.connect()
    assert len(made) == 2 and c.device is made[1]


def test_connect_without_a_device_inside_the_job_still_connects():
    # the job's own ensure() after a dropped link: nothing to close, so connect now
    c, made = make()
    with c.job("x"):
        assert c.ensure() is made[0]


def test_reconnect_starts_a_stopped_screen_and_says_so():
    stopped = Closable(files=FILES)
    stopped.xochitl_active = False
    c, made = make([stopped])
    notices = []
    c.on_notice.append(notices.append)
    c.connect()
    assert stopped.xochitl_active
    runs = [a for m, a in stopped.log if m == "run"]
    assert "systemctl start xochitl" in runs
    assert runs.index("systemctl try-restart goMarkableStream") > runs.index("systemctl start xochitl")
    assert notices == [SCREEN_RESTARTED]
    assert SCREEN_RESTARTED == "The tablet's screen was stopped; rmdesk restarted it."
    assert c.notice == SCREEN_RESTARTED


def test_reconnect_with_running_screen_starts_nothing():
    c, made = make()
    notices = []
    c.on_notice.append(notices.append)
    c.connect()
    runs = [a for m, a in made[0].log if m == "run"]
    assert "systemctl is-active xochitl" in runs
    assert "systemctl start xochitl" not in runs
    assert notices == [] and c.notice is None


def test_reconnect_inside_a_job_never_starts_xochitl():
    stopped = Closable(files=FILES)
    stopped.xochitl_active = False
    c, made = make([stopped])
    with c.job("x"):
        c.ensure()
        assert not stopped.xochitl_active


def test_deferred_reconnect_after_job_starts_stopped_screen():
    first = Closable(files=FILES)
    second = Closable(files=FILES)
    c, made = make([first, second])
    c.connect()
    with c.job("sending"):
        c.connection_lost(DeviceError("x"))
        safety.stop_xochitl(first, sleep=lambda s: None)
        second.xochitl_active = False  # the same tablet, seen over the new link
        c.connect()
    assert second.xochitl_active and c.notice == SCREEN_RESTARTED


def test_failed_screen_restart_is_reported():
    def h(cmd):
        if cmd == "systemctl is-active xochitl":
            return CommandResult(3, "inactive\n", "")
        if cmd == "systemctl start xochitl":
            return CommandResult(1, "", "boom")
        return None
    d = Closable(files=FILES, run_handler=h)
    d.xochitl_active = False
    c, _ = make([d])
    notices = []
    c.on_notice.append(notices.append)
    assert c.connect() == State.USB
    assert len(notices) == 1 and "restart" in notices[0] and "power button" in notices[0]


def test_screen_check_errors_never_break_connect():
    def h(cmd):
        if "xochitl" in cmd:
            raise DeviceError("flaky")
        return None
    c, _ = make([Closable(files=FILES, run_handler=h)])
    assert c.connect() == State.USB


def test_repro_shared_close_with_job_slot(tmp_path):
    """The reviewer's repro, with the job slot the Screen view really holds."""
    from rmdesk.notebook import page
    from tests.test_safety_live import FIX, _lines
    from tests.test_safety_live import device as live_device
    orig = (FIX / "Normal_AB.rm").read_bytes()
    d = Closable(files=live_device(orig).files)
    c, _ = make([d])
    c.connect()

    def build(op):
        c.connection_lost(DeviceError("read X: No such file"))
        c.connect()
        p = page.load(op.data)
        p.append_to_rmdesk_layer(_lines(2))
        return p.to_bytes(), 2
    with c.job("sending to the tablet"):
        safety.send_to_open_page(d, build, lambda op: True, root=tmp_path, sleep=lambda s: None)
    assert d.xochitl_active
