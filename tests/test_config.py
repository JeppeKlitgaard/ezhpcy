import logging
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from ezhpcy import config as config_module
from ezhpcy.scheduler.types import SchedulerType
from ezhpcy.types import ResolvedConfig


def test_log_level_is_case_insensitive() -> None:
    config = config_module.Config.from_mapping({"log_level": "debug"})

    assert config.log_level == logging.DEBUG


def test_log_level_can_be_overridden_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("EZHPCY_LOG_LEVEL", "warning")

    config = config_module.Config()

    assert config.log_level == logging.WARNING


def test_invalid_log_level_is_rejected() -> None:
    with pytest.raises(ValidationError, match="log_level"):
        config_module.Config.from_mapping({"log_level": "verbose"})


def test_debug_defaults_to_false() -> None:
    assert config_module.Config.from_mapping({}).debug is False


def test_debug_can_be_enabled_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("EZHPCY_DEBUG", "TRUE")

    config = config_module.Config()

    assert config.debug is True


def test_auto_provision_defaults_to_true() -> None:
    assert config_module.Config.from_mapping({}).auto_provision is True


def test_auto_provision_can_be_disabled_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("EZHPCY_AUTO_PROVISION", "FALSE")

    config = config_module.Config()

    assert config.auto_provision is False


def test_profile_password_prompt_defaults_to_true_and_is_inherited() -> None:
    config = config_module.Config.from_mapping(
        {
            "profile": {
                "base": {"connection": {"password_prompt": False}},
                "child": {"inherit": "base"},
                "default": {},
            }
        }
    )

    assert config.resolve_profile("default").connection.password_prompt is True
    assert config.resolve_profile("child").connection.password_prompt is False


def test_profile_ssh_keepalive_defaults_to_30_seconds_and_is_inherited() -> None:
    config = config_module.Config.from_mapping(
        {
            "profile": {
                "base": {"connection": {"ssh_keepalive_interval_seconds": 75}},
                "child": {"inherit": "base"},
                "default": {},
            }
        }
    )

    default = config.resolve_profile("default").connection
    child = config.resolve_profile("child").connection
    assert default.ssh_keepalive_interval_seconds == 30
    assert child.ssh_keepalive_interval_seconds == 75


def test_profile_ssh_keepalive_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="ssh_keepalive_interval_seconds"):
        config_module.Config.from_mapping(
            {"profile": {"base": {"connection": {"ssh_keepalive_interval_seconds": 0}}}}
        )


def test_profile_worker_heartbeat_is_independent_and_inherited() -> None:
    config = config_module.Config.from_mapping(
        {
            "profile": {
                "base": {
                    "connection": {"ssh_keepalive_interval_seconds": 75},
                    "timings": {
                        "worker_heartbeat_interval_seconds": 20,
                        "worker_heartbeat_timeout_seconds": 60,
                    },
                },
                "child": {"inherit": "base"},
                "default": {},
            }
        }
    )

    default = config.resolve_profile("default")
    child = config.resolve_profile("child")
    assert default.timings.worker_heartbeat_interval_seconds == 30
    assert default.timings.worker_heartbeat_timeout_seconds == 90
    assert child.connection.ssh_keepalive_interval_seconds == 75
    assert child.timings.worker_heartbeat_interval_seconds == 20
    assert child.timings.worker_heartbeat_timeout_seconds == 60


def test_profile_job_intervals_default_and_are_inherited() -> None:
    config = config_module.Config.from_mapping(
        {
            "profile": {
                "base": {
                    "timings": {
                        "job_poll_interval_seconds": 4.5,
                        "job_monitor_interval_seconds": 120,
                    }
                },
                "child": {"inherit": "base"},
                "default": {},
            }
        }
    )

    default = config.resolve_profile("default").timings
    child = config.resolve_profile("child").timings
    assert default.job_poll_interval_seconds == 2.5
    assert default.job_monitor_interval_seconds == 60
    assert child.job_poll_interval_seconds == 4.5
    assert child.job_monitor_interval_seconds == 120


