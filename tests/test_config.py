import logging
import re
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from ezhpcy import config as config_module
from ezhpcy.scheduler.types import SchedulerType
from ezhpcy.types import JobConfig, Login, PasswordConfig


def test_logging_is_configured_in_its_tables() -> None:
    config = config_module.Config.from_mapping(
        {
            "logging": {
                "console": {"enabled": False, "level": "debug"},
                "file": {"enabled": False, "level": "Warning", "keep": 3},
            }
        }
    )

    assert config.logging == config_module.LoggingConfig(
        console=config_module.ConsoleLoggingConfig(enabled=False, level=logging.DEBUG),
        file=config_module.FileLoggingConfig(
            enabled=False, level=logging.WARNING, keep=3
        ),
    )


@pytest.mark.parametrize(
    ("logging_config", "field"),
    [
        ({"console": {"level": "verbose"}}, "console.level"),
        ({"file": {"level": "verbose"}}, "file.level"),
        ({"file": {"keep": 0}}, "file.keep"),
        ({"file": {"rotate": 1}}, "file.rotate"),
        ({"level": "debug"}, "level"),
    ],
)
def test_invalid_logging_settings_are_rejected(
    logging_config: dict[str, object], field: str
) -> None:
    with pytest.raises(ValidationError, match=re.escape(f"logging.{field}")):
        config_module.Config.from_mapping({"logging": logging_config})


def test_debug_can_be_enabled_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("EZHPCY_DEBUG", "TRUE")

    config = config_module.Config()

    assert config.debug is True


def test_auto_provision_can_be_disabled_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("EZHPCY_AUTO_PROVISION", "FALSE")

    config = config_module.Config()

    assert config.auto_provision is False


@pytest.mark.parametrize("host", ["login.example.com", "10.0.0.1", "::1"])
def test_a_profiles_login_node_is_a_domain_name_or_an_ip_address(host: str) -> None:
    config = config_module.Config.from_mapping(
        {"profile": {"base": {"connection": {"host": host, "user": "alice"}}}}
    )

    assert config.profiles["base"].login.host == host


def test_a_login_node_that_is_neither_is_refused() -> None:
    with pytest.raises(ValidationError, match="must be a domain name or an IP address"):
        config_module.Config.from_mapping(
            {"profile": {"base": {"connection": {"host": "not a host"}}}}
        )


def test_profile_ssh_keepalive_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="ssh_keepalive_interval"):
        config_module.Config.from_mapping(
            {"profile": {"base": {"connection": {"ssh_keepalive_interval": 0}}}}
        )


