import pytest
from keyring.errors import KeyringError
from typer.testing import CliRunner

from ezhpcy.cli import app, keyring as keyring_cli
from ezhpcy.cli.tunnel import common


def test_keyring_set_is_available_with_connection_options() -> None:
    result = CliRunner().invoke(app, ["keyring", "set", "--help"])

    assert result.exit_code == 0
    assert "--user" in result.stdout
    assert "--host" in result.stdout
    assert "--password-env" in result.stdout
    assert "--password-file" in result.stdout
    assert "--password-fd" in result.stdout


def test_keyring_set_stores_password_for_user_at_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        keyring_cli.keyring,
        "set_password",
        lambda service, account, password: calls.append((service, account, password)),
    )

    result = CliRunner().invoke(
        app,
        [
            "keyring",
            "set",
            "--user",
            "alice",
            "--host",
            "login.example.com",
            "--password",
            "secret",
        ],
    )

    assert result.exit_code == 0
    assert calls == [("ezhpcy", "alice@login.example.com", "secret")]
    assert "alice@login.example.com" in result.stdout
    assert "secret" not in result.stdout


def test_keyring_set_prompts_for_missing_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []
    prompt_calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(common.config.connection, "password", None)

    def prompt(prompt: str, *, password: bool, **_kwargs) -> str:
        prompt_calls.append((prompt, password))
        return "prompted-secret"

    monkeypatch.setattr(keyring_cli.Prompt, "ask", prompt)
    monkeypatch.setattr(
        keyring_cli.keyring,
        "set_password",
        lambda service, account, password: calls.append((service, account, password)),
    )

    result = CliRunner().invoke(
        app,
        ["keyring", "set", "--user", "alice", "--host", "login.example.com"],
    )

    assert result.exit_code == 0
    assert prompt_calls == [("Enter password for alice@login.example.com", True)]
    assert calls == [("ezhpcy", "alice@login.example.com", "prompted-secret")]


def test_keyring_set_reports_backend_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: str) -> None:
        raise KeyringError("backend unavailable")

    monkeypatch.setattr(keyring_cli.keyring, "set_password", fail)

    result = CliRunner().invoke(
        app,
        ["keyring", "set", "--user", "alice", "--password", "secret"],
    )

    assert result.exit_code == 1
    assert "backend unavailable" in result.stdout
    assert "secret" not in result.stdout
