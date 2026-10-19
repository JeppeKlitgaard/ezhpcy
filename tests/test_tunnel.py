"""The `tunnel` command's options and checks, before the session starts."""

import re
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from ezhpcy import types as types_module
from ezhpcy.cli import tunnel as tunnel_module
from ezhpcy.messages import plain
from ezhpcy.scheduler.types import SchedulerType
from ezhpcy.tunnel.ssh_config import WorkerHost
from ezhpcy.types import ConnectionInfo, ProfileConfig, ResolvedConfig, ResourcesConfig
from tests.support.cli import capture, invoke
from tests.support.tunnel import tunnel_log


@pytest.mark.parametrize(
    ("value", "expected_bytes"),
    [
        (None, None),
        ("2048", 2048),
        ("2048MB", 2_048_000_000),
        ("256GB", 256_000_000_000),
        ("1.5 GiB", 1_610_612_736),
        ("1TB", 1_000_000_000_000),
        ("1B", 1),
    ],
)
def test_parse_memory_normalizes_sizes_to_bytes(
    value: str | None, expected_bytes: int | None
) -> None:
    memory = ResourcesConfig(memory=value).memory
    assert (int(memory) if memory is not None else None) == expected_bytes


@pytest.mark.parametrize("value", ["0", "-1GB", "GB", "12QQ", "lots"])
def test_parse_memory_rejects_invalid_sizes(value: str) -> None:
    with pytest.raises(ValidationError, match="memory"):
        ResourcesConfig(memory=value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("1:00", timedelta(hours=1)),
        ("0:05", timedelta(minutes=5)),
        ("1:5", timedelta(hours=1, minutes=5)),
        (" 01:30 ", timedelta(hours=1, minutes=30)),
        ("100:00", timedelta(hours=100)),
    ],
)
def test_parse_time_limit(value: str | None, expected: timedelta | None) -> None:
    assert ResourcesConfig(time_limit=value).time_limit == expected


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("soon", "time_limit must use H:MM format"),
        ("1:000", "time_limit must use H:MM format"),
        ("90", "time_limit must use H:MM format"),
        ("1:60", "time_limit minutes must be less than 60"),
        ("0:00", "time_limit must be positive"),
        (timedelta(0), "time_limit must be positive"),
        (timedelta(seconds=90), "time_limit must be a whole number of minutes"),
        # Not seconds, as a bare `timedelta` field would read it.
        (60, "Input should be a valid timedelta"),
    ],
)
def test_parse_time_limit_rejects_invalid_limits(value: object, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ResourcesConfig.model_validate({"time_limit": value})


@pytest.mark.parametrize(
    ("value", "dumped"),
    [
        # As written, so their anonymous aliases stay the same.
        ("1:00", "1:00"),
        ("0:30", "0:30"),
        ("100:05", "100:05"),
        # Written differently; each now shares the alias of the form above.
        ("01:00", "1:00"),
        ("1:5", "1:05"),
        (" 1:00 ", "1:00"),
    ],
)
def test_time_limit_is_dumped_as_h_mm(value: str, dumped: str) -> None:
    resources = ResourcesConfig(time_limit=value)

    assert resources.model_dump(mode="json")["time_limit"] == dumped
    # Profile inheritance and the CLI options re-validate Python dumps.
    assert ResourcesConfig.model_validate(resources.model_dump()) == resources


def test_tunnel_command_accepts_anonymous_cli_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture(monkeypatch, "tunnel")

    result = invoke(_ANONYMOUS_TUNNEL_ARGS)

    assert result.exit_code == 0, result
    assert captured["profile_name"] is None
    resolved = captured["resolved"]
    assert resolved.scheduler.type is SchedulerType.LSF
    assert resolved.resources.cores == 1
    assert resolved.timings.job_poll_interval_seconds == 2.5
    assert resolved.timings.job_monitor_interval_seconds == 60
    assert resolved.connection == ConnectionInfo(host="login.example.com", user="alice")


def test_tunnel_command_resolves_profile_and_applies_cli_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tunnel_module.config,
        "profile",
        {
            "base": ProfileConfig.model_validate(
                {
                    "connection": {
                        "host": "login.example.com",
                        "user": "alice",
                        "ssh_keepalive_interval_seconds": 75,
                    },
                    "scheduler": {"type": "LSF", "submission_mode": "interactive"},
                    "resources": {
                        "queue": "normal",
                        "cores": 4,
                        "gpus": 1,
                        "exclusive": True,
                        "time_limit": "1:00",
                        "memory": "32GB",
                    },
                    "timings": {
                        "worker_heartbeat_interval_seconds": 20,
                        "worker_heartbeat_timeout_seconds": 60,
                        "job_poll_interval_seconds": 4,
                        "job_monitor_interval_seconds": 120,
                    },
                }
            ),
            "gpu": ProfileConfig.model_validate(
                {"inherit": "base", "resources": {"queue": "gpu", "cores": 8}}
            ),
        },
    )
    captured = capture(monkeypatch, "tunnel")

    result = invoke(
        [
            "tunnel",
            "gpu",
            "--cores",
            "12",
            "--memory",
            "64GB",
            "--shared",
            "--worker-heartbeat-interval",
            "12",
            "--job-poll-interval",
            "1.5",
        ],
    )

    assert result.exit_code == 0, result
    assert captured["profile_name"] == "gpu"
    resolved = captured["resolved"]
    assert resolved.scheduler.type is SchedulerType.LSF
    assert resolved.resources.queue == "gpu"
    assert resolved.resources.cores == 12
    assert resolved.resources.gpus == 1
    assert resolved.resources.exclusive is False
    assert resolved.resources.memory_bytes == 64_000_000_000
    assert resolved.resources.time_limit == timedelta(hours=1)
    assert resolved.connection == ConnectionInfo(
        host="login.example.com",
        user="alice",
        ssh_keepalive_interval_seconds=75,
    )
    assert captured["auto_provision"] is True
    assert resolved.timings.worker_heartbeat_interval_seconds == 12
    assert resolved.timings.worker_heartbeat_timeout_seconds == 60
    assert resolved.timings.job_poll_interval_seconds == 1.5
    assert resolved.timings.job_monitor_interval_seconds == 120
    worker_ports = captured["worker_ports"]
    assert isinstance(worker_ports, tuple)
    assert len(set(worker_ports)) == 6


