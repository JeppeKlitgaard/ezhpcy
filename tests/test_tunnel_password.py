import logging
import os
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from keyring.errors import KeyringError

from ezhpcy import login as login_module
from ezhpcy.cli import _options, _resolve
from ezhpcy.constants import KEYRING_SERVICE_NAME
from ezhpcy.types import ConfigError, Login, PasswordConfig, ProfileConfig
from tests.support.cli import invoke


def read_password(**sources: object) -> str | None:
    """The password `alice@login.example.com`'s `sources` give, if any."""
    login = Login.model_validate(
        {"user": "alice", "host": "login.example.com", **sources}
    )
    password = login_module.read_password(login)
    return password.get_secret_value() if password is not None else None


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

    assert read_password(password_file=password_file) == "påss word"


def test_password_fd_is_read_without_closing_callers_descriptor() -> None:
    read_fd = _password_fd("secret\n")
    try:
        assert read_password(password_fd=read_fd) == "secret"
        os.fstat(read_fd)
    finally:
        os.close(read_fd)


def test_password_without_an_explicit_or_profile_source_is_none() -> None:
    assert read_password() is None


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
        login_module.keyring, "get_password", lambda *_args: "from-keyring"
    )

    login, _ = _resolve.login_from_cli(
        _options.ConnectionOptions(user="alice", host="login.example.com"),
        profile_name=profile_name,
    )

    assert login.password_prompt is False
    assert login_module.read_password(login).get_secret_value() == "from-keyring"


def test_a_password_on_the_command_line_is_the_one_given_to_connect() -> None:
    _, password = _resolve.login_from_cli(
        _options.ConnectionOptions(password="from-command-line"), profile_name="base"
    )

    assert password is not None
    assert password.get_secret_value() == "from-command-line"


def test_a_password_source_on_the_command_line_replaces_the_profiles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        _resolve.config,
        "connection",
        PasswordConfig(password_fd=0, password_keyring=True),
    )
    password_file = tmp_path / "password"
    password_file.write_text("from-file", encoding="utf-8")

    login, _ = _resolve.login_from_cli(
        _options.ConnectionOptions(password_file=password_file), profile_name="base"
    )

    assert (login.password_fd, login.password_keyring) == (None, False)
    assert login_module.read_password(login).get_secret_value() == "from-file"


def test_password_keyring_uses_service_and_user_at_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def get_password(service: str, account: str) -> str:
        calls.append((service, account))
        return "from-keyring"

    monkeypatch.setattr(login_module.keyring, "get_password", get_password)

    assert read_password(password_keyring=True) == "from-keyring"
    assert calls == [(KEYRING_SERVICE_NAME, "alice@login.example.com")]


def _keyring_without_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(login_module.keyring, "get_password", lambda *_args: None)


def _failing_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: str) -> None:
        raise KeyringError("backend unavailable")

    monkeypatch.setattr(login_module.keyring, "get_password", fail)


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
    logger = logging.getLogger(login_module.__name__)
    logger.addHandler(handler)
    try:
        assert read_password(password_keyring=True) is None
    finally:
        logger.removeHandler(handler)

    [warning] = warnings
    assert message in warning
    assert warning.endswith("The password is asked for if needed.")

    with pytest.raises(ConfigError, match=message):
        read_password(password_keyring=True, password_prompt=False)


def test_configured_password_sources_are_tried_fd_file_then_keyring(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        login_module.keyring, "get_password", lambda *_args: "from-keyring"
    )
    password_file = tmp_path / "password"
    password_file.write_text("from-file", encoding="utf-8")
    password_fd = _password_fd("from-fd")
    try:
        assert (
            read_password(
                password_fd=password_fd,
                password_file=password_file,
                password_keyring=True,
            )
            == "from-fd"
        )
    finally:
        os.close(password_fd)
    assert (
        read_password(password_file=password_file, password_keyring=True) == "from-file"
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
        login_module.keyring, "get_password", lambda *_args: "from-keyring"
    )

    login, _ = _resolve.login_from_cli(
        _options.ConnectionOptions(), profile_name="base"
    )

    assert login_module.read_password(login).get_secret_value() == "from-keyring"


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


def test_a_password_file_that_cant_be_read_is_a_usage_error_naming_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "missing"
    monkeypatch.setattr(
        _resolve.config,
        "profile",
        {
            "base": ProfileConfig.model_validate(
                {
                    "connection": {
                        "host": "login.example.com",
                        "user": "alice",
                        "password_file": missing,
                    },
                    "scheduler": {"type": "LSF", "submission_mode": "batch"},
                }
            )
        },
    )

    result = invoke(["tunnel", "base"])

    assert result.exit_code == 2, result
    stderr = " ".join(result.stderr.split())
    assert "for profile.base.connection.password_file" in stderr
    assert "Could not read the file" in stderr
