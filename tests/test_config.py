from pathlib import Path

from ezhpcy import config as config_module


def test_default_config_dir_respects_xdg_config_home(monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg-config")

    assert config_module._get_default_config_dir() == Path("/tmp/xdg-config/ezhpcy")


def test_default_cache_dir_respects_xdg_cache_home(monkeypatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/xdg-cache")

    assert config_module._get_default_cache_dir() == Path("/tmp/xdg-cache/ezhpcy")


def test_default_data_dir_respects_xdg_data_home(monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdg-data")

    assert config_module._get_default_data_dir() == Path("/tmp/xdg-data/ezhpcy")
