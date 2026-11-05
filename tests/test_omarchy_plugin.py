"""The repository is also an Omarchy shell plugin (manifest.json at the root)."""
import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "manifest.json").read_text())


def test_manifest_matches_the_package():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert MANIFEST["version"] == project["version"]
    assert MANIFEST["license"] == project["license"] == "MIT"
    assert MANIFEST["schemaVersion"] == 1 and MANIFEST["kinds"] == ["bar-widget"]
    assert MANIFEST["id"] == "io.github.ruffbudda.rmdesk"


def test_entry_point_and_scripts_exist():
    assert (ROOT / MANIFEST["entryPoints"]["barWidget"]).is_file()
    for script in ("omarchy/open.sh", "scripts/install.sh", "scripts/uninstall.sh",
                   "scripts/setup-venv.sh", "scripts/install-desktop.sh"):
        assert os.access(ROOT / script, os.X_OK), script
    assert (ROOT / "omarchy/i18n/en.json").is_file() and (ROOT / "LICENSE").is_file()


def test_no_symlinks_in_the_plugin_folder():
    links = [p for p in ROOT.rglob("*") if p.is_symlink() and ".git" not in p.parts]
    assert links == []


@pytest.mark.skipif(shutil.which("omarchy-plugin-validate") is None, reason="not on Omarchy")
def test_omarchy_accepts_the_manifest():
    res = subprocess.run(["omarchy-plugin-validate", str(ROOT)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
