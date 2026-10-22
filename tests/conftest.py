import logging
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

# `ezhpcy.config` loads the workstation's settings when it's first imported, which
# is below. Drop every `EZHPCY_*` variable (e.g. from `just`'s dotenv-load) and point
# EZHPCY_CONFIG_FILE at a file that doesn't exist, so the suite, and the `ezhpcy`
# processes tests start, run on default settings.
for variable in [name for name in os.environ if name.upper().startswith("EZHPCY_")]:
    del os.environ[variable]
os.environ["EZHPCY_CONFIG_FILE"] = str(
    Path(tempfile.gettempdir()) / f"ezhpcy-tests-{os.getpid()}" / "ezhpcy.toml"
)

import pytest

from ezhpcy.config import LocalFileConfig, config
from ezhpcy.logging import (
    ASYNCSSH_LOGGER_NAME,
    OUTPUT_HANDLER_NAME,
    configure_log_file,
    configure_logging,
)
from ezhpcy.tunnel import ssh_config
from tests.support.login import MACHINE_ID, route_login
from tests.support.sshserver import SSHTestServer

# As the CLI does, so every test sees the same `ezhpcy` logger: one that has the
# terminal handler and doesn't propagate.
configure_logging(config.logging.console.level)


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
def isolated_local_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> LocalFileConfig:
    """Keep every file EzHPCy reads or writes in `tmp_path`: the configuration file,
    worker keys, accepted login-node host keys, the alias locks, and the generated
    SSH configuration that the user's ~/.ssh/config may Include, and the log
    files."""
    local_file = LocalFileConfig(
        cache_dir=tmp_path / "cache",
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        runtime_dir=tmp_path / "runtime",
        log_dir=tmp_path / "log",
        config_file=tmp_path / "config" / "ezhpcy.toml",
    )
    monkeypatch.setattr(config, "local_file", local_file)
    return local_file


@pytest.fixture(autouse=True)
def fixed_machine_id(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Give every test the same machine ID, except those marked `real_machine_id`.
    On macOS `machineid` runs `ioreg` through `subprocess.run`, so a test that
    fakes `subprocess.run` would otherwise break any code that asks for the ID.
    """
    if request.node.get_closest_marker("real_machine_id"):
        return
    monkeypatch.setattr("ezhpcy.utils.machineid.hashed_id", lambda _: MACHINE_ID)


@pytest.fixture(autouse=True)
def no_openssh_resolution(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Answer `ssh -G` as if no OpenSSH client were installed, except in tests
    marked `real_resolve_alias`: a real one reads the workstation's own
    ~/.ssh/config, wherever HOME points.
    """
    if request.node.get_closest_marker("real_resolve_alias"):
        return
    monkeypatch.setattr(ssh_config, "resolve_alias", lambda _alias: None)


@pytest.fixture(autouse=True)
def no_log_file() -> Iterator[None]:
    """Stop writing the log file a CLI run started, in the test's `tmp_path`."""
    yield
    configure_log_file(None)


@pytest.fixture
def isolated_runtime_dir(isolated_local_files: LocalFileConfig) -> Path:
    return isolated_local_files.runtime_dir


@pytest.fixture(autouse=True)
def quiet_asyncssh_logger() -> Iterator[None]:
    """
    Detach asyncssh's records from EzHPCy's terminal handler after a test that
    turns on debug logging, because the test server's thread logs after the test
    has ended and would write past pytest's capture.
    """
    _detach_asyncssh_logger()
    yield
    _detach_asyncssh_logger()


def _detach_asyncssh_logger() -> None:
    logger = logging.getLogger(ASYNCSSH_LOGGER_NAME)
    logger.setLevel(logging.NOTSET)
    for handler in list(logger.handlers):
        if handler.get_name() == OUTPUT_HANDLER_NAME:
            logger.removeHandler(handler)
