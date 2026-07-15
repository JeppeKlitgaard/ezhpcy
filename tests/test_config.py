from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from ezhpcy import config as config_module
from ezhpcy.scheduler.types import SchedulerType
from ezhpcy.types import ResolvedConfig


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


def test_nested_profile_inheritance_resolves_all_ancestor_values() -> None:
    config = config_module.LocalConfig.from_mapping(
        {
            "default_profile": "default",
            "profile": {
                "default": {
                    "description": "Base LSF profile",
                    "host": "login.example.com",
                    "user": "alice",
                    "scheduler": "LSF",
                    "queue": "normal",
                    "lsf_resource_reserve_per_task": True,
                },
                "batch": {
                    "inherit": "default",
                    "queue": "batch",
                    "exclusive": True,
                },
                "gpu": {
                    "description": "GPU jobs",
                    "inherit": "batch",
                    "queue": "gpu",
                    "cores": 8,
                    "memory": "32GB",
                    "time_limit": "1:00",
                },
            },
        }
    )

    profile = config.resolve_profile("gpu")

    assert str(profile.host) == "login.example.com"
    assert profile.description == "GPU jobs"
    assert profile.user == "alice"
    assert profile.scheduler is SchedulerType.LSF
    assert profile.queue == "gpu"
    assert profile.cores == 8
    assert profile.exclusive
    assert int(profile.memory) == 32_000_000_000
    assert profile.time_limit_delta == timedelta(hours=1)
    assert profile.lsf_resource_reserve_per_task


@pytest.mark.parametrize(
    ("profiles", "message"),
    [
        (
            {"a": {"inherit": "b"}, "b": {"inherit": "a"}},
            "inheritance cycle",
        ),
        ({"a": {"inherit": "missing"}}, "inherits unknown profile"),
        ({"not allowed": {"host": "login.example.com"}}, "profile names"),
    ],
)
def test_invalid_profile_graphs_are_rejected(
    profiles: dict[str, dict[str, object]], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        config_module.LocalConfig.from_mapping({"profile": profiles})


@pytest.mark.parametrize(
    "values",
    [
        {"host": None, "user": "alice"},
        {"host": "login.example.com", "user": None},
    ],
)
def test_resolved_config_requires_host_and_user(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ResolvedConfig.model_validate(values)
