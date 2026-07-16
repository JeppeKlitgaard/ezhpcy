import signal
from typing import Annotated

import paramiko
import typer

from ezhpcy.cli.common import (
    CoresOpt,
    ExclusiveOpt,
    GpusOpt,
    HostOpt,
    InteractiveSubmissionCommandOpt,
    MemoryOpt,
    PasswordFdOpt,
    PasswordFileOpt,
    PasswordKeyringOpt,
    PasswordOpt,
    QueueOpt,
    QueueTimeoutOpt,
    SchedulerOpt,
    StartupTimeoutOpt,
    TimeLimitOpt,
    UserOpt,
    profile_context_from_cli,
)
from ezhpcy.cli.utils.ssh import InteractiveSSHClient
from ezhpcy.ipc import create_broker_backend
from ezhpcy.tunnel.broker import ForegroundBroker


def broker_cmd(
    profile_or_worker_host: Annotated[
        str,
        typer.Argument(
            help=(
                "Profile name followed by WORKER_HOST, or WORKER_HOST by itself "
                "for an anonymous configuration."
            )
        ),
    ],
    worker_port: Annotated[
        int,
        typer.Option("--worker-port", min=1024, max=65535, help="Worker SSH port."),
    ] = ...,
    worker_host: Annotated[
        str | None,
        typer.Argument(help="Worker host when a profile is supplied."),
    ] = None,
    user: UserOpt = None,
    password: PasswordOpt = None,
    password_file: PasswordFileOpt = None,
    password_fd: PasswordFdOpt = None,
    password_keyring: PasswordKeyringOpt = False,
    host: HostOpt = None,
    scheduler_type: SchedulerOpt = None,
    queue: QueueOpt = None,
    cores: CoresOpt = None,
    gpus: GpusOpt = None,
    exclusive: ExclusiveOpt = None,
    time_limit: TimeLimitOpt = None,
    memory: MemoryOpt = None,
    queue_timeout_seconds: QueueTimeoutOpt = None,
    startup_timeout_seconds: StartupTimeoutOpt = None,
    interactive_submission_command: InteractiveSubmissionCommandOpt = None,
) -> None:
    """Run the authenticated worker-stream broker in the foreground."""
    if worker_host is None:
        profile = None
        worker_host = profile_or_worker_host
    else:
        profile = profile_or_worker_host
    profile_context = profile_context_from_cli(
        profile=profile,
        user=user,
        password=password,
        password_file=password_file,
        password_fd=password_fd,
        password_keyring=password_keyring,
        host=host,
        scheduler_type=scheduler_type,
        queue=queue,
        cores=cores,
        gpus=gpus,
        exclusive=exclusive,
        time_limit=time_limit,
        memory=memory,
        queue_timeout_seconds=queue_timeout_seconds,
        startup_timeout_seconds=startup_timeout_seconds,
        interactive_submission_command=interactive_submission_command,
    )
    backend = create_broker_backend(
        profile=profile_context.name,
        resolved_config=(
            profile_context.profile if profile_context.name is None else None
        ),
    )

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
