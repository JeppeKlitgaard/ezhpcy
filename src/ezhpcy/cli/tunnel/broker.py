import signal
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
from ezhpcy.ipc import create_broker_backend
from ezhpcy.tunnel.broker import ForegroundBroker


def broker_cmd(
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
) -> None:
    """Run the authenticated worker-stream broker in the foreground."""
    local_machine_or_fail()
    connection_info = connection_info_from_options(
        user=user, password=password, host=host
    )
    backend = create_broker_backend()

    with InteractiveSSHClient(connection_info) as ssh:
        ssh.interactive_connect()
        transport = ssh.get_transport()
        if transport is None or not transport.is_active():
            raise paramiko.SSHException("Login-node SSH session is not active")

        broker = ForegroundBroker(
            transport,
            (worker_host, worker_port),
            backend,
            error_handler=lambda error: typer.echo(
                f"Broker client error: {error}", err=True
            ),
        )
        typer.echo(
            f"Broker IPC ready; worker endpoint {worker_host}:{worker_port} will be "
            "checked on first connection (press Ctrl+C to stop)."
        )
        previous_sigbreak_handler = None
        if hasattr(signal, "SIGBREAK"):
            # A Windows process-group interrupt is delivered as Ctrl+Break.
            # Treat it like Ctrl+C so automated launchers and terminal hosts use
            # the same descriptor/socket cleanup path.
            previous_sigbreak_handler = signal.signal(
                signal.SIGBREAK, signal.default_int_handler
            )
        try:
            broker.serve_forever()
        except KeyboardInterrupt:
            typer.echo("Stopping broker...", err=True)
        finally:
            broker.close()
            if previous_sigbreak_handler is not None:
                signal.signal(signal.SIGBREAK, previous_sigbreak_handler)
