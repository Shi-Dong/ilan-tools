"""Shared fixtures for ilan test suite."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def tmp_workdir(tmp_path: Path) -> Path:
    """Return a temporary directory suitable for use as ilan's workdir.

    Creates the standard sub-directories that :class:`ilan.store.Store` expects.
    """
    for sub in ("logs", "output"):
        (tmp_path / sub).mkdir()
    return tmp_path


@pytest.fixture()
def tmp_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ilan's config file to a temporary location.

    Patches the module-level ``_CONFIG_DIR`` and ``_CONFIG_FILE`` in
    ``ilan.config`` so that tests don't touch ``~/.config/ilan``.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "config.json"

    import ilan.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "_CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "_CONFIG_FILE", config_file)
    return config_file


@pytest.fixture()
def mock_claude_bin(tmp_path: Path) -> Path:
    """Create a ``claude`` wrapper script that invokes :mod:`tests.mock_claude`.

    Returns the path to the wrapper.  Tests should prepend its parent
    directory to ``PATH`` so that ``subprocess.Popen(["claude", ...])``
    picks it up.
    """
    mock_src = Path(__file__).with_name("mock_claude.py")
    wrapper = tmp_path / "claude"
    wrapper.write_text(
        f"#!/usr/bin/env bash\nexec {sys.executable} {mock_src} \"$@\"\n"
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return wrapper


@pytest.fixture()
def env_with_mock_claude(mock_claude_bin: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put mock claude on PATH so that Runner._spawn finds it."""
    monkeypatch.setenv("PATH", f"{mock_claude_bin.parent}:{os.environ.get('PATH', '')}")