def test_tunnel_command_limits_worker_port_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """More retries than dynamic ports would make port selection loop forever."""
    captured = capture(monkeypatch, "tunnel")

    result = invoke([*_ANONYMOUS_TUNNEL_ARGS, "--worker-port-retries", "101"])

    assert result.exit_code == 2
    assert "--worker-port-retries" in result.stderr
    assert "Must be <= 100" in result.stderr
    assert captured == {}


def test_tunnel_command_allows_wrapper_with_implicit_resource_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tunnel_module.config,
        "profile",
        {
            "base": ProfileConfig.model_validate(
                {
                    "connection": {"host": "login.example.com", "user": "alice"},
                    "scheduler": {
                        "type": "LSF",
                        "submission_mode": "interactive",
                        "interactive_submission_command": ["/site/bin/interactive-lsf"],
                    },
                }
            )
        },
    )
    captured = capture(monkeypatch, "tunnel")

    result = invoke(["tunnel", "base"])

    assert result.exit_code == 0, result
    resolved = captured["resolved"]
    assert resolved.resources.queue is None
    assert resolved.resources.cores == 1
    assert resolved.resources.gpus == 0
    assert resolved.resources.exclusive is False
    assert resolved.scheduler.interactive_submission_command == [
        "/site/bin/interactive-lsf"
    ]


