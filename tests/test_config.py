import logging
from datetime import timedelta

import pytest
from pydantic import ValidationError

from ezhpcy import config as config_module
from ezhpcy.logging import LOG_LEVEL_ENVIRONMENT_VARIABLE, LogLevel
from ezhpcy.scheduler.types import SchedulerType
from ezhpcy.types import ResolvedConfig


def test_log_level_is_case_insensitive() -> None:
    config = config_module.Config.from_mapping({"log_level": "debug"})

    assert config.log_level is LogLevel.DEBUG


def test_log_level_can_be_overridden_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv(LOG_LEVEL_ENVIRONMENT_VARIABLE, "warning")

    config = config_module.Config()

    assert config.log_level is LogLevel.WARNING


def test_invalid_log_level_is_rejected() -> None:
    with pytest.raises(ValidationError, match="log_level"):
        config_module.Config.from_mapping({"log_level": "verbose"})


def test_auto_provision_defaults_to_true() -> None:
    assert config_module.Config.from_mapping({}).auto_provision is True


def test_auto_provision_can_be_disabled_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("EZHPCY_AUTO_PROVISION", "FALSE")

    config = config_module.Config()

    assert config.auto_provision is False


def test_config_singleton_applies_its_log_level() -> None:
    assert isinstance(config_module.config, config_module.Config)
    assert (
        logging.getLogger("ezhpcy").level
        == logging.getLevelNamesMapping()[config_module.config.log_level]
    )


def test_nested_profile_inheritance_resolves_all_ancestor_values() -> None:
    config = config_module.Config.from_mapping(
        {
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
        config_module.Config.from_mapping({"profile": profiles})


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
