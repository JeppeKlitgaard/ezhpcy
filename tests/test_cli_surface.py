"""
The CLI's user-facing surface: commands, options, validation and errors.

These tests go through `invoke` and the `capture` seams only, never through the CLI
framework's objects, and never compare exact help text, so that they can stay
unchanged when the framework behind the CLI changes. See PLAN.md, "Behaviour to
preserve: CLI surface".
"""

import json
import logging
import os
import re
import sys
from collections.abc import Iterator
from datetime import timedelta
from operator import attrgetter
from pathlib import Path
from types import SimpleNamespace

import keyring
import pytest

from ezhpcy.config import config
from ezhpcy.constants import PACKAGE_NAME
from ezhpcy.logging import TERMINAL_HANDLER_NAME
from ezhpcy.scheduler.types import SchedulerType
from ezhpcy.types import ProfileConfig, SubmissionMode
from ezhpcy.utils import ezhpcy_version
from tests.support.cli import RELAYED_BYTES, capture, invoke

_PROFILES = {
    "base": {
        "connection": {"host": "login.example.com", "user": "alice"},
        "scheduler": {"type": "LSF", "submission_mode": "interactive"},
    },
    "exclusive": {"inherit": "base", "resources": {"exclusive": True}},
}

_LOGIN_COMMANDS = ["tunnel", "provision", "prune"]

# `keyring set` prompts for a missing password, which can't hide input from the
# test's piped stdin.
_PIPED_PASSWORD_PROMPT = pytest.mark.filterwarnings("ignore::getpass.GetPassWarning")


@pytest.fixture(autouse=True)
def surface_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Replace the workstation's configuration with known profiles and settings."""
    monkeypatch.setattr(
        config,
        "profile",
        {
            name: ProfileConfig.model_validate(values)
            for name, values in _PROFILES.items()
        },
    )
    monkeypatch.setattr(config, "auto_provision", True)
    monkeypatch.setattr(config, "debug", False)
    config_file = tmp_path / "ezhpcy.toml"
    monkeypatch.setattr(config.local_file, "config_file", config_file)
    return config_file


@pytest.fixture(autouse=True)
def restore_logger() -> Iterator[None]:
    """Undo the logging configuration that `--debug` applies."""
    package_logger = logging.getLogger(PACKAGE_NAME)
    previous_level = package_logger.level
    handler = _terminal_handler()
    previous_formatter = handler.formatter
    yield
    package_logger.setLevel(previous_level)
    handler.setFormatter(previous_formatter)


def _terminal_handler() -> logging.Handler:
    return next(
        handler
        for handler in logging.getLogger(PACKAGE_NAME).handlers
        if handler.get_name() == TERMINAL_HANDLER_NAME
    )