@pytest.mark.parametrize(
    "field",
    [
        "job_poll_interval",
        "job_monitor_interval",
    ],
)
def test_profile_job_intervals_must_be_positive(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        config_module.Config.from_mapping(
            {"profile": {"base": {"timings": {field: 0}}}}
        )


@pytest.mark.parametrize(
    ("table", "key"),
    [
        ("connection", "ssh_keepalive_interval_seconds"),
        ("timings", "queue_timeout_seconds"),
        ("timings", "worker_heartbeat_timeout_seconds"),
    ],
)
def test_the_old_seconds_settings_are_unknown_keys(table: str, key: str) -> None:
    with pytest.raises(ValidationError, match=rf"{table}\.{key}\n  Extra inputs"):
        config_module.Config.from_mapping({"profile": {"base": {table: {key: 30}}}})


def test_the_keepalive_interval_reaches_the_login_as_a_duration() -> None:
    config = config_module.Config.from_mapping(
        {
            "profile": {
                "base": {
                    "connection": {
                        "host": "login.example.com",
                        "user": "alice",
                        "ssh_keepalive_interval": 7.5,
                    }
                }
            }
        }
    )

    assert config.profiles["base"].login.keepalive_interval == timedelta(seconds=7.5)


@pytest.mark.parametrize("command", [[], ["a100sh", ""], ["a100sh", "a\0b"]])
def test_profile_interactive_submission_command_arguments_must_be_usable(
    command: list[str],
) -> None:
    with pytest.raises(ValidationError, match="interactive_submission_command"):
        config_module.Config.from_mapping(
            {
                "profile": {
                    "base": {"scheduler": {"interactive_submission_command": command}}
                }
            }
        )


@pytest.mark.parametrize(
    ("profile_name", "expected"),
    [
        # Without a profile, only the top-level settings apply.
        (
            None,
            {
                "user": None,
                "password_file": Path("top.txt"),
                "password_fd": 3,
                "password_keyring": False,
                "password_prompt": False,
            },
        ),
        # `false` disables an inherited setting: TOML has no null.
        (
            "base",
            {
                "user": "alice",
                "password_file": Path("top.txt"),
                "password_fd": False,
                "password_keyring": True,
                "password_prompt": False,
            },
        ),
        (
            "child",
            {
                "user": "alice",
                "password_file": Path("child.txt"),
                "password_fd": False,
                "password_keyring": True,
                "password_prompt": True,
            },
        ),
    ],
)
def test_connection_settings_are_inherited_one_by_one_from_the_top_level_table(
    profile_name: str | None, expected: dict[str, object]
) -> None:
    config = config_module.Config.from_mapping(
        {
            "connection": {
                "password_file": "top.txt",
                "password_fd": 3,
                "password_prompt": False,
            },
            "profile": {
                "base": {
                    "connection": {
                        "user": "alice",
                        "password_fd": False,
                        "password_keyring": True,
                    }
                },
                "child": {
                    "inherit": "base",
                    "connection": {
                        "password_file": "child.txt",
                        "password_prompt": True,
                    },
                },
            },
        }
    )

    connection = config.resolve_profile(profile_name).connection

    assert connection.model_dump(include=set(expected)) == expected


@pytest.mark.parametrize(
    "connection",
    [{"password_file": True}, {"password_fd": True}, {"password_fd": -1}],
)
def test_password_file_and_fd_take_false_but_not_true(
    connection: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        config_module.Config.from_mapping({"connection": connection})


def test_password_fd_zero_is_a_descriptor_not_false() -> None:
    config = config_module.Config.from_mapping({"connection": {"password_fd": 0}})

    assert config.resolve_profile(None).connection.password_fd == 0
    assert config.resolve_profile(None).connection.password_fd is not False


@pytest.mark.parametrize(
    ("top_level", "base", "profile_name", "key"),
    [
        ({"password": "x"}, {}, None, "connection.password"),
        ({"password": "x"}, {}, "child", "connection.password"),
        # Blamed on the profile that sets it, not the one that inherits it.
        ({}, {"password": "x"}, "child", "profile.base.connection.password"),
    ],
)
def test_a_stored_password_is_rejected_where_it_is_set(
    top_level: dict[str, str],
    base: dict[str, str],
    profile_name: str | None,
    key: str,
) -> None:
    # Loading succeeds: one profile's mistake doesn't stop the others.
    config = config_module.Config.from_mapping(
        {
            "connection": top_level,
            "profile": {
                "base": {"connection": base},
                "child": {"inherit": "base"},
            },
        }
    )

    with pytest.raises(
        config_module.StoredPasswordError,
        match=rf"^{re.escape(key)} must not be set, because passwords must not be "
        r"stored",
    ):
        config.reject_stored_password(profile_name)


def test_local_file_cannot_be_set_in_the_config_file(tmp_path: Path) -> None:
    config_file = tmp_path / "ezhpcy.toml"
    config_file.write_text(
        '[local_file]\nconfig_file = "other.toml"\n', encoding="utf-8"
    )

    class FileConfig(config_module.Config):
        model_config = SettingsConfigDict(toml_file=config_file)

    with pytest.raises(ValidationError, match="local_file cannot be configured"):
        FileConfig()


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        # The CLI's PROFILE argument, not the `profile` table.
        ("EZHPCY_PROFILE", "base"),
        ("EZHPCY_PROFILE", '{"base": {"connection": {"host": "elsewhere"}}}'),
        ("EZHPCY_LOCAL_FILE", '{"config_dir": "elsewhere"}'),
        ("EZHPCY_LOGGING", '{"console": {"level": "debug"}}'),
        ("EZHPCY_CONNECTION", '{"password_keyring": true}'),
    ],
)
def test_tables_are_not_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, variable: str, value: str
) -> None:
    class FileConfig(config_module.Config):
        model_config = SettingsConfigDict(toml_file=tmp_path / "ezhpcy.toml")

    monkeypatch.setenv(variable, value)
    monkeypatch.setenv("EZHPCY_AUTO_PROVISION", "false")

    config = FileConfig()

    assert config.profile == {}
    assert config.local_file == config_module.LocalFileConfig()
    assert config.logging == config_module.LoggingConfig()
    assert config.connection == PasswordConfig()
    # Plain settings still come from the environment.
    assert config.auto_provision is False


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
    assert profile.resources.time_limit == timedelta(hours=1)
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
def test_login_requires_host_and_user(connection: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Login.model_validate(connection)


@pytest.mark.parametrize(
    "scheduler",
    [{}, {"type": "LSF"}, {"submission_mode": "batch"}],
)
def test_job_config_requires_scheduler_type_and_submission_mode(
    scheduler: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        JobConfig.model_validate({"scheduler": scheduler})