def test_tunnel_command_logs_inherited_submission_options_ignored_by_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tunnel_module.config,
        "profile",
        {
            "base": ProfileConfig.model_validate(
                {
                    "connection": {"host": "login.example.com", "user": "alice"},
                    "scheduler": {"type": "LSF", "submission_mode": "interactive"},
                    "resources": {"queue": "normal"},
                    "lsf": {"application_profile": "qrsh"},
                }
            ),
            "wrapper": ProfileConfig.model_validate(
                {
                    "inherit": "base",
                    "scheduler": {
                        "interactive_submission_command": ["/site/bin/interactive-lsf"]
                    },
                }
            ),
        },
    )
    capture(monkeypatch, "tunnel")

    with patch("ezhpcy.cli.tunnel.logger") as logger:
        result = invoke(["tunnel", "wrapper"])

    assert result.exit_code == 0, result
    [[message], _] = logger.info.call_args
    assert plain(message) == (
        "Ignoring inherited scheduler submission options for "
        "scheduler.interactive_submission_command: "
        "lsf.application_profile, resources.queue"
    )
    logger.info.assert_called_once()


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("resources", "queue", "normal"),
        ("resources", "cores", 1),
        ("resources", "gpus", 0),
        ("resources", "exclusive", False),
        ("resources", "time_limit", "1:00"),
        ("resources", "memory", "1GB"),
        ("lsf", "resource_reserve_per_task", False),
        ("lsf", "application_profile", "qrsh"),
        ("lsf", "submission_environment", {"LSF_QRSH": "true"}),
        ("lsf", "export_environment", ["TERM"]),
    ],
)
def test_tunnel_command_rejects_configured_submission_options_with_wrapper(
    monkeypatch: pytest.MonkeyPatch,
    section: str,
    field: str,
    value: object,
) -> None:
    monkeypatch.setattr(
        tunnel_module.config,
        "profile",
        {
            "base": ProfileConfig.model_validate(
                {
                    "connection": {"host": "login.example.com", "user": "alice"},
                    "scheduler": {
                        "type": "LSF",
                        "submission_mode": "interactive",
                        "interactive_submission_command": ["/site/bin/interactive-lsf"],
                    },
                    section: {field: value},
                }
            )
        },
    )

    result = invoke(["tunnel", "base"])

    assert result.exit_code == 2
    assert "cannot be combined with submission options" in result.stderr
    assert f"profile.base.{section}.{field}" in result.stderr


def _alias_profiles() -> dict[str, ProfileConfig]:
    return {
        name: ProfileConfig.model_validate(
            {
                "connection": {"host": "login.example.com", "user": "alice"},
                "scheduler": {"type": "LSF", "submission_mode": "interactive"},
            }
        )
        for name in ("cpu", "gpu")
    }


_ANONYMOUS_TUNNEL_ARGS = [
    "tunnel",
    "--host",
    "login.example.com",
    "--user",
    "alice",
    "--scheduler",
    "LSF",
    "--submission-mode",
    "interactive",
]


def test_tunnel_without_auto_provision_needs_the_local_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture(monkeypatch, "tunnel")

    with tunnel_log() as log:
        result = invoke([*_ANONYMOUS_TUNNEL_ARGS, "--no-auto-provision"])

    assert result.exit_code == 1
    # Before connecting.
    assert captured == {}
    [error] = log.command_errors()
    assert error.startswith(
        "Could not start tunnel: worker SSH credentials are missing; run "
        "'ezhpcy provision' first (missing: "
    )


@pytest.mark.parametrize(
    ("arguments", "alias", "user"),
    [
        ([], "gpu", "alice"),
        (["--user", "bob"], "gpu", "bob"),
        (["--alias", "my-gpu"], "my-gpu", "alice"),
    ],
)
def test_profile_tunnel_alias_defaults_to_the_profile_name(
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime_dir: Path,
    arguments: list[str],
    alias: str,
    user: str,
) -> None:
    monkeypatch.setattr(tunnel_module.config, "profile", _alias_profiles())
    captured = capture(monkeypatch, "tunnel")

    result = invoke(["tunnel", "gpu", *arguments])

    assert result.exit_code == 0, result
    ssh_host = captured["ssh_host"]
    assert isinstance(ssh_host, WorkerHost)
    assert (ssh_host.alias, ssh_host.user) == (alias, user)
    # Idle profiles get no Host block.
    assert not (isolated_runtime_dir / "ssh-config").exists()


def test_anonymous_tunnel_alias_is_stable_for_identical_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture(monkeypatch, "tunnel")

    def alias_for(*extra: str) -> str:
        result = invoke([*_ANONYMOUS_TUNNEL_ARGS, *extra])
        assert result.exit_code == 0, result
        ssh_host = captured["ssh_host"]
        assert isinstance(ssh_host, WorkerHost)
        return ssh_host.alias

    first = alias_for("--queue", "gpu")

    assert re.fullmatch(r"ezhpcy-[0-9a-f]{12}", first)
    assert alias_for("--queue", "gpu") == first
    assert alias_for("--queue", "cpu") != first


SCHEDULER = {"type": "LSF", "submission_mode": "batch"}