def _password_fd(password: str) -> int:
    """Return a readable descriptor holding `password`; the test closes it."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, password.encode())
    os.close(write_fd)
    return read_fd


# Every command runs


@pytest.mark.parametrize("name", ["tunnel", "t"])
def test_tunnel_and_its_alias_start_a_tunnel_for_a_profile(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    captured = capture(monkeypatch, "tunnel")

    result = invoke([name, "base"])

    assert result.exit_code == 0, result
    assert captured["resolved"].connection.user == "alice"
    assert captured["ssh_host"].alias == "base"


def test_provision_with_yes_starts_provisioning_without_asking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture(monkeypatch, "provision")

    result = invoke(["provision", "base", "--yes"])

    assert captured["connection"].user == "alice", result
    assert "remote_operation" in captured
    assert "Proceed?" not in result.stdout


def test_provision_asks_first_and_can_be_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture(monkeypatch, "provision")

    result = invoke(["provision", "base"], input="n\n")

    assert result.exit_code == 1, result
    assert "Proceed?" in result.stdout
    assert "provisioning cancelled" in result.stdout
    assert "remote_operation" not in captured


def _removes_cache_root(captured: dict[str, object]) -> bool:
    operation = str(captured.get("remote_operation"))
    return "rm" in operation and "'/home/alice/.cache/ezhpcy'" in operation


def test_prune_without_all_prunes_stale_data_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture(monkeypatch, "prune")

    result = invoke(["prune", "base"])

    assert "remote_operation" in captured, result
    assert not _removes_cache_root(captured)


@pytest.mark.parametrize("arguments", [["--all", "--yes"], ["-a", "-y"]])
def test_prune_all_with_yes_removes_every_managed_file_without_asking(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    captured = capture(monkeypatch, "prune")

    result = invoke(["prune", "base", *arguments])

    assert _removes_cache_root(captured), result
    assert "Proceed?" not in result.stdout


def test_prune_all_asks_first_and_defaults_to_no(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture(monkeypatch, "prune")

    result = invoke(["prune", "base", "--all"], input="\n")

    assert result.exit_code == 1, result
    assert "Proceed?" in result.stdout
    assert "prune cancelled" in result.stdout
    assert not _removes_cache_root(captured)


def test_proxy_relays_for_the_given_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = capture(monkeypatch, "proxy")

    result = invoke(["proxy", "gpu"])

    assert result.exit_code == 0, result
    assert captured["alias"] == "gpu"
    assert result.stdout == RELAYED_BYTES.decode()


@_PIPED_PASSWORD_PROMPT
def test_keyring_set_stores_the_password_for_user_at_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture(monkeypatch, "keyring")

    result = invoke(
        ["keyring", "set", "--host", "login.example.com", "--user", "alice"],
        input="typed\n",
    )

    assert result.exit_code == 0, result
    assert captured == {
        "service": "ezhpcy",
        "account": "alice@login.example.com",
        "password": "typed",
    }


def test_config_edit_opens_the_configuration_file_in_the_given_editor(
    surface_config: Path,
) -> None:
    editor = f'"{sys.executable}" -c pass'

    result = invoke(["config", "edit", editor])

    assert result.exit_code == 0, result
    assert surface_config.is_file()


def test_config_load_writes_a_preset(surface_config: Path) -> None:
    result = invoke(["config", "load", "dtu", "--user", "alice"])

    assert result.exit_code == 0, result
    assert 'user = "alice"' in surface_config.read_text(encoding="utf-8")


def test_doctor_runs(surface_config: Path) -> None:
    surface_config.write_text("", encoding="utf-8")

    result = invoke(["doctor"])

    # The outcome depends on the workstation's OpenSSH setup; only that it ran
    # and checked the profiles is part of the surface.
    assert result.exception is None, result
    assert result.exit_code in (0, 1)
    assert "Profile base is valid" in result.stdout


@pytest.mark.parametrize("arguments", [[], ["--json"]])
def test_info_runs(arguments: list[str]) -> None:
    result = invoke(["info", *arguments])

    assert result.exit_code == 0, result
    assert ezhpcy_version() in result.stdout
    if arguments:
        json.loads(result.stdout)


def test_version_prints_the_version() -> None:
    result = invoke(["version"])

    assert result.exit_code == 0, result
    assert result.stdout.strip() == ezhpcy_version()


def test_version_json_prints_the_version_as_json() -> None:
    result = invoke(["version", "--json"])

    assert result.exit_code == 0, result
    assert json.loads(result.stdout) == {"version": ezhpcy_version()}


def test_list_profiles_lists_the_configured_profiles() -> None:
    result = invoke(["list-profiles"])

    assert result.exit_code == 0, result
    assert "base" in result.stdout
    assert "exclusive" in result.stdout


# Options reach the command


_PASSWORD_FILE = "{password_file}"
_PASSWORD_FD = "{password_fd}"

# (arguments, environment, ConnectionInfo field, expected value), applied on top
# of a configuration that sets user alice at login.example.com.
_CONNECTION_OPTION_CASES = [
    (["--host", "other.example.com"], {}, "host", "other.example.com"),
    (["-h", "other.example.com"], {}, "host", "other.example.com"),
    ([], {"EZHPCY_HOST": "other.example.com"}, "host", "other.example.com"),
    (["--user", "bob"], {}, "user", "bob"),
    (["-u", "bob"], {}, "user", "bob"),
    ([], {"EZHPCY_USER": "bob"}, "user", "bob"),
    (["--password", "from-option"], {}, "password", "from-option"),
    ([], {"EZHPCY_PASSWORD": "from-option"}, "password", "from-option"),
    (["--password-file", _PASSWORD_FILE], {}, "password", "from-file"),
    ([], {"EZHPCY_PASSWORD_FILE": _PASSWORD_FILE}, "password", "from-file"),
    (["--password-fd", _PASSWORD_FD], {}, "password", "from-fd"),
    ([], {"EZHPCY_PASSWORD_FD": _PASSWORD_FD}, "password", "from-fd"),
    (["--password-keyring"], {}, "password", "from-keyring"),
    ([], {"EZHPCY_PASSWORD_KEYRING": "1"}, "password", "from-keyring"),
]


@pytest.fixture
def password_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[dict[str, str]]:
    """Real password sources for `_CONNECTION_OPTION_CASES` placeholders."""
    password_file = tmp_path / "password"
    password_file.write_text("from-file\n", encoding="utf-8")
    password_fd = _password_fd("from-fd\n")
    monkeypatch.setattr(keyring, "get_password", lambda *_args: "from-keyring")
    try:
        yield {_PASSWORD_FILE: str(password_file), _PASSWORD_FD: str(password_fd)}
    finally:
        os.close(password_fd)


def _filled(values: list[str], sources: dict[str, str]) -> list[str]:
    return [sources.get(value, value) for value in values]


@pytest.mark.parametrize(
    ("arguments", "environment", "field", "expected"), _CONNECTION_OPTION_CASES
)
@pytest.mark.parametrize("command", _LOGIN_COMMANDS)
def test_connection_options_reach_the_login_connection(
    monkeypatch: pytest.MonkeyPatch,
    password_sources: dict[str, str],
    command: str,
    arguments: list[str],
    environment: dict[str, str],
    field: str,
    expected: str,
) -> None:
    captured = capture(monkeypatch, command)
    env = dict(zip(environment, _filled(list(environment.values()), password_sources)))

    result = invoke([command, "base", *_filled(arguments, password_sources)], env=env)

    connection = (
        captured["resolved"].connection
        if command == "tunnel"
        else captured.get("connection")
    )
    assert connection is not None, result
    assert getattr(connection, field) == expected


@_PIPED_PASSWORD_PROMPT
@pytest.mark.parametrize(
    ("arguments", "environment", "field", "expected"), _CONNECTION_OPTION_CASES
)
def test_connection_options_reach_keyring_set(
    monkeypatch: pytest.MonkeyPatch,
    password_sources: dict[str, str],
    arguments: list[str],
    environment: dict[str, str],
    field: str,
    expected: str,
) -> None:
    captured = capture(monkeypatch, "keyring")
    env = {
        "EZHPCY_HOST": "login.example.com",
        "EZHPCY_USER": "alice",
        **dict(zip(environment, _filled(list(environment.values()), password_sources))),
    }

    result = invoke(
        ["keyring", "set", *_filled(arguments, password_sources)],
        env=env,
        input="typed\n",
    )

    assert result.exit_code == 0, result
    user, host = str(captured["account"]).split("@")
    stored = SimpleNamespace(user=user, host=host, password=captured["password"])
    assert getattr(stored, field) == expected


def test_profile_argument_can_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture(monkeypatch, "tunnel")

    result = invoke(["tunnel"], env={"EZHPCY_PROFILE": "base"})

    assert result.exit_code == 0, result
    assert captured["resolved"].connection.user == "alice"
    assert captured["ssh_host"].alias == "base"


# (arguments, attribute of the resolved configuration, expected value)
_TUNNEL_OPTION_CASES = [
    (["--scheduler", "PBS"], "scheduler.type", SchedulerType.PBS),
    (["--scheduler", "pbs"], "scheduler.type", SchedulerType.PBS),
    (["--submission-mode", "batch"], "scheduler.submission_mode", SubmissionMode.BATCH),
    (["--submission-mode", "BATCH"], "scheduler.submission_mode", SubmissionMode.BATCH),
    (
        ["--interactive-submission-command", "a100sh --gpu 'two words'"],
        "scheduler.interactive_submission_command",
        ["a100sh", "--gpu", "two words"],
    ),
    (["--queue", "gpu"], "resources.queue", "gpu"),
    (["-q", "gpu"], "resources.queue", "gpu"),
    (["--cores", "8"], "resources.cores", 8),
    (["-n", "8"], "resources.cores", 8),
    (["--gpus", "2"], "resources.gpus", 2),
    (["--time-limit", "1:30"], "resources.time_limit_delta", timedelta(minutes=90)),
    (["--memory", "2GiB"], "resources.memory_bytes", 2 * 1024**3),
    (["--memory", "2GB"], "resources.memory_bytes", 2 * 1000**3),
    (["--queue-timeout", "120"], "timings.queue_timeout_seconds", 120),
    (["--startup-timeout", "30"], "timings.worker_startup_timeout_seconds", 30),
    (["--job-poll-interval", "0.5"], "timings.job_poll_interval_seconds", 0.5),
    (["--job-monitor-interval", "5"], "timings.job_monitor_interval_seconds", 5),
    (
        ["--worker-heartbeat-interval", "10"],
        "timings.worker_heartbeat_interval_seconds",
        10,
    ),
    (
        ["--worker-heartbeat-timeout", "40"],
        "timings.worker_heartbeat_timeout_seconds",
        40,
    ),
]


@pytest.mark.parametrize(("arguments", "attribute", "expected"), _TUNNEL_OPTION_CASES)
def test_tunnel_options_reach_the_resolved_configuration(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    attribute: str,
    expected: object,
) -> None:
    captured = capture(monkeypatch, "tunnel")

    result = invoke(["tunnel", "base", *arguments])

    assert result.exit_code == 0, result
    assert attrgetter(attribute)(captured["resolved"]) == expected


def test_tunnel_alias_option_names_the_ssh_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture(monkeypatch, "tunnel")

    result = invoke(["tunnel", "base", "--alias", "my-gpu"])

    assert result.exit_code == 0, result
    assert captured["ssh_host"].alias == "my-gpu"


def test_tunnel_worker_port_is_the_only_port_tried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture(monkeypatch, "tunnel")

    result = invoke(["tunnel", "base", "--worker-port", "50000"])

    assert result.exit_code == 0, result
    assert captured["worker_ports"] == (50000,)


def test_tunnel_worker_port_retries_adds_alternate_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture(monkeypatch, "tunnel")

    result = invoke(["tunnel", "base", "--worker-port-retries", "2"])

    assert result.exit_code == 0, result
    assert len(set(captured["worker_ports"])) == 3


@pytest.mark.parametrize(
    ("configured", "arguments"),
    [(True, []), (False, ["--auto-provision"])],
)
def test_tunnel_auto_provisions_when_configured_or_asked(
    monkeypatch: pytest.MonkeyPatch, configured: bool, arguments: list[str]
) -> None:
    monkeypatch.setattr(config, "auto_provision", configured)
    captured = capture(monkeypatch, "tunnel")

    result = invoke(["tunnel", "base", *arguments])

    assert result.exit_code == 0, result
    assert captured["auto_provision"] is True


@pytest.mark.parametrize(
    ("configured", "arguments"),
    [(False, []), (True, ["--no-auto-provision"])],
)
def test_tunnel_without_auto_provision_needs_existing_worker_credentials(
    monkeypatch: pytest.MonkeyPatch, configured: bool, arguments: list[str]
) -> None:
    monkeypatch.setattr(config, "auto_provision", configured)
    captured = capture(monkeypatch, "tunnel")

    result = invoke(["tunnel", "base", *arguments])

    assert result.exit_code == 1, result
    assert "run `ezhpcy provision` first" in result.stderr
    assert captured == {}


def test_auto_provision_flags_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = capture(monkeypatch, "tunnel")

    result = invoke(["tunnel", "base", "--auto-provision", "--no-auto-provision"])

    assert result.exit_code == 2, result
    assert "cannot be used together" in result.stderr
    assert captured == {}


# --exclusive / --shared


@pytest.mark.parametrize(
    ("profile", "arguments", "expected"),
    [
        ("base", [], False),
        ("base", ["--exclusive"], True),
        ("exclusive", [], True),
        ("exclusive", ["--shared"], False),
        ("exclusive", ["--exclusive"], True),
    ],
)
def test_exclusive_and_shared_override_the_profile_only_when_given(
    monkeypatch: pytest.MonkeyPatch, profile: str, arguments: list[str], expected: bool
) -> None:
    captured = capture(monkeypatch, "tunnel")

    result = invoke(["tunnel", profile, *arguments])

    assert result.exit_code == 0, result
    assert captured["resolved"].resources.exclusive is expected


@pytest.mark.parametrize(
    ("arguments", "conflicts"),
    [([], False), (["--exclusive"], True), (["--shared"], True)],
)
def test_exclusive_and_shared_count_as_given_submission_options(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str], conflicts: bool
) -> None:
    """Neither flag leaves the option unset, so it can't conflict with a wrapper."""
    captured = capture(monkeypatch, "tunnel")

    result = invoke(
        ["tunnel", "base", "--interactive-submission-command", "a100sh", *arguments]
    )

    if conflicts:
        assert result.exit_code == 2, result
        assert "--exclusive/--shared" in result.stderr
        assert captured == {}
    else:
        assert result.exit_code == 0, result