@pytest.mark.parametrize(
    "field", ["job_poll_interval_seconds", "job_monitor_interval_seconds"]
)
def test_profile_job_intervals_must_be_positive(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        config_module.Config.from_mapping(
            {"profile": {"base": {"timings": {field: 0}}}}
        )


def test_profile_password_keyring_can_be_enabled() -> None:
    config = config_module.Config.from_mapping(
        {"profile": {"base": {"connection": {"password_keyring": True}}}}
    )

    assert config.resolve_profile("base").connection.password_keyring is True


def test_profile_password_sources_are_mutually_exclusive() -> None:
    config = config_module.Config.from_mapping(
        {
            "profile": {
                "base": {
                    "connection": {
                        "password_file": "password.txt",
                        "password_keyring": True,
                    }
                }
            }
        }
    )

    with pytest.raises(
        ValueError,
        match=r"connection\.password_file, connection\.password_keyring",
    ):
        config.resolve_profile("base")


def test_profile_password_is_rejected_to_keep_secrets_out_of_configuration() -> None:
    config = config_module.Config.from_mapping(
        {"profile": {"base": {"connection": {"password": "not-a-secret"}}}}
    )

    with pytest.raises(
        config_module.ProfilePasswordSourceError, match="must not be stored"
    ):
        config.resolve_profile("base")


def test_profile_password_source_overrides_the_inherited_source() -> None:
    config = config_module.Config.from_mapping(
        {
            "profile": {
                "base": {
                    "connection": {
                        "user": "alice",
                        "password_keyring": True,
                    }
                },
                "child": {
                    "inherit": "base",
                    "connection": {"password_file": "password.txt"},
                },
            }
        }
    )

    connection = config.resolve_profile("child").connection
    assert connection.password_keyring is False
    assert connection.password_file == Path("password.txt")
    # Only the password source is replaced; the rest is still inherited.
    assert connection.user == "alice"


def test_local_file_cannot_be_set_in_the_config_file(tmp_path: Path) -> None:
    config_file = tmp_path / "ezhpcy.toml"
    config_file.write_text(
        '[local_file]\nconfig_file = "other.toml"\n', encoding="utf-8"
    )

    class FileConfig(config_module.Config):
        model_config = SettingsConfigDict(toml_file=config_file)

    with pytest.raises(ValidationError, match="local_file cannot be configured"):
        FileConfig()


def test_local_file_cannot_be_set_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("EZHPCY_LOCAL_FILE", '{"config_dir": "elsewhere"}')

    with pytest.raises(ValidationError, match="local_file cannot be configured"):
        config_module.Config()


def test_config_singleton_applies_its_log_level() -> None:
    assert isinstance(config_module.config, config_module.Config)
    # A workstation config with `debug = true` overrides the log level.
    expected_level = (
        logging.DEBUG if config_module.config.debug else config_module.config.log_level
    )
    assert logging.getLogger("ezhpcy").level == expected_level


def test_nested_profile_inheritance_resolves_all_ancestor_values() -> None:
    config = config_module.Config.from_mapping(
        {
            "profile": {
                "default": {
                    "description": "Base LSF profile",
                    "connection": {"host": "login.example.com", "user": "alice"},
                    "scheduler": {"type": "LSF"},
                    "resources": {"queue": "normal"},
                    "lsf": {"resource_reserve_per_task": True},
                },
                "batch": {
                    "inherit": "default",
                    "resources": {"queue": "batch", "exclusive": True},
                },
                "gpu": {
                    "description": "GPU jobs",
                    "inherit": "batch",
                    "resources": {
                        "queue": "gpu",
                        "cores": 8,
                        "memory": "32GB",
                        "time_limit": "1:00",
                    },
                },
            },
        }
    )

    profile = config.resolve_profile("gpu")

    assert str(profile.connection.host) == "login.example.com"
    assert profile.description == "GPU jobs"
    assert profile.connection.user == "alice"
    assert profile.scheduler.type is SchedulerType.LSF
    assert profile.resources.queue == "gpu"
    assert profile.resources.cores == 8
    assert profile.resources.exclusive
    assert int(profile.resources.memory) == 32_000_000_000
    assert profile.resources.time_limit_delta == timedelta(hours=1)
    assert profile.lsf.resource_reserve_per_task


def test_profile_inheritance_merges_each_sub_config_field_by_field() -> None:
    config = config_module.Config.from_mapping(
        {
            "profile": {
                "base": {
                    "lsf": {
                        "application_profile": "qrsh",
                        "submission_environment": {"A": "1", "B": "2"},
                    }
                },
                "child": {
                    "inherit": "base",
                    "lsf": {"submission_environment": {"C": "3"}},
                },
            }
        }
    )

    lsf = config.resolve_profile("child").lsf

    assert lsf.application_profile == "qrsh"
    # Values inside a sub-config are replaced, not merged.
    assert lsf.submission_environment == {"C": "3"}
    assert lsf.model_fields_set == {"application_profile", "submission_environment"}


@pytest.mark.parametrize(
    "profile",
    [
        {"host": "login.example.com"},
        {"lsf_application_profile": "qrsh"},
        {"connection": {"queue": "gpu"}},
    ],
)
def test_unknown_profile_keys_are_rejected(profile: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        config_module.Config.from_mapping({"profile": {"base": profile}})


@pytest.mark.parametrize(
    ("profiles", "message"),
    [
        (
            {"a": {"inherit": "b"}, "b": {"inherit": "a"}},
            "inheritance cycle",
        ),
        ({"a": {"inherit": "missing"}}, "inherits unknown profile"),
        (
            {"not allowed": {"connection": {"host": "login.example.com"}}},
            "profile names",
        ),
    ],
)
def test_invalid_profile_graphs_are_rejected(
    profiles: dict[str, dict[str, object]], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        config_module.Config.from_mapping({"profile": profiles})


@pytest.mark.parametrize(
    "connection",
    [
        {"host": None, "user": "alice"},
        {"host": "login.example.com"},
        {"host": "login.example.com", "user": ""},
    ],
)
def test_resolved_config_requires_host_and_user(connection: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ResolvedConfig.model_validate({"connection": connection})
