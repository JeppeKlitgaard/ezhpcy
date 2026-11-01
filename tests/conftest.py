import logging
import os
import tempfile
from collections.abc import Generator, Iterator
from pathlib import Path

# `ezhpcy.config` reads `EZHPCY_CONFIG_FILE` when imported, which is below, and the
# CLI reads the other `EZHPCY_*` variables (`Config.from_environment`). Drop every
# `EZHPCY_*` variable (e.g. from `just`'s dotenv-load) and point EZHPCY_CONFIG_FILE
# at a file that doesn't exist, so the suite, and the `ezhpcy` processes tests
# start, run on default settings.
for variable in [name for name in os.environ if name.upper().startswith("EZHPCY_")]:
    del os.environ[variable]
# Rich would fix the CLI's consoles at this width as they are imported, so that
# `invoke`'s wouldn't apply to them, and the terminal's would wrap their messages.
os.environ.pop("COLUMNS", None)
os.environ["EZHPCY_CONFIG_FILE"] = str(
    Path(tempfile.gettempdir()) / f"ezhpcy-tests-{os.getpid()}" / "ezhpcy.toml"
)

import pytest

from ezhpcy.cli._console import flush_output
from ezhpcy.cli._logging import (
    ASYNCSSH_LOGGER_NAME,
    OUTPUT_HANDLER_NAME,
    configure_log_file,
    configure_logging,
)
from ezhpcy.config import ConsoleLoggingConfig, LocalFileConfig, local_files
from ezhpcy.constants import PACKAGE_NAME
from ezhpcy.tunnel import ssh_config
from tests.support.login import MACHINE_ID, route_login
from tests.support.sshserver import SSHTestServer
from tests.support.worker import worker_credentials, worker_sshd

# As the CLI does, so every test sees the same `ezhpcy` logger: one that has the
# terminal handler and doesn't propagate.
configure_logging(ConsoleLoggingConfig().level)

# Longest a test's end waits for the output thread, as a test that failed may have
# left it on a terminal that takes no output.
_FLUSH_SECONDS = 5


@pytest.hookimpl(wrapper=True, trylast=True)
def _flush_output_within_capture() -> Generator[None]:
    """
    Have the output thread write what a test logged before pytest stops capturing,
    around which this wraps: a fixture's teardown is too late, as capture stops
    between a test's phases, and the terminal log would go to the real stderr.
    """
    try:
        return (yield)
    finally:
        flush_output(_FLUSH_SECONDS)


pytest_runtest_setup = _flush_output_within_capture
pytest_runtest_call = _flush_output_within_capture
pytest_runtest_teardown = _flush_output_within_capture


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


@pytest.fixture
def worker(login: SSHTestServer, tmp_path: Path) -> Iterator[SSHTestServer]:
    """
    The worker's sshd for jobs on `login`: a second in-process SSH server that
    `login` forwards to, with the worker's credentials as `ezhpcy provision`
    leaves them (see `tests.support.worker`).
    """
    with worker_sshd(login, tmp_path / "worker", worker_credentials()) as server:
        yield server


@pytest.fixture(autouse=True)
def isolated_local_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> LocalFileConfig:
    """Keep every file EzHPCy reads or writes in `tmp_path`: the configuration file,
    worker keys, accepted login-node host keys, the alias locks, and the generated
    SSH configuration that the user's ~/.ssh/config may Include, and the log
    files. Patched field by field, as modules hold the one `local_files`."""
    for name, value in {
        "cache_dir": tmp_path / "cache",
        "config_dir": tmp_path / "config",
        "data_dir": tmp_path / "data",
        "runtime_dir": tmp_path / "runtime",
        "log_dir": tmp_path / "log",
        "config_file": tmp_path / "config" / "ezhpcy.toml",
    }.items():
        monkeypatch.setattr(local_files, name, value)
    return local_files


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
def quiet_api(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """
    Fail a test whose `ezhpcy.api` calls printed or configured logging. Its logger
    has no terminal handler, as in a library user's program, so that what is
    printed isn't the CLI's.
    """
    root = logging.getLogger()
    package = logging.getLogger(PACKAGE_NAME)
    monkeypatch.setattr(package, "handlers", [])
    monkeypatch.setattr(package, "propagate", True)
    # It would do nothing here, with pytest's handlers on the root logger.
    monkeypatch.setattr(
        logging, "basicConfig", lambda **_: pytest.fail("logging.basicConfig called")
    )
    before = (list(root.handlers), root.level, package.level)
    yield
    assert capsys.readouterr() == ("", "")
    assert (list(root.handlers), root.level, package.level) == before
    assert package.handlers == []
    assert package.propagate


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
