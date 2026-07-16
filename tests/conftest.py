import pytest


@pytest.fixture(autouse=True)
def without_connection_option_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep dotenv-loaded CLI defaults from leaking into individual tests."""
    for environment_variable in (
        "EZHPCY_HOST",
        "EZHPCY_USER",
        "EZHPCY_PROFILE",
        "EZHPCY_PASSWORD",
        "EZHPCY_PASSWORD_FILE",
        "EZHPCY_PASSWORD_FD",
        "EZHPCY_PASSWORD_KEYRING",
    ):
        monkeypatch.delenv(environment_variable, raising=False)
