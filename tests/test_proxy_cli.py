import logging
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ezhpcy import ipc
from ezhpcy.cli import app, proxy as proxy_module
from ezhpcy.ipc import create_tunnel_backend


@pytest.fixture
def runtime_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(ipc.config.local_file, "runtime_dir", tmp_path)
    return tmp_path


def test_proxy_relays_through_the_tunnel_published_for_its_alias(
    runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relayed: list[object] = []
    monkeypatch.setattr(
        proxy_module,
        "relay_proxy_stdio",
        lambda backend, *_streams: relayed.append(backend),
    )
    backend = create_tunnel_backend(alias="gpu", authkey=b"a" * 32)
    listener = backend.listen(lambda _connection: None)
    try:
        result = CliRunner().invoke(app, ["proxy", "gpu"])
    finally:
        listener.close()

    assert result.exit_code == 0, result.output
    assert len(relayed) == 1
    assert relayed[0].address == listener.address
    assert relayed[0].instance_id == backend.instance_id


def test_proxy_without_a_running_tunnel_reports_how_to_start_one(
    runtime_dir: Path,
) -> None:
    result = CliRunner().invoke(app, ["proxy", "gpu"])

    assert result.exit_code == 1
    assert "ezhpcy proxy:" in result.stderr
    assert "start `ezhpcy tunnel`" in result.stderr


@pytest.mark.parametrize("alias", ["../escape", "has space", "wild*"])
def test_proxy_rejects_an_alias_that_is_not_a_safe_name(
    runtime_dir: Path, alias: str
) -> None:
    result = CliRunner().invoke(app, ["proxy", alias])

    assert result.exit_code == 2
    assert "invalid host alias" in result.stderr


def test_proxy_takes_debug_logging_from_the_tunnel(
    runtime_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    levels: list[int] = []
    monkeypatch.setattr(proxy_module, "relay_proxy_stdio", lambda *_args: None)
    monkeypatch.setattr(
        proxy_module,
        "configure_logging",
        lambda level, **_kwargs: levels.append(level),
    )
    listener = create_tunnel_backend(alias="gpu", debug=True).listen(
        lambda _connection: None
    )
    try:
        result = CliRunner().invoke(app, ["proxy", "gpu"])
    finally:
        listener.close()

    assert result.exit_code == 0, result.output
    assert levels == [logging.DEBUG]


def test_proxy_requires_an_alias() -> None:
    result = CliRunner().invoke(app, ["proxy"])

    assert result.exit_code == 2
    assert "Missing argument 'alias'" in result.stderr
