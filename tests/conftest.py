from pathlib import Path

import pytest

from ezhpcy.config import config


@pytest.fixture(autouse=True)
def isolated_runtime_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Keep tunnel descriptors and generated SSH configuration out of the real
    runtime directory, which the user's ~/.ssh/config may Include."""
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(config.local_file, "runtime_dir", runtime_dir)
    return runtime_dir


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
