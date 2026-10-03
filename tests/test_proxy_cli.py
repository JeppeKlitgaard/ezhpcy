import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from ezhpcy import ipc
from ezhpcy.cli import proxy as proxy_module
from ezhpcy.ipc import create_tunnel_backend
from tests.support.cli import invoke


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
        result = invoke(["proxy", "gpu"])
    finally:
        listener.close()

    assert result.exit_code == 0, result
    assert len(relayed) == 1
    assert relayed[0].address == listener.address
    assert relayed[0].instance_id == backend.instance_id


def test_proxy_without_a_running_tunnel_reports_how_to_start_one(
    runtime_dir: Path,
) -> None:
    result = invoke(["proxy", "gpu"])

    assert result.exit_code == 1
    assert "ezhpcy proxy:" in result.stderr
    assert "start `ezhpcy tunnel`" in result.stderr


@pytest.mark.parametrize("alias", ["../escape", "has space", "wild*"])
def test_proxy_rejects_an_alias_that_is_not_a_safe_name(
    runtime_dir: Path, alias: str
) -> None:
    result = invoke(["proxy", alias])

    assert result.exit_code == 2
    assert "invalid host alias" in result.stderr


def test_proxy_requires_an_alias() -> None:
    result = invoke(["proxy"])

    assert result.exit_code == 2
    assert "ALIAS requires an argument" in result.stderr


def test_proxy_does_not_import_other_commands_or_their_dependencies() -> None:
    # OpenSSH runs `ezhpcy proxy` for every connection, so it should import only
    # what it needs. This needs a fresh interpreter: the test session has already
    # imported everything. The alias is invalid, so the proxy stops in
    # `ezhpcy.ipc` without looking for a tunnel.
    code = textwrap.dedent(
        """
        import sys
        from ezhpcy.cli import main

        try:
            main(["proxy", "has space"])
        except SystemExit:
            pass
        print(*sys.modules)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )

    loaded = set(result.stdout.split())
    assert "ezhpcy.ipc" in loaded
    assert "invalid host alias" in result.stderr
    unwanted = {
        "ezhpcy.cli.tunnel",
        "ezhpcy.cli.provision",
        "ezhpcy.cli.keyring",
        "ezhpcy.ssh",
        "ezhpcy.scheduler.lsf",
        "ezhpcy.scheduler.pbs",
        "paramiko",
        "keyring",
        "jinja2",
    }
    assert loaded & unwanted == set()