def test_anonymous_alias_digest_is_order_independent() -> None:
    first = ResolvedConfig.model_validate(
        {
            "connection": {"user": "alice", "host": "login.example.com"},
            "scheduler": SCHEDULER,
            "lsf": {"submission_environment": {"A": "first", "Z": "last"}},
        }
    )
    second = ResolvedConfig.model_validate(
        {
            "connection": {"host": "login.example.com", "user": "alice"},
            "scheduler": SCHEDULER,
            "lsf": {"submission_environment": {"Z": "last", "A": "first"}},
        }
    )

    assert first.alias_digest() == second.alias_digest()


def test_anonymous_alias_digest_excludes_password_sources() -> None:
    without_password = ResolvedConfig.model_validate(
        {
            "connection": {"user": "alice", "host": "login.example.com"},
            "scheduler": SCHEDULER,
        }
    )
    password = ResolvedConfig.model_validate(
        {
            "connection": {
                "user": "alice",
                "host": "login.example.com",
                "password": "secret",
                "password_prompt": False,
            },
            "scheduler": SCHEDULER,
        }
    )

    assert password.alias_digest() == without_password.alias_digest()


def test_anonymous_alias_digest_excludes_timings() -> None:
    connection = {"user": "alice", "host": "login.example.com"}
    default = ResolvedConfig.model_validate(
        {"connection": connection, "scheduler": SCHEDULER}
    )
    changed = ResolvedConfig.model_validate(
        {
            "connection": {**connection, "ssh_keepalive_interval_seconds": 75},
            "scheduler": SCHEDULER,
            "timings": {"queue_timeout_seconds": 60},
        }
    )

    assert default.alias_digest() == changed.alias_digest()


def test_anonymous_alias_digest_is_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A change to the digest renames every anonymous tunnel, so make it on purpose.

    Pinned from before `time_limit` became a `timedelta`.
    """
    monkeypatch.setattr(types_module, "local_machine_id", lambda: "test-machine")
    resolved = ResolvedConfig.model_validate(
        {
            "connection": {"user": "alice", "host": "login.example.com"},
            "scheduler": SCHEDULER,
            "resources": {
                "queue": "gpu",
                "cores": 8,
                "gpus": 1,
                "time_limit": "4:30",
                "memory": "32GB",
            },
        }
    )

    assert resolved.alias_digest() == (
        "9a8261e14d47a7b754e2d99f08e637a816f77cbc8a9e177c10804875f6f6e18a"
    )


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("connection", "user", "bob"),
        ("connection", "host", "other.example.com"),
        ("resources", "queue", "cpu"),
        ("resources", "cores", 16),
        ("scheduler", "type", "PBS"),
        ("scheduler", "submission_mode", "interactive"),
        ("scheduler", "interactive_submission_command", ["/site/bin/interactive"]),
        ("pbs", "command_directory", "/opt/pbspro/bin"),
    ],
)
def test_anonymous_alias_digest_includes_job_configuration(
    section: str, field: str, value: object
) -> None:
    base_values: dict[str, dict[str, object]] = {
        "connection": {"user": "alice", "host": "login.example.com"},
        "scheduler": SCHEDULER,
        "resources": {"queue": "gpu", "cores": 8},
    }
    changed_values = {
        **base_values,
        section: {**base_values.get(section, {}), field: value},
    }

    assert (
        ResolvedConfig.model_validate(base_values).alias_digest()
        != ResolvedConfig.model_validate(changed_values).alias_digest()
    )


def test_anonymous_tunnel_accepts_an_explicit_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture(monkeypatch, "tunnel")

    result = invoke([*_ANONYMOUS_TUNNEL_ARGS, "--alias", "scratch"])

    assert result.exit_code == 0, result
    ssh_host = captured["ssh_host"]
    assert isinstance(ssh_host, WorkerHost)
    assert ssh_host.alias == "scratch"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["tunnel", "gpu", "--alias", "cpu"], "is the name of profile"),
        ([*_ANONYMOUS_TUNNEL_ARGS, "--alias", "gpu"], "is the name of profile"),
        (["tunnel", "gpu", "--alias", "two words"], "may contain only"),
        (["tunnel", "gpu", "--alias", "wild*"], "may contain only"),
    ],
)
def test_tunnel_rejects_unusable_aliases(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str], message: str
) -> None:
    monkeypatch.setattr(tunnel_module.config, "profile", _alias_profiles())
    captured = capture(monkeypatch, "tunnel")

    result = invoke(arguments)

    assert result.exit_code == 2
    assert message in result.stderr
    assert captured == {}
