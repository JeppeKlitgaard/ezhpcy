import os
from pathlib import Path

import pytest
from keyring.errors import KeyringError
from typer.testing import CliRunner

from ezhpcy.cli import app, common
from ezhpcy.cli.utils.bad_parameter import RichBadParameter
from ezhpcy.types import ProfileConfig


def resolve_password(
    *,
    password: str | None = None,
    password_file: Path | None = None,
    password_fd: int | None = None,
    password_keyring: bool = False,
    config_password_file: Path | None = None,
    config_password_fd: int | None = None,
    config_password_keyring: bool = False,
) -> str | None:
    return common.resolve_password(
        password=password,
        password_file=password_file,
        password_fd=password_fd,
        password_keyring=password_keyring,
        user="alice",
        host="login.example.com",
        config_password_file=config_password_file,
        config_password_fd=config_password_fd,
        config_password_keyring=config_password_keyring,
    )


@pytest.fixture(autouse=True)
def without_default_password(monkeypatch: pytest.MonkeyPatch) -> None:
    for environment_variable in (
        common.HOST_ENV_VAR,
        common.USER_ENV_VAR,
        common.PASSWORD_ENV_VAR,
        common.PASSWORD_FILE_ENV_VAR,
        common.PASSWORD_FD_ENV_VAR,
        common.PASSWORD_KEYRING_ENV_VAR,
    ):
        monkeypatch.delenv(environment_variable, raising=False)
    monkeypatch.setattr(
        common.config,
        "profile",
        {
            "base": ProfileConfig(
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


def test_password_without_an_explicit_or_profile_source_is_none() -> None:
    assert resolve_password() is None


def test_profile_password_keyring_can_read_from_keyring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(common.keyring, "get_password", lambda *_args: "from-keyring")

    assert resolve_password(config_password_keyring=True) == "from-keyring"


def test_explicit_password_source_overrides_profile_password_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        resolve_password(password="from-command-line", config_password_keyring=True)
        == "from-command-line"
    )


def test_password_file_is_an_explicit_password_source(
    tmp_path: Path,
) -> None:
    password_file = tmp_path / "password"
    password_file.write_text("from-file\n", encoding="utf-8")

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
        resolve_password(password="secret", password_file=password_file)


def test_invalid_profile_password_sources_are_reported_as_a_cli_parameter_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        common.config,
        "profile",
        {
            "base": ProfileConfig(
                host="login.example.com",
                user="alice",
                password_file=Path("password.txt"),
                password_keyring=True,
            )
        },
    )

    with pytest.raises(RichBadParameter):
        common.profile_context_from_options(profile="base")

    result = CliRunner().invoke(
        app,
        [
            "relay",
            "worker.example.com",
            "--worker-port",
            "2222",
            "--profile",
            "base",
        ],
        color=True,
    )

    assert result.exit_code == 2
    assert "Invalid value:" in result.output
    assert "Invalid value for --profile" not in result.output
    assert "password_file, password_keyring" in result.output


def test_connection_options_read_password_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "ezhpcy.cli.keyring.keyring.set_password",
        lambda service, account, password: calls.append((service, account, password)),
    )

    result = CliRunner().invoke(
        app,
        ["keyring", "set"],
        env={
            common.HOST_ENV_VAR: "login.example.com",
            common.USER_ENV_VAR: "alice",
            common.PASSWORD_ENV_VAR: "from-environment",
        },
    )

    assert result.exit_code == 0, result.output
    assert calls == [("ezhpcy", "alice@login.example.com", "from-environment")]


def test_connection_options_read_password_file_from_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    password_file = tmp_path / "password"
    password_file.write_text("from-file\n", encoding="utf-8")
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "ezhpcy.cli.keyring.keyring.set_password",
        lambda service, account, password: calls.append((service, account, password)),
    )

    result = CliRunner().invoke(
        app,
        ["keyring", "set"],
        env={
            common.HOST_ENV_VAR: "login.example.com",
            common.USER_ENV_VAR: "alice",
            common.PASSWORD_FILE_ENV_VAR: str(password_file),
        },
    )

    assert result.exit_code == 0, result.output
    assert calls == [("ezhpcy", "alice@login.example.com", "from-file")]


def test_connection_options_read_password_fd_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "ezhpcy.cli.keyring.keyring.set_password",
        lambda service, account, password: calls.append((service, account, password)),
    )
    try:
        os.write(write_fd, b"from-file-descriptor\n")
        os.close(write_fd)
        write_fd = -1

        result = CliRunner().invoke(
            app,
            ["keyring", "set"],
            env={
                common.HOST_ENV_VAR: "login.example.com",
                common.USER_ENV_VAR: "alice",
                common.PASSWORD_FD_ENV_VAR: str(read_fd),
            },
        )
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)

    assert result.exit_code == 0, result.output
    assert calls == [("ezhpcy", "alice@login.example.com", "from-file-descriptor")]


def test_connection_options_read_password_keyring_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(common.keyring, "get_password", lambda *_args: "from-keyring")
    monkeypatch.setattr(
        "ezhpcy.cli.keyring.keyring.set_password",
        lambda service, account, password: calls.append((service, account, password)),
    )

    result = CliRunner().invoke(
        app,
        ["keyring", "set"],
        env={
            common.HOST_ENV_VAR: "login.example.com",
            common.USER_ENV_VAR: "alice",
            common.PASSWORD_KEYRING_ENV_VAR: "true",
        },
    )

    assert result.exit_code == 0, result.output
    assert calls == [("ezhpcy", "alice@login.example.com", "from-keyring")]


@pytest.mark.parametrize(
    "command",
    [
        ["provision"],
        ["prune"],
        ["compute"],
        ["relay"],
        ["broker"],
    ],
)
def test_remote_command_help_includes_every_password_source(
    command: list[str],
) -> None:
    result = CliRunner().invoke(app, [*command, "--help"])

    assert result.exit_code == 0
    assert "--password" in result.stdout
    assert "--password-file" in result.stdout
    assert "--password-fd" in result.stdout
    assert "--password-keyring" in result.stdout
    assert "--profile" in result.stdout