# --debug


@pytest.mark.parametrize(
    ("arguments", "env"),
    [
        (["--debug", "version"], {}),
        (["version", "--debug"], {}),
        (["version"], {"EZHPCY_DEBUG": "1"}),
    ],
)
def test_debug_enables_debug_logging_with_timestamps(
    arguments: list[str], env: dict[str, str]
) -> None:
    result = invoke(arguments, env=env)

    assert result.exit_code == 0, result
    assert logging.getLogger(PACKAGE_NAME).level == logging.DEBUG
    record = logging.LogRecord(PACKAGE_NAME, logging.DEBUG, "", 0, "message", (), None)
    rendered = _terminal_handler().format(record)
    assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ", rendered), rendered


# Validation limits


@pytest.mark.parametrize(
    ("arguments", "option"),
    [
        (["--cores", "0"], "--cores"),
        (["--gpus", "-1"], "--gpus"),
        (["--queue-timeout", "0.5"], "--queue-timeout"),
        (["--startup-timeout", "0.5"], "--startup-timeout"),
        (["--job-poll-interval", "0.05"], "--job-poll-interval"),
        (["--job-monitor-interval", "0.05"], "--job-monitor-interval"),
        (["--worker-heartbeat-interval", "0.5"], "--worker-heartbeat-interval"),
        (["--worker-heartbeat-timeout", "0.5"], "--worker-heartbeat-timeout"),
        (["--worker-port", "1023"], "--worker-port"),
        (["--worker-port", "65536"], "--worker-port"),
        (["--worker-port-retries", "-1"], "--worker-port-retries"),
        (["--password-fd", "-1"], "--password-fd"),
        (["--password-file", "{missing}"], "--password-file"),
        (["--password-file", "{directory}"], "--password-file"),
        (["--scheduler", "SLURM"], "--scheduler"),
        (["--submission-mode", "sometimes"], "--submission-mode"),
        (["--cores", "many"], "--cores"),
    ],
)
def test_values_outside_the_limits_are_usage_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    arguments: list[str],
    option: str,
) -> None:
    captured = capture(monkeypatch, "tunnel")
    paths = {"{missing}": str(tmp_path / "missing"), "{directory}": str(tmp_path)}

    result = invoke(["tunnel", "base", *_filled(arguments, paths)])

    assert result.exit_code == 2, result
    assert option in result.stderr
    assert captured == {}


