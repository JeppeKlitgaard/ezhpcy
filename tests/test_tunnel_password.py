import os
from pathlib import Path

import pytest
from keyring.errors import KeyringError
from typer.testing import CliRunner

from ezhpcy.cli import app
from ezhpcy.cli.tunnel import common
from ezhpcy.cli.utils.bad_parameter import RichBadParameter
from ezhpcy.types import ProfileConfig


def resolve_password(
    *,
    password: str | None = None,
    password_env: bool = False,
    password_file: Path | None = None,
    password_fd: int | None = None,
    password_keyring: bool = False,
    config_password: str | None = None,
) -> str | None:
    return common.resolve_password(
        password=password,
        password_env=password_env,
        password_file=password_file,
        password_fd=password_fd,
        password_keyring=password_keyring,
        user="alice",
        host="login.example.com",
        config_password=config_password,
    )


@pytest.fixture(autouse=True)
def without_default_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(common.PASSWORD_ENV_VAR, raising=False)
    monkeypatch.setattr(common.get_config(), "default_profile", "default")
    monkeypatch.setattr(
        common.get_config(),
        "profile",
        {
            "default": ProfileConfig(
                host="login.example.com", user="alice", scheduler="LSF"
            )
        },
    )


def test_password_file_is_read_as_utf8_and_trailing_newlines_are_removed(
    tmp_path: Path,
) -> None:
    password_file = tmp_path / "password"
    password_file.write_text("påss word\r\n", encoding="utf-8")

    assert resolve_password(password_file=password_file) == "påss word"


def test_password_fd_is_read_without_closing_callers_descriptor() -> None:
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"secret\n")
        os.close(write_fd)
        write_fd = -1

        assert resolve_password(password_fd=read_fd) == "secret"
        os.fstat(read_fd)
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


def test_password_env_reads_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(common.PASSWORD_ENV_VAR, "from-environment")

    assert resolve_password(password_env=True) == "from-environment"


def test_environment_variable_is_ignored_without_password_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(common.PASSWORD_ENV_VAR, "from-environment")

    assert resolve_password(config_password="from-config") == "from-config"


def test_password_env_fails_when_environment_variable_is_unset() -> None:
    with pytest.raises(RichBadParameter, match="EZHPCY_PASSWORD is not set"):
        resolve_password(password_env=True)


def test_password_env_cli_fails_loudly_when_variable_is_unset() -> None:
    result = CliRunner().invoke(
        app,
        [
            "tunnel",
            "compute",
            "--scheduler",
            "lsf",
            "--user",
            "alice",
            "--password-env",
        ],
    )

    assert result.exit_code == 2
    assert "EZHPCY_PASSWORD" in result.stderr
    assert "not set" in result.stderr


def test_password_falls_back_to_profile() -> None:
    assert resolve_password(config_password="from-profile") == "from-profile"


def test_explicit_password_source_does_not_implicitly_read_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    password_file = tmp_path / "password"
    password_file.write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv(common.PASSWORD_ENV_VAR, "from-environment")

    assert resolve_password(password_file=password_file) == "from-file"


def test_password_keyring_uses_service_and_user_at_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def get_password(service: str, account: str) -> str:
        calls.append((service, account))
        return "from-keyring"

    monkeypatch.setattr(common.keyring, "get_password", get_password)

    assert resolve_password(password_keyring=True) == "from-keyring"
    assert calls == [(common.KEYRING_SERVICE_NAME, "alice@login.example.com")]


def test_missing_keyring_password_is_an_actionable_parameter_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(common.keyring, "get_password", lambda *_args: None)

    with pytest.raises(RichBadParameter, match="no password was found"):
        resolve_password(password_keyring=True)


def test_keyring_backend_error_is_an_actionable_parameter_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: str) -> None:
        raise KeyringError("backend unavailable")

    monkeypatch.setattr(common.keyring, "get_password", fail)

    with pytest.raises(RichBadParameter, match="backend unavailable"):
        resolve_password(password_keyring=True)


def test_explicit_password_sources_are_mutually_exclusive(tmp_path: Path) -> None:
    password_file = tmp_path / "password"
    password_file.write_text("secret", encoding="utf-8")

    with pytest.raises(RichBadParameter, match="mutually exclusive"):
        resolve_password(password_env=True, password_file=password_file)


@pytest.mark.parametrize(
    "command",
    [
        ["tunnel", "provision"],
        ["tunnel", "prune"],
        ["tunnel", "compute"],
        ["tunnel", "relay"],
        ["tunnel", "broker"],
    ],
)
def test_tunnel_command_help_includes_every_password_source(
    command: list[str],
) -> None:
    result = CliRunner().invoke(app, [*command, "--help"])

    assert result.exit_code == 0
    assert "--password" in result.stdout
    assert "--password-env" in result.stdout
    assert "--password-file" in result.stdout
    assert "--password-fd" in result.stdout
    assert "--password-keyring" in result.stdout
    assert common.PASSWORD_ENV_VAR in result.stdout
    assert "--profile" in result.stdout
