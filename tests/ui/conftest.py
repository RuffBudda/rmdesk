import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"  # never real windows, even from a desktop shell

import pytest


@pytest.fixture(autouse=True)
def _no_real_prune(monkeypatch):
    """The Screen view's Compose mode prunes backups at startup; never touch the real backup dir in tests."""
    from rmdesk import safety
    monkeypatch.setattr(safety, "prune", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def _fast_start_recheck(monkeypatch):
    """Views call safety without an injected sleep: keep the post-start re-check (it still
    runs is-active) but without waiting START_RECHECK real seconds per start."""
    from rmdesk import safety
    monkeypatch.setattr(safety, "START_RECHECK", 0)


class _NoNetworkWeb:
    """Stands in for the tablet's USB web interface: a test never reaches 10.11.99.1."""

    def __init__(self, *a, **k):
        pass

    def available(self):
        return False

    def upload(self, path, folder_id):
        raise AssertionError("a test tried the real USB web interface")

    def download_pdf(self, doc_id, dest):
        raise AssertionError("a test tried the real USB web interface")


@pytest.fixture(autouse=True)
def _no_real_usb_web(monkeypatch):
    """MainWindow's default web factory resolves ops.UsbWeb when called: keep it offline."""
    from rmdesk.library import ops
    monkeypatch.setattr(ops, "UsbWeb", _NoNetworkWeb)


@pytest.fixture(autouse=True)
def _no_layout_probe(monkeypatch):
    """LiveCompose asks a newly connected tablet its layout in a worker (for the items that
    follow the page); the fake tablets' command logs stay as the tests expect without it.
    Tests of the probe turn it back on."""
    from rmdesk.ui.live_compose import LiveCompose
    monkeypatch.setattr(LiveCompose, "probe_layout_on_connect", False)


@pytest.fixture(autouse=True)
def _no_real_debug_dir(monkeypatch, tmp_path):
    """Scroll-fit debug captures go to the test's tmp dir, never ~/.local/share/rmdesk."""
    from rmdesk.ui import live_compose
    monkeypatch.setattr(live_compose, "DEBUG_DIR", tmp_path / "debug")
