import logging
import os
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from keyring.errors import KeyringError

from ezhpcy.cli import _options, _password, _resolve
from ezhpcy.cli._errors import CliUsageError
from ezhpcy.types import PasswordConfig, ProfileConfig
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
    password_prompt: bool = True,
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
        password_prompt=password_prompt,
    )


def _password_fd(password: str) -> int:
    """Return a readable descriptor holding `password`; the test closes it."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, password.encode())
    os.close(write_fd)
    return read_fd


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
    read_fd = _password_fd("secret\n")
    try:
        assert resolve_password(password_fd=read_fd) == "secret"
        os.fstat(read_fd)
    finally:
        os.close(read_fd)


def test_password_without_an_explicit_or_profile_source_is_none() -> None:
    assert resolve_password() is None


@pytest.mark.parametrize("profile_name", [None, "base"])
def test_top_level_keyring_and_prompt_reach_the_connection(
    monkeypatch: pytest.MonkeyPatch, profile_name: str | None
) -> None:
    """
    With both enabled, the keyring's password is used, and the prompt setting
    kept for when it is needed.
    """
    monkeypatch.setattr(
        _resolve.config,
        "connection",
        PasswordConfig(password_keyring=True, password_prompt=False),
    )
    monkeypatch.setattr(
        _password.keyring, "get_password", lambda *_args: "from-keyring"
    )

    conn_info = _resolve.connection_from_cli(
        _options.ConnectionOptions(user="alice", host="login.example.com"),
        _resolve.resolve_profile_config(profile_name).connection,
        profile_name=profile_name,
    )

    assert conn_info.password == "from-keyring"
    assert conn_info.password_prompt is False


def test_explicit_password_source_overrides_profile_password_source() -> None:
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


def _keyring_without_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_password.keyring, "get_password", lambda *_args: None)


def _failing_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: str) -> None:
        raise KeyringError("backend unavailable")

    monkeypatch.setattr(_password.keyring, "get_password", fail)


@pytest.mark.parametrize(
    ("keyring_state", "message"),
    [
        (_keyring_without_password, "No password was found"),
        (_failing_keyring, "backend unavailable"),
    ],
)
def test_keyring_without_a_password_leaves_it_to_the_prompt_if_allowed(
    monkeypatch: pytest.MonkeyPatch,
    keyring_state: Callable[[pytest.MonkeyPatch], None],
    message: str,
) -> None:
    keyring_state(monkeypatch)
    # The `ezhpcy` logger doesn't propagate, so `caplog` doesn't see it.
    warnings: list[str] = []
    handler = logging.Handler(logging.WARNING)
    handler.emit = lambda record: warnings.append(record.getMessage())
    logger = logging.getLogger(_password.__name__)
    logger.addHandler(handler)
    try:
        assert resolve_password(config_password_keyring=True) is None
    finally:
        logger.removeHandler(handler)

    [warning] = warnings
    assert message in warning
    assert warning.endswith("The password is asked for if needed.")

    with pytest.raises(CliUsageError, match=message):
        resolve_password(config_password_keyring=True, password_prompt=False)


def test_configured_password_sources_are_tried_fd_file_then_keyring(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        _password.keyring, "get_password", lambda *_args: "from-keyring"
    )
    password_file = tmp_path / "password"
    password_file.write_text("from-file", encoding="utf-8")
    password_fd = _password_fd("from-fd")
    try:
        assert (
            resolve_password(
                config_password_fd=password_fd,
                config_password_file=password_file,
                config_password_keyring=True,
            )
            == "from-fd"
        )
    finally:
        os.close(password_fd)
    assert (
        resolve_password(
            config_password_file=password_file, config_password_keyring=True
        )
        == "from-file"
    )


def test_a_disabled_password_file_and_fd_leave_it_to_the_keyring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _resolve.config,
        "connection",
        PasswordConfig(password_fd=0, password_file=Path("password.txt")),
    )
    monkeypatch.setattr(
        _resolve.config,
        "profile",
        {
            "base": ProfileConfig.model_validate(
                {
                    "connection": {
                        "host": "login.example.com",
                        "user": "alice",
                        "password_fd": False,
                        "password_file": False,
                        "password_keyring": True,
                    }
                }
            )
        },
    )
    monkeypatch.setattr(
        _password.keyring, "get_password", lambda *_args: "from-keyring"
    )

    conn_info = _resolve.connection_from_cli(
        _options.ConnectionOptions(),
        _resolve.resolve_profile_config("base").connection,
        profile_name="base",
    )

    assert conn_info.password == "from-keyring"


def test_invalid_profile_password_settings_are_reported_as_a_cli_parameter_error(
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
                        "password": "hunter2",
                    }
                }
            )
        },
    )

    result = invoke(["tunnel", "base"], color=True)

    assert result.exit_code == 2
    # Each setting is one styled run: Rich's highlighter didn't split it up.
    assert "\x1b[1;39mprofile.base.connection.password\x1b[0m" in result.stderr
    stderr = re.sub(r"\x1b\[[0-9;]*m", "", result.stderr)
    assert "profile.base.connection.password must not be set" in stderr
    assert "Invalid value for" not in result.stdout
    assert "Invalid value for" not in stderr
