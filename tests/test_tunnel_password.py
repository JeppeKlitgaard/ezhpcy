import os
import re
from pathlib import Path

import pytest
from keyring.errors import KeyringError

from ezhpcy.cli import _options, _password, _resolve
from ezhpcy.cli._errors import CliUsageError
from ezhpcy.types import ProfileConfig
from tests.support.cli import invoke


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
    return _password.resolve_password(
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
def base_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _resolve.config,
        "profile",
        {
            "base": ProfileConfig.model_validate(
                {
                    "connection": {"host": "login.example.com", "user": "alice"},
                    "scheduler": {"type": "LSF"},
                }
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
    monkeypatch.setattr(
        _password.keyring, "get_password", lambda *_args: "from-keyring"
    )

    assert resolve_password(config_password_keyring=True) == "from-keyring"


def test_explicit_password_source_overrides_profile_password_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        resolve_password(password="from-command-line", config_password_keyring=True)
        == "from-command-line"
    )


def test_password_keyring_uses_service_and_user_at_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def get_password(service: str, account: str) -> str:
        calls.append((service, account))
        return "from-keyring"

    monkeypatch.setattr(_password.keyring, "get_password", get_password)

    assert resolve_password(password_keyring=True) == "from-keyring"
    assert calls == [(_options.KEYRING_SERVICE_NAME, "alice@login.example.com")]


def test_missing_keyring_password_is_an_actionable_parameter_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_password.keyring, "get_password", lambda *_args: None)

    with pytest.raises(CliUsageError, match="No password was found"):
        resolve_password(password_keyring=True)


def test_keyring_backend_error_is_an_actionable_parameter_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: str) -> None:
        raise KeyringError("backend unavailable")

    monkeypatch.setattr(_password.keyring, "get_password", fail)

    with pytest.raises(CliUsageError, match="backend unavailable"):
        resolve_password(password_keyring=True)


@pytest.mark.parametrize(
    "arguments", [["--queue", "gpu"], ["--scheduler", "LSF"], ["--queue-timeout", "5"]]
)
@pytest.mark.parametrize("command", ["provision", "prune"])
def test_connection_only_commands_reject_job_options(
    command: str, arguments: list[str]
) -> None:
    result = invoke([command, "base", *arguments])

    assert result.exit_code == 2
    assert "Unknown option" in result.stderr


def test_invalid_profile_password_sources_are_reported_as_a_cli_parameter_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _resolve.config,
        "profile",
        {
            "base": ProfileConfig.model_validate(
                {
                    "connection": {
                        "host": "login.example.com",
                        "user": "alice",
                        "password_file": Path("password.txt"),
                        "password_keyring": True,
                    }
                }
            )
        },
    )

    with pytest.raises(CliUsageError):
        _resolve.resolve_profile_config("base")

    result = invoke(
        ["tunnel", "base"],
        color=True,
    )

    assert result.exit_code == 2
    # Each setting is one styled run: Rich's highlighter didn't split it up.
    assert "\x1b[1;39mconnection.password_file\x1b[0m" in result.stderr
    stderr = re.sub(r"\x1b\[[0-9;]*m", "", result.stderr)
    assert "Profile base has mutually exclusive password source settings" in stderr
    assert "Invalid value for" not in result.stdout
    assert "Invalid value for" not in stderr
    assert "password_file, connection.password_keyring" in stderr
