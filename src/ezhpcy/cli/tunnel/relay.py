from typing import Annotated

import paramiko
import typer

from ezhpcy.cli.tunnel.common import (
    HostOpt,
    PasswordOpt,
    UserOpt,
    connection_info_from_options,
    local_machine_or_fail,
)
from ezhpcy.cli.utils.ssh import InteractiveSSHClient
from ezhpcy.config import config
from ezhpcy.tunnel.relay import DirectTCPIPRelay


def relay_cmd(
    worker_host: Annotated[
        str, typer.Argument(help="Hostname or internal address of the worker node.")
    ],
    worker_port: Annotated[
        int,
        typer.Option("--worker-port", min=1024, max=65535, help="Worker SSH port."),
    ],
    user: UserOpt = config.connection.user,
    password: PasswordOpt = config.connection.password,
    host: HostOpt = config.connection.host,
    listen_port: Annotated[
        int,
        typer.Option(
            "--listen-port",
            min=0,
            max=65535,
            help="Loopback port; zero selects an available port.",
        ),
    ] = 0,
) -> None:
    """Relay local TCP connections to a manually started worker SSH daemon."""
    local_machine_or_fail()
    connection_info = connection_info_from_options(
        user=user, password=password, host=host
    )

    with InteractiveSSHClient(connection_info) as ssh:
        ssh.interactive_connect()
        transport = ssh.get_transport()
        if transport is None or not transport.is_active():
            raise paramiko.SSHException("Login-node SSH session is not active")

        with DirectTCPIPRelay(
            transport,
            (worker_host, worker_port),
            listen_port=listen_port,
            client_error_handler=lambda origin, _error: typer.echo(
                f"Relay connection for SSH client {origin[0]}:{origin[1]} "
                "was reset by a peer; the channel was closed cleanly.",
                err=True,
            ),
        ) as relay:
            listen_host, bound_port = relay.address
            typer.echo(
                f"Relay ready on {listen_host}:{bound_port} (press Ctrl+C to stop)."
            )
            try:
                relay.serve_forever(poll_interval=0.25)
            except KeyboardInterrupt:
                pass
