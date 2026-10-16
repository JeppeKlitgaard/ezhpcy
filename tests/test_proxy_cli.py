import subprocess
import sys
import textwrap
from contextlib import nullcontext

import pytest

from ezhpcy.cli import proxy as proxy_module
from ezhpcy.ipc.descriptor import TunnelDescriptor
from tests.support.cli import invoke


def test_proxy_relays_through_the_tunnel_published_for_its_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected: list[TunnelDescriptor] = []
    relayed: list[object] = []

    def connect(descriptor: TunnelDescriptor) -> nullcontext[str]:
        connected.append(descriptor)
        return nullcontext("stream")

    monkeypatch.setattr(proxy_module, "connect", connect)
    monkeypatch.setattr(
        proxy_module,
        "relay_proxy_stdio",
        lambda stream, *_streams: relayed.append(stream),
    )
    descriptor = TunnelDescriptor("gpu", port=4321, authkey=b"a" * 32)
    descriptor.publish()

    result = invoke(["proxy", "gpu"])

    assert result.exit_code == 0, result
    assert connected == [descriptor]
    assert relayed == ["stream"]


def test_proxy_without_a_running_tunnel_reports_how_to_start_one() -> None:
    result = invoke(["proxy", "gpu"])

    assert result.exit_code == 1
    assert "ezhpcy proxy:" in result.stderr
    assert "start `ezhpcy tunnel`" in result.stderr


@pytest.mark.parametrize("alias", ["../escape", "has space", "wild*"])
def test_proxy_rejects_an_alias_that_is_not_a_safe_name(alias: str) -> None:
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
    # `ezhpcy.ipc.descriptor` without looking for a tunnel.
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
    assert "ezhpcy.ipc.descriptor" in loaded
    assert "invalid host alias" in result.stderr
    unwanted = {
        "ezhpcy.cli.tunnel",
        "ezhpcy.tunnel.session",
        "ezhpcy.tunnel.worker",
        "ezhpcy.tunnel.server",
        "ezhpcy.cli.provision",
        "ezhpcy.cli.keyring",
        "ezhpcy.ssh",
        "ezhpcy.scheduler.lsf",
        "ezhpcy.scheduler.pbs",
        "asyncssh",
        "keyring",
        "jinja2",
    }
    assert loaded & unwanted == set()
