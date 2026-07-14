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


def test_default_runtime_dir_respects_xdg_runtime_dir(
    monkeypatch, tmp_path: Path
) -> None:
    runtime_dir = tmp_path / "xdg-runtime"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))

    assert config_module._get_default_runtime_dir() == runtime_dir / "ezhpcy"


def test_default_runtime_dir_falls_back_to_per_user_temp(monkeypatch) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(config_module.tempfile, "gettempdir", lambda: "/tmp")
    monkeypatch.setattr(config_module.os, "getuid", lambda: 1234, raising=False)

    assert config_module._get_default_runtime_dir() == Path("/tmp/ezhpcy-1234")


def test_lsf_resource_reservations_default_to_dtu_per_task_policy() -> None:
    assert config_module.HPCConfig().lsf_resource_reserve_per_task
    assert not config_module.HPCConfig(
        lsf_resource_reserve_per_task=False
    ).lsf_resource_reserve_per_task
