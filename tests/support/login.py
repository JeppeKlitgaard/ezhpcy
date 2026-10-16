"""Sending a command's login connection to the in-process SSH server."""

import json
from pathlib import Path, PurePosixPath

import pytest

from ezhpcy import ssh
from ezhpcy.cli.utils import ssh as cli_ssh
from ezhpcy.config import config
from ezhpcy.types import ConnectionInfo, RemoteState
from tests.support.sshserver import SSHTestServer, reply

MACHINE_ID = "machine-id"
CACHE = PurePosixPath("/home/alice/.cache")
STATE = PurePosixPath("/home/alice/.local/state")
REMOTE_STATE = RemoteState(cache_dir=CACHE, state_dir=STATE)


def route_login(
    server: SSHTestServer, monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    """
    Send the commands' login connection to the test server, whose host key is
    trusted, and give it a fake for `remote_state`'s command. The replacement
    `connect_interactive` passes further options on, so a test can wrap it.
    """
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    known_hosts = config.local_file.login_known_hosts_file
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    known_hosts.write_text(server.known_hosts_line, encoding="utf-8")

    connect_interactive = cli_ssh.connect_interactive

    async def connect_to_server(
        conn_info: ConnectionInfo, **options: object
    ) -> ssh.RemoteHost:
        # `ConnectionInfo.host` accepts domain names only.
        conn_info = conn_info.model_copy(update={"host": server.host})
        return await connect_interactive(
            conn_info,
            port=server.port,
            client_keys=[server.client_key],
            agent_path=None,
            **options,
        )

    monkeypatch.setattr(cli_ssh, "connect_interactive", connect_to_server)
    server.command("bash -lc", reply(stdout=remote_state_json()))


def remote_state_json() -> str:
    """What `RemoteHost.remote_state`'s command prints on the test server."""
    return json.dumps({"cache_dir": str(CACHE), "state_dir": str(STATE)})
