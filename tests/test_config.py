"""Tests for ilan.config — load, save, defaults, get_workdir."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import ilan.config as cfg


class TestDefaults:
    def test_default_keys(self) -> None:
        expected = {
            "workdir", "model-claude", "model-codex", "effort",
            "time-zone", "editor", "default-backend",
            "api-key-mode", "api-key-claude", "api-key-codex",
            "github-token",
            "dashboard-interval",
            "line-number", "markdown", "one-line-summary",
        }
        assert set(cfg.DEFAULTS.keys()) == expected

    def test_secret_keys(self) -> None:
        assert {
            "api-key-claude", "api-key-codex", "github-token"
        } == cfg.SECRET_KEYS
        assert cfg.SECRET_KEYS <= cfg.VALID_KEYS

    def test_default_backend_is_claude(self) -> None:
        assert cfg.DEFAULTS["default-backend"] == "claude"

    def test_api_key_codex_default_empty(self) -> None:
        assert cfg.DEFAULTS["api-key-codex"] == ""
        assert "api-key-codex" in cfg.SECRET_KEYS

    def test_api_key_mode_default_false(self) -> None:
        assert cfg.DEFAULTS["api-key-mode"] is False

    def test_github_token_default_empty(self) -> None:
        assert cfg.DEFAULTS["github-token"] == ""
        # Server-side (not a client-only key), so mirroring runs on the server.
        assert "github-token" not in cfg.CLIENT_SIDE_KEYS

    def test_valid_keys_matches_defaults(self) -> None:
        assert set(cfg.DEFAULTS.keys()) == cfg.VALID_KEYS

    def test_int_keys(self) -> None:
        assert {"dashboard-interval"} == cfg.INT_KEYS

    def test_bool_keys(self) -> None:
        assert {
            "api-key-mode", "line-number", "markdown", "one-line-summary",
        } == cfg.BOOL_KEYS

    def test_client_side_keys(self) -> None:
        assert {
            "dashboard-interval",
            "editor",
            "line-number",
            "markdown",
            "one-line-summary",
            "time-zone",
        } == cfg.CLIENT_SIDE_KEYS
        assert cfg.CLIENT_SIDE_KEYS <= cfg.VALID_KEYS

    def test_line_number_default_false(self) -> None:
        assert cfg.DEFAULTS["line-number"] is False

    def test_markdown_default_false(self) -> None:
        assert cfg.DEFAULTS["markdown"] is False

    def test_one_line_summary_default_true(self) -> None:
        assert cfg.DEFAULTS["one-line-summary"] is True


class TestParseBool:
    @pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on", True])
    def test_truthy(self, value) -> None:
        assert cfg.parse_bool(value) is True

    @pytest.mark.parametrize("value", ["false", "0", "no", "off", "", False])
    def test_falsy(self, value) -> None:
        assert cfg.parse_bool(value) is False


class TestResolveTimeZone:
    @pytest.mark.parametrize(
        "alias,expected",
        [
            ("china", "Asia/Shanghai"),
            ("beijing", "Asia/Shanghai"),
            ("shanghai", "Asia/Shanghai"),
            ("wuhan", "Asia/Shanghai"),
            ("japan", "Asia/Tokyo"),
            ("tokyo", "Asia/Tokyo"),
            ("korea", "Asia/Seoul"),
            ("seoul", "Asia/Seoul"),
            ("uk", "Europe/London"),
            ("london", "Europe/London"),
            ("pacific", "US/Pacific"),
            ("west", "US/Pacific"),
            ("western", "US/Pacific"),
            ("atlantic", "US/Eastern"),
            ("east", "US/Eastern"),
            ("eastern", "US/Eastern"),
        ],
    )
    def test_known_aliases(self, alias: str, expected: str) -> None:
        assert cfg.resolve_time_zone(alias) == expected

    @pytest.mark.parametrize("value", ["Tokyo", "TOKYO", "ToKyO", "  japan  "])
    def test_case_insensitive_and_trimmed(self, value: str) -> None:
        assert cfg.resolve_time_zone(value) == "Asia/Tokyo"

    @pytest.mark.parametrize("value", ["Asia/Tokyo", "US/Pacific", "UTC", "Europe/London"])
    def test_raw_iana_passes_through(self, value: str) -> None:
        assert cfg.resolve_time_zone(value) == value

    def test_unknown_value_passes_through_trimmed(self) -> None:
        assert cfg.resolve_time_zone("  Mars/Olympus  ") == "Mars/Olympus"


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
        assert conf["dashboard-interval"] == 1
        assert conf["model-claude"] == "claude-opus-4-7"
        assert conf["model-codex"] == "gpt-5.6-sol"

    def test_load_merges_with_defaults(self, tmp_config: Path) -> None:
        tmp_config.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_config, "w") as f:
            json.dump({"model-claude": "claude-sonnet-4-6"}, f)
        conf = cfg.load()
        assert conf["model-claude"] == "claude-sonnet-4-6"
        # Other defaults still present
        assert conf["dashboard-interval"] == 1
        assert conf["workdir"] == "~/.ilan"

    def test_load_preserves_user_overrides(self, tmp_config: Path) -> None:
        tmp_config.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_config, "w") as f:
            json.dump({"dashboard-interval": 10, "editor": "vim"}, f)
        conf = cfg.load()
        assert conf["dashboard-interval"] == 10
        assert conf["editor"] == "vim"

    def test_load_drops_unknown_keys(self, tmp_config: Path) -> None:
        """Keys removed from DEFAULTS in a newer version must not leak out
        of old config files (e.g. num-agents, summarize-model)."""
        tmp_config.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_config, "w") as f:
            json.dump({"editor": "vim", "num-agents": 5, "summarize-model": "sonnet"}, f)
        conf = cfg.load()
        assert conf["editor"] == "vim"
        assert "num-agents" not in conf
        assert "summarize-model" not in conf
        assert set(conf) == cfg.VALID_KEYS

    def test_load_migrates_alias_model_values_to_defaults(
        self, tmp_config: Path
    ) -> None:
        """Config files written before exact model ids were enforced may
        still hold aliases; load() must fall back to the defaults."""
        tmp_config.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_config, "w") as f:
            json.dump({"model-claude": "opus", "model-codex": "sol"}, f)
        conf = cfg.load()
        assert conf["model-claude"] == cfg.DEFAULTS["model-claude"]
        assert conf["model-codex"] == cfg.DEFAULTS["model-codex"]

    def test_load_keeps_exact_model_ids(self, tmp_config: Path) -> None:
        tmp_config.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_config, "w") as f:
            json.dump({"model-claude": "claude-fable-5-1",
                       "model-codex": "gpt-5.1-codex-max"}, f)
        conf = cfg.load()
        assert conf["model-claude"] == "claude-fable-5-1"
        assert conf["model-codex"] == "gpt-5.1-codex-max"


class TestSave:
    def test_save_writes_json(self, tmp_config: Path) -> None:
        cfg.save({"model-claude": "haiku", "dashboard-interval": 3})
        with open(tmp_config) as f:
            data = json.load(f)
        assert data["model-claude"] == "haiku"
        assert data["dashboard-interval"] == 3

    def test_save_creates_dir_if_needed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        nested = tmp_path / "deep" / "nested"
        config_file = nested / "config.json"
        monkeypatch.setattr(cfg, "_CONFIG_DIR", nested)
        monkeypatch.setattr(cfg, "_CONFIG_FILE", config_file)
        cfg.save({"model-claude": "opus"})
        assert config_file.exists()

    def test_roundtrip(self, tmp_config: Path) -> None:
        original = {"workdir": "/custom", "dashboard-interval": 8,
                     "model-claude": "claude-sonnet-4-6",
                     "model-codex": "gpt-5.1-codex-max",
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


class TestModelIdValidation:
    """model-claude / model-codex must hold exact model ids, not aliases."""

    @pytest.mark.parametrize("value", [
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
        "claude-fable-5-1",
    ])
    def test_accepts_exact_claude_ids(self, value: str) -> None:
        assert cfg.is_valid_model_id("model-claude", value)

    @pytest.mark.parametrize("value", [
        "opus",            # bare CLI alias
        "sonnet",
        "haiku",
        "claude-opus",     # no version digits — alias-shaped
        "Claude-Opus-4-7", # model ids are lowercase
        "claude-opus-4-7 ",
        "gpt-5.6-sol",     # wrong vendor for this key
        "",
    ])
    def test_rejects_illegal_claude_values(self, value: str) -> None:
        assert not cfg.is_valid_model_id("model-claude", value)

    @pytest.mark.parametrize("value", [
        "gpt-5.6-sol",
        "gpt-5.1-codex-max",
        "gpt-5-codex",
        "o3",
        "o4-mini",
    ])
    def test_accepts_exact_codex_ids(self, value: str) -> None:
        assert cfg.is_valid_model_id("model-codex", value)

    @pytest.mark.parametrize("value", [
        "sol",             # bare alias
        "codex",
        "gpt",
        "gpt-x",           # no version digits
        "gpt-5-",          # dangling separator
        "claude-opus-4-7", # wrong vendor for this key
        "",
    ])
    def test_rejects_illegal_codex_values(self, value: str) -> None:
        assert not cfg.is_valid_model_id("model-codex", value)

    def test_defaults_are_exact_model_ids(self) -> None:
        """Guards future edits: the shipped defaults must pass the guardrail."""
        for key in cfg.MODEL_KEYS:
            assert cfg.is_valid_model_id(key, str(cfg.DEFAULTS[key]))
