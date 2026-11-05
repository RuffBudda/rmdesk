from rmdesk.config import Config, load, save


def test_config_roundtrip(tmp_path):
    p = tmp_path / "config.toml"
    save(Config(wifi_ip="192.168.1.50", last_send={"k": "v"}), p)
    cfg = load(p)
    assert cfg.wifi_ip == "192.168.1.50"
    assert cfg.last_send == {"k": "v"}
    assert cfg.last_firmware is None


def test_config_missing_file_defaults(tmp_path):
    assert load(tmp_path / "nope.toml") == Config()


def test_config_roundtrip_special_strings(tmp_path):
    p = tmp_path / "config.toml"
    nasty = 'q" b\\ nl\n tab\t emoji \U0001F600 café 日本 del\x7f'
    save(Config(wifi_ip=nasty, last_send={nasty: nasty}), p)
    cfg = load(p)
    assert cfg.wifi_ip == nasty
    assert cfg.last_send == {nasty: nasty}


def test_config_corrupt_file_moved_aside(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("wifi_ip = = broken")
    assert load(p) == Config()
    assert not p.exists()
    assert (tmp_path / "config.toml.bad").read_text() == "wifi_ip = = broken"


def test_config_save_failure_cleans_temp(tmp_path, monkeypatch):
    import os
    import pytest
    p = tmp_path / "config.toml"

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        save(Config(wifi_ip="x"), p)
    assert list(tmp_path.iterdir()) == []


def test_screen_rotation_roundtrip_and_invalid_fallback(tmp_path):
    p = tmp_path / "config.toml"
    assert Config().screen_rotation == 0
    save(Config(screen_rotation=270), p)
    assert load(p).screen_rotation == 270
    for bad in ("45", "-90", "360", '"90"', "true", "90.0"):
        p.write_text(f"screen_rotation = {bad}\n")
        assert load(p).screen_rotation == 0, bad


def test_old_config_with_sync_warning_shown_still_loads(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('wifi_ip = "10.0.0.2"\nsync_warning_shown = ["a", "b"]\n')
    cfg = load(p)
    assert cfg.wifi_ip == "10.0.0.2"
    assert not hasattr(cfg, "sync_warning_shown")


def test_sidebar_collapsed_roundtrip_and_invalid_fallback(tmp_path):
    p = tmp_path / "config.toml"
    assert Config().sidebar_collapsed is False
    save(Config(sidebar_collapsed=True), p)
    assert load(p).sidebar_collapsed is True
    p.write_text('sidebar_collapsed = "yes"\n', encoding="utf-8")
    assert load(p).sidebar_collapsed is False