@pytest.mark.parametrize(
    "arguments",
    [
        ["--cores", "1"],
        ["--gpus", "0"],
        ["--queue-timeout", "1"],
        ["--startup-timeout", "1"],
        ["--job-poll-interval", "0.1"],
        ["--job-monitor-interval", "0.1"],
        ["--worker-heartbeat-interval", "1"],
        ["--worker-heartbeat-timeout", "1"],
        ["--worker-port", "1024"],
        ["--worker-port", "65535"],
        ["--worker-port-retries", "0"],
    ],
)
def test_values_at_the_limits_are_accepted(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    captured = capture(monkeypatch, "tunnel")

    result = invoke(["tunnel", "base", *arguments])

    assert result.exit_code == 0, result
    assert "resolved" in captured


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--time-limit", "soon"], "time_limit"),
        (["--memory", "lots"], "memory"),
        (["--memory", "0"], "memory"),
    ],
)
def test_invalid_resource_values_are_usage_errors(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str], message: str
) -> None:
    captured = capture(monkeypatch, "tunnel")

    result = invoke(["tunnel", "base", *arguments])

    assert result.exit_code == 2, result
    assert message in result.stderr
    assert captured == {}


# Errors from command bodies


@pytest.mark.parametrize(
    ("command", "arguments", "messages"),
    [
        pytest.param(
            "tunnel",
            ["--host", "login.example.com", "--scheduler", "LSF"],
            ["user must be set", "--user", "ezhpcy list-profiles"],
            id="missing-user",
        ),
        pytest.param(
            "provision",
            ["--user", "alice"],
            ["host must be set", "--host", "ezhpcy list-profiles"],
            id="missing-host",
        ),
        pytest.param(
            "keyring",
            ["set", "--host", "login.example.com"],
            ["user must be set", "--user"],
            id="keyring-missing-user",
        ),
        pytest.param(
            "tunnel",
            ["base", "--password", "secret", "--password-keyring"],
            ["mutually exclusive", "--password", "--password-keyring"],
            id="conflicting-password-sources",
        ),
        pytest.param(
            "tunnel",
            ["--host", "login.example.com", "--user", "alice"],
            ["scheduler.type must be set", "--scheduler"],
            id="missing-scheduler",
        ),
        pytest.param(
            "tunnel",
            [
                "base",
                "--submission-mode",
                "batch",
                "--interactive-submission-command",
                "a100sh",
            ],
            ["interactive_submission_command requires", "interactive"],
            id="wrapper-in-batch-mode",
        ),
        pytest.param(
            "tunnel",
            ["base", "--interactive-submission-command", "a100sh", "--cores", "2"],
            ["cannot be combined with submission options", "--cores"],
            id="wrapper-with-resources",
        ),
        pytest.param(
            "tunnel",
            ["base", "--interactive-submission-command", "'unterminated"],
            ["--interactive-submission-command", "quoting"],
            id="wrapper-quoting",
        ),
        pytest.param(
            "tunnel",
            ["base", "--alias", "exclusive"],
            ["is the name of profile", "exclusive"],
            id="alias-is-a-profile",
        ),
        pytest.param(
            "tunnel",
            ["no-such-profile"],
            [
                'invalid value "no-such-profile" for profile',
                'choose from: "base", "exclusive"',
            ],
            id="unknown-profile",
        ),
        pytest.param(
            "config",
            ["load", "unknown"],
            ["unknown", "dtu"],
            id="unknown-preset",
        ),
    ],
)
def test_command_errors_are_usage_errors_with_their_key_message(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    arguments: list[str],
    messages: list[str],
) -> None:
    captured = (
        capture(monkeypatch, command)
        if command in (*_LOGIN_COMMANDS, "keyring")
        else {}
    )

    result = invoke([command, *arguments], input="typed\n")

    assert result.exit_code == 2, result
    stderr = result.stderr.casefold()
    for message in messages:
        assert message.casefold() in stderr
    assert captured == {}


