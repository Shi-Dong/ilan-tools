"""Tests for ilan.config — load, save, defaults, get_workdir."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import ilan.config as cfg


class TestDefaults:
    def test_default_keys(self) -> None:
        expected = {"workdir", "num-agents", "model", "effort", "time-zone", "editor"}
        assert set(cfg.DEFAULTS.keys()) == expected

    def test_valid_keys_matches_defaults(self) -> None:
        assert cfg.VALID_KEYS == set(cfg.DEFAULTS.keys())

    def test_int_keys(self) -> None:
        assert cfg.INT_KEYS == {"num-agents"}


class TestLoad:
    def test_load_creates_config_if_missing(self, tmp_config: Path) -> None:
        assert not tmp_config.exists()
        conf = cfg.load()
        assert tmp_config.exists()
        assert conf["num-agents"] == 5
        assert conf["model"] == "opus"

    def test_load_merges_with_defaults(self, tmp_config: Path) -> None:
        tmp_config.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_config, "w") as f:
            json.dump({"model": "sonnet"}, f)
        conf = cfg.load()
        assert conf["model"] == "sonnet"
        # Other defaults still present
        assert conf["num-agents"] == 5
        assert conf["workdir"] == "~/.ilan"

    def test_load_preserves_user_overrides(self, tmp_config: Path) -> None:
        tmp_config.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_config, "w") as f:
            json.dump({"num-agents": 10, "editor": "vim"}, f)
        conf = cfg.load()
        assert conf["num-agents"] == 10
        assert conf["editor"] == "vim"


class TestSave:
    def test_save_writes_json(self, tmp_config: Path) -> None:
        cfg.save({"model": "haiku", "num-agents": 3})
        with open(tmp_config) as f:
            data = json.load(f)
        assert data["model"] == "haiku"
        assert data["num-agents"] == 3

    def test_save_creates_dir_if_needed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        nested = tmp_path / "deep" / "nested"
        config_file = nested / "config.json"
        monkeypatch.setattr(cfg, "_CONFIG_DIR", nested)
        monkeypatch.setattr(cfg, "_CONFIG_FILE", config_file)
        cfg.save({"model": "opus"})
        assert config_file.exists()

    def test_roundtrip(self, tmp_config: Path) -> None:
        original = {"workdir": "/custom", "num-agents": 8, "model": "sonnet",
                     "effort": "low", "time-zone": "UTC", "editor": "nano"}
        cfg.save(original)
        loaded = cfg.load()
        for k, v in original.items():
            assert loaded[k] == v


class TestGetWorkdir:
    def test_default_workdir(self, tmp_config: Path) -> None:
        wd = cfg.get_workdir()
        assert wd == Path("~/.ilan").expanduser()

    def test_custom_workdir(self, tmp_config: Path) -> None:
        cfg.save({**cfg.DEFAULTS, "workdir": "/tmp/my-ilan"})
        wd = cfg.get_workdir()
        assert wd == Path("/tmp/my-ilan")

    def test_tilde_expansion(self, tmp_config: Path) -> None:
        cfg.save({**cfg.DEFAULTS, "workdir": "~/my-ilan-dir"})
        wd = cfg.get_workdir()
        assert "~" not in str(wd)
        assert str(wd).endswith("my-ilan-dir")
