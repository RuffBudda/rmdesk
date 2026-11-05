"""Persistent user config stored as TOML at ~/.config/rmdesk/config.toml."""
from __future__ import annotations

import json
import os
import tempfile
import tomllib
from dataclasses import asdict, dataclass, fields
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "rmdesk"
CONFIG_PATH = CONFIG_DIR / "config.toml"


@dataclass
class Config:
    wifi_ip: str | None = None
    last_firmware: str | None = None
    last_send: dict | None = None
    serial: str | None = None
    screen_rotation: int = 0  # 0/90/180/270, remembered from the Screen view
    confirm_sends: bool = False  # ask "Write to the tablet?" before each Compose send
    sidebar_collapsed: bool = False  # unused since the sidebar went; kept so old configs load


def load(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Config()
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        os.replace(path, path.with_name(path.name + ".bad"))
        return Config()
    known = {f.name for f in fields(Config)}
    cfg = Config(**{k: v for k, v in data.items() if k in known})
    if type(cfg.screen_rotation) is not int or cfg.screen_rotation not in (0, 90, 180, 270):
        cfg.screen_rotation = 0
    if type(cfg.confirm_sends) is not bool:
        cfg.confirm_sends = False
    if type(cfg.sidebar_collapsed) is not bool:
        cfg.sidebar_collapsed = False
    return cfg


def _string(s: str) -> str:
    # JSON escapes (\n, \", \uXXXX for controls) are valid TOML; keep non-ASCII
    # literal (surrogate-pair escapes are invalid TOML) and escape DEL by hand.
    return json.dumps(s, ensure_ascii=False).replace("\x7f", "\\u007f")


def _value(v) -> str:
    if isinstance(v, str):
        return _string(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_value(i) for i in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{_string(k)} = {_value(i)}" for k, i in v.items()) + "}"
    raise TypeError(f"unsupported config value: {v!r}")


def dumps(cfg: Config) -> str:
    return "".join(f"{k} = {_value(v)}\n" for k, v in asdict(cfg).items() if v is not None)


def save(cfg: Config, path: Path | None = None) -> None:
    path = path or CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(dumps(cfg))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