def test_unknown_profile_without_any_profiles_points_to_a_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "profile", {})
    captured = capture(monkeypatch, "tunnel")

    result = invoke(["tunnel", "missing"])

    assert result.exit_code == 2, result
    assert "No profiles are configured" in result.stderr
    assert "ezhpcy config load" in result.stderr
    assert captured == {}


# Help


def _listed_names(help_text: str, names: list[str]) -> list[str]:
    """Return `names` in the order help lists them, one per line at most."""
    pattern = re.compile(r"^\W*(" + "|".join(map(re.escape, names)) + r")\b")
    listed = []
    for line in help_text.splitlines():
        if (match := pattern.match(line)) and match.group(1) not in listed:
            listed.append(match.group(1))
    return listed


def test_no_arguments_shows_help_with_the_commands_in_order() -> None:
    expected = [
        "provision",
        "prune",
        "tunnel",
        "proxy",
        "Configuration Commands",
        "config",
        "doctor",
        "keyring",
        "Meta Commands",
        "info",
        "version",
        "list-profiles",
    ]

    result = invoke([])

    assert result.exception is None, result
    assert _listed_names(result.stdout, expected) == expected


@pytest.mark.parametrize("group", [[], ["config"], ["keyring"]])
def test_help_is_listed_with_the_options_not_the_commands(group: list[str]) -> None:
    first_command = {"config": "edit", "keyring": "set"}.get(next(iter(group), ""))

    result = invoke([*group, "--help"])

    assert result.exit_code == 0, result
    names = ["--debug", "--help", first_command or "provision"]
    assert _listed_names(result.stdout, names) == names


