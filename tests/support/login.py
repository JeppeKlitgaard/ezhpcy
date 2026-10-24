"""Sending a command's login connection to the in-process SSH server."""

import json
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from unittest import mock

import pytest

from ezhpcy import login as login_module, ssh
from ezhpcy.config import config
from ezhpcy.types import Login, RemoteState
from tests.support.sshserver import SSHTestServer, reply

MACHINE_ID = "machine-id"
CACHE = PurePosixPath("/home/alice/.cache")
STATE = PurePosixPath("/home/alice/.local/state")
REMOTE_STATE = RemoteState(cache_dir=CACHE, state_dir=STATE)


def route_login(
    server: SSHTestServer, monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    """
    Send every login connection to the test server, whose host key is trusted,
    and give it a fake for `remote_state`'s command.
    """
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    known_hosts = config.local_file.login_known_hosts_file
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    known_hosts.write_text(server.known_hosts_line, encoding="utf-8")

    connect = login_module.connect

    async def connect_to_server(login: Login, **options: object) -> ssh.RemoteHost:
        # Profiles' hosts are domain names.
        routed = login.model_copy(update={"host": server.host, "port": server.port})
        with ssh_options(client_keys=[server.client_key], agent_path=None):
            host = await connect(routed, **options)
        # As connected to the login node configured, whose worker credentials
        # those are.
        host.login = login
        return host

    monkeypatch.setattr(login_module, "connect", connect_to_server)
    server.command("bash -lc", reply(stdout=remote_state_json()))


@contextmanager
def ssh_options(**options: object) -> Generator[None]:
    """
    Pass `options` on to `asyncssh.connect` from every login connection made
    meanwhile (`ezhpcy.ssh.connect`), under those EzHPCy gives itself.
    """
    connect = ssh.connect

    async def with_options(login: Login, **given: object) -> ssh.RemoteHost:
        return await connect(login, **(options | given))

    with mock.patch.object(ssh, "connect", with_options):
        yield


def offer_client_key(
    server: SSHTestServer, monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    """
    Have every connection offer only the test server's client key, from a home
    directory of its own (so an empty ~/.ssh/known_hosts), without trusting the
    server's host key: for tests of the login flow itself.
    """
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    connect = ssh.connect

    async def connect_with_key(login: Login, **options: object) -> ssh.RemoteHost:
        return await connect(
            login, client_keys=[server.client_key], agent_path=None, **options
        )

    monkeypatch.setattr(ssh, "connect", connect_with_key)


def remote_state_json() -> str:
    """What `RemoteHost.remote_state`'s command prints on the test server."""
    return json.dumps({"cache_dir": str(CACHE), "state_dir": str(STATE)})
