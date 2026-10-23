"""
A second in-process SSH server as the worker's sshd, reached through the login
server as the real one is: `connect_worker` opens its connection over a
`direct-tcpip` channel of the login connection. Use the `worker` fixture.
"""

import asyncio
import shutil
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import asyncssh

from ezhpcy.constants import WORKER_HOST_ALIAS
from ezhpcy.provision_local import WorkerCredentials
from tests.support.login import MACHINE_ID
from tests.support.sshserver import Auth, SSHTestServer, reply

# What the worker's `hostname` says.
NODE = "node-1"


def worker_credentials() -> WorkerCredentials:
    """The worker's credentials, created and pinned as `ezhpcy provision` does."""
    credentials = WorkerCredentials(MACHINE_ID, SSHTestServer.user, "login.example.com")
    credentials.ensure_local_keys()
    credentials.pin_host_key()
    return credentials


def remove_worker_credentials() -> None:
    """Remove the worker's local credentials, as if never provisioned."""
    shutil.rmtree(worker_credentials().local_dir)


@contextmanager
def worker_sshd(
    login: SSHTestServer, root: Path, credentials: WorkerCredentials
) -> Generator[SSHTestServer]:
    """
    Serve the worker's sshd with `credentials`' host key, accepting their client
    key, on a port `login` may forward to. Its `hostname` says `NODE`.
    """
    worker = SSHTestServer(
        root, host_key=asyncssh.read_private_key(credentials.host_key)
    )
    worker.auth = Auth(client_keys=[asyncssh.read_private_key(credentials.client_key)])
    worker.command("hostname", reply(stdout=f"{NODE}\n"))
    with worker:
        login.allow_forwarding(worker.port)
        yield worker


def ssh_through(port: int, command: str = "hostname") -> str:
    """
    As `ssh <alias>` does through a tunnel's Host block: connect to the loopback
    `port` with the worker's credentials, checking its pinned host key, and
    return what `command` prints. Blocks, so call it from a thread.
    """
    credentials = worker_credentials()

    async def run() -> str:
        async with asyncssh.connect(
            "127.0.0.1",
            port,
            username=credentials.login_user,
            client_keys=[str(credentials.client_key)],
            known_hosts=str(credentials.known_hosts),
            host_key_alias=WORKER_HOST_ALIAS,
            agent_path=None,
            config=None,
        ) as conn:
            return str((await conn.run(command, check=True)).stdout)

    return asyncio.run(run())
