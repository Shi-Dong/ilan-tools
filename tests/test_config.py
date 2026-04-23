"""Tests for ilan.config — load, save, defaults, get_workdir."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import ilan.config as cfg


class TestDefaults:
    def test_default_keys(self) -> None:
        expected = {
            "workdir", "num-agents", "model", "effort",
            "summarize-model", "summarize-effort",
            "time-zone", "editor", "api-key", "dashboard-interval",
            "line-number",
        }
        assert set(cfg.DEFAULTS.keys()) == expected

    def test_valid_keys_matches_defaults(self) -> None:
        assert cfg.VALID_KEYS == set(cfg.DEFAULTS.keys())

    def test_int_keys(self) -> None:
        assert cfg.INT_KEYS == {"num-agents", "dashboard-interval"}

    def test_bool_keys(self) -> None:
        assert cfg.BOOL_KEYS == {"line-number"}

    def test_line_number_default_false(self) -> None:
        assert cfg.DEFAULTS["line-number"] is False


class TestParseBool:
    @pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on", True])
    def test_truthy(self, value) -> None:
        assert cfg.parse_bool(value) is True

    @pytest.mark.parametrize("value", ["false", "0", "no", "off", "", False])
    def test_falsy(self, value) -> None:
        assert cfg.parse_bool(value) is False


class TestLastTailCache:
    def test_save_and_load_roundtrip(self, tmp_config: Path) -> None:
        cfg.save_last_tail("my-task", ["first", "second", "third"])
        assert cfg.load_last_tail("my-task") == ["first", "second", "third"]

    def test_load_returns_empty_when_missing(self, tmp_config: Path) -> None:
        assert cfg.load_last_tail("never-saved") == []

    def test_path_sanitises_task_name(self, tmp_config: Path) -> None:
        """Task names with slashes or spaces must not escape the cache dir."""
        cfg.save_last_tail("weird/name with spaces", ["x"])
        p = cfg.last_tail_path("weird/name with spaces")
        assert p.parent == cfg._last_tail_dir()
        assert cfg.load_last_tail("weird/name with spaces") == ["x"]

    def test_overwrite_previous_cache(self, tmp_config: Path) -> None:
        cfg.save_last_tail("t", ["a", "b"])
        cfg.save_last_tail("t", ["c"])
        assert cfg.load_last_tail("t") == ["c"]


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