@pytest.mark.parametrize("group", [[], ["config"], ["keyring"], ["version"]])
@pytest.mark.parametrize("flag", ["-h", "--version"])
def test_there_is_no_short_help_flag_or_version_flag(
    group: list[str], flag: str
) -> None:
    """`-h` is short for `--host`, so it is never help, not even where there's no host."""
    result = invoke([*group, flag])

    assert result.exit_code == 2, result


def _help_panels(help_text: str, titles: list[str]) -> dict[str, str]:
    """Split help output into the text under each panel title."""
    starts = sorted((help_text.index(title), title) for title in titles)
    ends = [start for start, _ in starts[1:]] + [len(help_text)]
    return {title: help_text[start:end] for (start, title), end in zip(starts, ends)}


_CONNECTION_OPTIONS = [
    "--host",
    "--user",
    "--password ",
    "--password-file",
    "--password-fd",
    "--password-keyring",
]


@pytest.mark.parametrize("command", _LOGIN_COMMANDS)
def test_connection_options_are_grouped_in_help(command: str) -> None:
    result = invoke([command, "--help"])

    assert result.exit_code == 0, result
    panel = _help_panels(result.stdout, ["Connection Options"])["Connection Options"]
    for option in _CONNECTION_OPTIONS:
        assert option in panel


