import signal
from typing import Annotated

import paramiko
import typer

from ezhpcy.cli.tunnel.common import (
    ProfileContext,
    with_profile_options,
)
from ezhpcy.cli.utils.ssh import InteractiveSSHClient
from ezhpcy.ipc import create_broker_backend
from ezhpcy.tunnel.broker import ForegroundBroker


@with_profile_options
def broker_cmd(
    worker_host: Annotated[
        str, typer.Argument(help="Hostname or internal address of the worker node.")
    ],
    worker_port: Annotated[
        int,
        typer.Option("--worker-port", min=1024, max=65535, help="Worker SSH port."),
    ],
    profile_context: ProfileContext,
) -> None:
    """Run the authenticated worker-stream broker in the foreground."""
    backend = create_broker_backend(profile_name=profile_context.name)

    with InteractiveSSHClient(profile_context.connection) as ssh:
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
