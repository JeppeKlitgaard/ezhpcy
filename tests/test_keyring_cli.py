import pytest
from keyring.errors import KeyringError, PasswordDeleteError
from rich.text import Text

from ezhpcy.cli import keyring as keyring_cli
from tests.support.cli import invoke


def test_keyring_set_stores_password_for_user_at_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        keyring_cli.keyring,
        "set_password",
        lambda service, account, password: calls.append((service, account, password)),
    )

    result = invoke(
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

    def prompt(prompt: Text, *, password: bool, **_kwargs) -> str:
        prompt_calls.append((str(prompt), password))
        return "prompted-secret"

    monkeypatch.setattr(keyring_cli.Prompt, "ask", prompt)
    monkeypatch.setattr(
        keyring_cli.keyring,
        "set_password",
        lambda service, account, password: calls.append((service, account, password)),
    )

    result = invoke(
        ["keyring", "set", "--user", "alice", "--host", "login.example.com"],
    )

    assert result.exit_code == 0
    assert prompt_calls == [("Enter password for alice@login.example.com", True)]
    assert calls == [("ezhpcy", "alice@login.example.com", "prompted-secret")]


def test_keyring_set_reports_backend_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: str) -> None:
        raise KeyringError("backend unavailable")

    monkeypatch.setattr(keyring_cli.keyring, "set_password", fail)

    result = invoke(
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

    assert result.exit_code == 1
    assert "backend unavailable" in result.stderr
    assert "secret" not in result.stdout + result.stderr


def test_keyring_unset_removes_password_for_user_at_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        keyring_cli.keyring,
        "delete_password",
        lambda service, account: calls.append((service, account)),
    )

    result = invoke(
        ["keyring", "unset", "-h", "login.example.com"],
        env={"EZHPCY_USER": "alice"},
    )

    assert result.exit_code == 0, result
    assert calls == [("ezhpcy", "alice@login.example.com")]
    assert "alice@login.example.com" in result.stdout


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (PasswordDeleteError("Password not found"), "No password is stored"),
        (KeyringError("backend unavailable"), "backend unavailable"),
    ],
)
def test_keyring_unset_reports_backend_errors(
    monkeypatch: pytest.MonkeyPatch, error: KeyringError, message: str
) -> None:
    def fail(*_args: str) -> None:
        raise error

    monkeypatch.setattr(keyring_cli.keyring, "delete_password", fail)

    result = invoke(
        ["keyring", "unset", "--user", "alice", "--host", "login.example.com"],
    )

    assert result.exit_code == 1
    assert message in result.stderr


def test_keyring_unset_takes_no_password_options() -> None:
    result = invoke(
        [
            "keyring",
            "unset",
            "--user",
            "alice",
            "--host",
            "login.example.com",
            "--password",
            "secret",
        ],
    )

    assert result.exit_code == 2
    assert "--password" in result.stderr


def test_keyring_set_names_the_option_of_an_invalid_host() -> None:
    result = invoke(
        ["keyring", "set", "--user", "alice", "--host", "not a host", "--password", "x"]
    )

    assert result.exit_code == 2
    assert 'Invalid value "not a host" for --host' in result.stderr
    assert "must be a domain name or an IP address" in result.stderr