def test_tunnel_options_are_grouped_in_help() -> None:
    groups = {
        "Connection Options": _CONNECTION_OPTIONS,
        "Scheduler Options": [
            "--scheduler",
            "--submission-mode",
            "--interactive-submission-command",
        ],
        "Resource Options": [
            "--queue",
            "--cores",
            "--gpus",
            "--exclusive",
            "--shared",
            "--time-limit",
            "--memory",
        ],
        "Timing Options": [
            "--queue-timeout",
            "--startup-timeout",
            "--job-poll-interval",
            "--job-monitor-interval",
            "--worker-heartbeat-interval",
            "--worker-heartbeat-timeout",
        ],
    }

    result = invoke(["tunnel", "--help"])

    assert result.exit_code == 0, result
    panels = _help_panels(result.stdout, list(groups))
    for title, options in groups.items():
        for option in options:
            assert option in panels[title], (title, option)


# proxy stdout carries only relayed bytes


def test_proxy_logs_to_stderr_when_the_tunnel_has_debug_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture(monkeypatch, "proxy", tunnel_debug=True)

    result = invoke(["proxy", "gpu"])

    assert result.exit_code == 0, result
    assert result.stdout == RELAYED_BYTES.decode()
    assert "Relaying." in result.stderr


@pytest.mark.parametrize(
    ("arguments", "exit_code"),
    [
        (["proxy", "gpu"], 1),
        (["--debug", "proxy", "gpu"], 1),
        (["proxy", "../escape"], 2),
    ],
)
def test_proxy_errors_write_nothing_to_stdout(
    arguments: list[str], exit_code: int
) -> None:
    result = invoke(arguments)

    assert result.exit_code == exit_code, result
    assert result.stdout == ""
    assert "ezhpcy proxy:" in result.stderr
