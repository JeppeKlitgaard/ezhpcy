import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from ezhpcy.config import config
from ezhpcy.logging import ASYNCSSH_LOGGER_NAME, TERMINAL_HANDLER_NAME
from tests.support.login import route_login
from tests.support.sshserver import SSHTestServer


@pytest.fixture
def ssh_server(tmp_path: Path) -> Iterator[SSHTestServer]:
    """An in-process SSH server whose remote files live in `tmp_path / "remote"`."""
    with SSHTestServer(tmp_path / "remote") as server:
        yield server


@pytest.fixture
def login(
    ssh_server: SSHTestServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> SSHTestServer:
    """`ssh_server`, which commands connect to as their login node."""
    route_login(ssh_server, monkeypatch, tmp_path / "home")
    return ssh_server


@pytest.fixture(autouse=True)
def isolated_runtime_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Keep tunnel descriptors and generated SSH configuration out of the real
    runtime directory, which the user's ~/.ssh/config may Include."""
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(config.local_file, "runtime_dir", runtime_dir)
    return runtime_dir


@pytest.fixture(autouse=True)
def isolated_config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Keep worker keys and accepted login-node host keys out of the real
    configuration directory."""
    config_dir = tmp_path / "config"
    monkeypatch.setattr(config.local_file, "config_dir", config_dir)
    return config_dir


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


@pytest.fixture(autouse=True)
def quiet_asyncssh_logger() -> Iterator[None]:
    """
    Keep asyncssh's records off EzHPCy's terminal handler, unless a test turns on
    debug logging itself. With `debug = true` in the workstation's config, the
    handler would otherwise be attached for the whole run, and the test server's
    thread, which logs after a test has ended, would write past pytest's capture.
    """
    _detach_asyncssh_logger()
    yield
    _detach_asyncssh_logger()


def _detach_asyncssh_logger() -> None:
    logger = logging.getLogger(ASYNCSSH_LOGGER_NAME)
    logger.setLevel(logging.NOTSET)
    for handler in list(logger.handlers):
        if handler.get_name() == TERMINAL_HANDLER_NAME:
            logger.removeHandler(handler)
