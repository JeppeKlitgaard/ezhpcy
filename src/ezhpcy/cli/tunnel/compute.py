import logging
import re
import secrets
import shlex
import signal
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Annotated

import paramiko
import typer

from ezhpcy.cli.tunnel.common import local_machine_or_fail, with_connection_options
from ezhpcy.cli.utils.ssh import InteractiveSSHClient
from ezhpcy.config import ConnectionInfo, config
from ezhpcy.constants import (
    PACKAGE_NAME,
    SSH_DIRECTORY_NAME,
    WORKER_CLIENT_KEY_NAME,
    WORKER_HOST_ALIAS,
)
from ezhpcy.ipc import create_broker_backend
from ezhpcy.ipc.common import IPCError
from ezhpcy.scheduler.base import (
    InteractiveJob,
    JobInfo,
    JobSpec,
    JobState,
    Scheduler,
    SchedulerError,
    UnsupportedSchedulerError,
)
from ezhpcy.scheduler.lsf import LSFScheduler
from ezhpcy.scheduler.pbs import PBSScheduler
from ezhpcy.scheduler.types import SchedulerType
from ezhpcy.tunnel.broker import ForegroundBroker

_FIRST_DYNAMIC_PORT = 49152
_LAST_DYNAMIC_PORT = 65535
_JOB_POLL_INTERVAL = 1.0
_JOB_MONITOR_INTERVAL = 5.0
_SSH_BANNER_LIMIT = 255
_MEMORY_PATTERN = re.compile(
    r"^(?P<amount>(?:\d+(?:\.\d*)?|\.\d+))\s*(?P<unit>[KMGT]?I?B)?$",
    re.IGNORECASE,
)
_MEMORY_UNIT_IN_MB = {
    "B": Decimal(1) / (1024**2),
    "KB": Decimal(1) / 1024,
    "KIB": Decimal(1) / 1024,
    "MB": Decimal(1),
    "MIB": Decimal(1),
    "GB": Decimal(1024),
    "GIB": Decimal(1024),
    "TB": Decimal(1024**2),
    "TIB": Decimal(1024**2),
}


class ComputeTunnelError(RuntimeError):
    pass


def _parse_memory_mb(value: str | None) -> int | None:
    if value is None:
        return None
    match = _MEMORY_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ValueError(
            "memory must be a positive number optionally followed by "
            "B, KB, MB, GB, or TB"
        )

    amount = Decimal(match.group("amount"))
    if amount <= 0:
        raise ValueError("memory must be positive")
    unit = (match.group("unit") or "MB").upper()
    memory_mb = amount * _MEMORY_UNIT_IN_MB[unit]
    return int(memory_mb.to_integral_value(rounding=ROUND_CEILING))


def _select_worker_port() -> int:
    return _FIRST_DYNAMIC_PORT + secrets.randbelow(
        _LAST_DYNAMIC_PORT - _FIRST_DYNAMIC_PORT + 1
    )


def _ensure_local_worker_credentials() -> None:
    ssh_directory = config.local_file.config_dir / SSH_DIRECTORY_NAME
    required_files = (
        ssh_directory / WORKER_CLIENT_KEY_NAME,
        ssh_directory / "worker_known_hosts",
    )
    missing = [str(path) for path in required_files if not path.is_file()]
    if missing:
        raise ComputeTunnelError(
            "worker SSH credentials are missing; run `ezhpcy tunnel install` first "
            f"(missing: {', '.join(missing)})"
        )


def _wait_for_running_job(
    scheduler: Scheduler,
    job_id: str,
    *,
    timeout_seconds: float,
    poll_interval: float = _JOB_POLL_INTERVAL,
    state_handler: Callable[[JobInfo], None] | None = None,
) -> JobInfo:
    deadline = time.monotonic() + timeout_seconds
    previous_state: tuple[JobState, str] | None = None

    while True:
        info = scheduler.inspect(job_id)
        current_state = (info.state, info.raw_state)
        if current_state != previous_state:
            previous_state = current_state
            if state_handler is not None:
                state_handler(info)

        if info.state is JobState.RUNNING and info.primary_host is not None:
            return info
        if info.state.is_terminal:
            exit_detail = (
                f" with exit code {info.exit_code}"
                if info.exit_code is not None
                else ""
            )
            raise ComputeTunnelError(
                f"worker job {job_id} ended in scheduler state "
                f"{info.raw_state}{exit_detail}"
            )
        if info.state is JobState.UNKNOWN:
            raise ComputeTunnelError(
                f"worker job {job_id} entered unknown scheduler state {info.raw_state}"
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ComputeTunnelError(
                f"worker job {job_id} did not start within {timeout_seconds:g} seconds"
            )
        time.sleep(min(poll_interval, remaining))


def _wait_for_worker_endpoint(
    transport: paramiko.Transport,
    destination: tuple[str, int],
    *,
    timeout_seconds: float,
    poll_interval: float = _JOB_POLL_INTERVAL,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    transport_logger = logging.getLogger("paramiko.transport")
    previous_log_level = transport_logger.level
    transport_logger.setLevel(logging.CRITICAL + 1)

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                detail = f": {last_error}" if last_error is not None else ""
                raise ComputeTunnelError(
                    f"worker SSH endpoint {destination[0]}:{destination[1]} did not "
                    f"become ready within {timeout_seconds:g} seconds{detail}"
                )

            channel = None
            try:
                channel = transport.open_channel(
                    "direct-tcpip",
                    dest_addr=destination,
                    src_addr=("ezhpcy-readiness", 0),
                    timeout=min(5.0, remaining),
                )
                channel.settimeout(min(5.0, remaining))
                banner = bytearray()
                while len(banner) < _SSH_BANNER_LIMIT and b"\n" not in banner:
                    chunk = channel.recv(_SSH_BANNER_LIMIT - len(banner))
                    if not chunk:
                        break
                    banner.extend(chunk)
                if bytes(banner).startswith(b"SSH-"):
                    return
                last_error = ComputeTunnelError(
                    f"unexpected worker banner {bytes(banner)!r}"
                )
            except (EOFError, OSError, paramiko.SSHException) as error:
                last_error = error
            finally:
                if channel is not None:
                    channel.close()

            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(poll_interval, remaining))
    finally:
        transport_logger.setLevel(previous_log_level)


def _monitor_job(
    scheduler: Scheduler,
    job_id: str,
    broker: ForegroundBroker,
    stop_requested: threading.Event,
    job_finished: threading.Event,
    errors: list[Exception],
) -> None:
    while not stop_requested.wait(_JOB_MONITOR_INTERVAL):
        try:
            info = scheduler.inspect(job_id)
        except SchedulerError as error:
            errors.append(error)
            broker.close()
            return

        if info.state.is_terminal or info.state is JobState.UNKNOWN:
            job_finished.set()
            typer.echo(
                f"Worker job {job_id} ended in scheduler state {info.raw_state}.",
                err=True,
            )
            broker.close()
            return


def _drain_interactive_job(
    job: InteractiveJob, stop_requested: threading.Event
) -> None:
    while not stop_requested.wait(0.05):
        output = job.read_available()
        if output:
            typer.echo(output.decode(errors="replace"), nl=False, err=True)
        if job.process.exit_status_ready():
            output = job.read_available()
            if output:
                typer.echo(output.decode(errors="replace"), nl=False, err=True)
            return


def _run_compute_tunnel(
    *,
    conn_info: ConnectionInfo,
    scheduler_type: SchedulerType,
    queue: str | None,
    slots: int | None,
    time_limit_minutes: int | None,
    memory_mb: int | None,
    queue_timeout_seconds: float,
    startup_timeout_seconds: float,
    worker_port: int,
) -> None:
    with InteractiveSSHClient(conn_info) as ssh:
        ssh.interactive_connect()
        transport = ssh.get_transport()
        if transport is None or not transport.is_active():
            raise paramiko.SSHException("Login-node SSH session is not active")

        try:
            ssh.run(["ezhpcy", "compute", "ssh-serve", "--help"])
        except RuntimeError as error:
            raise ComputeTunnelError(
                "the remote ezhpcy installation does not provide worker SSH; run "
                "`ezhpcy tunnel reinstall`"
            ) from error

        remote_files = ssh.get_file_config()
        match scheduler_type:
            case SchedulerType.LSF:
                effective_queue = queue or config.hpc.lsf_queue
                effective_slots = slots if slots is not None else config.hpc.lsf_slots
                scheduler: Scheduler = LSFScheduler(
                    ssh.run_login_shell,
                    ssh.start_login_shell,
                    interactive_application_profile=(
                        config.hpc.lsf_application_profile
                    ),
                    interactive_submission_environment=(
                        config.hpc.lsf_submission_environment
                    ),
                    interactive_export_environment=(
                        config.hpc.lsf_export_environment
                    ),
                )
            case SchedulerType.PBS:
                effective_queue = queue or config.hpc.pbs_queue
                effective_slots = slots
                scheduler = PBSScheduler(
                    ssh.run_login_shell,
                    ssh.start_login_shell,
                    command_directory=config.hpc.pbs_command_directory,
                )
            case _:
                raise UnsupportedSchedulerError(scheduler_type)
        typer.echo(f"Using {scheduler_type.value} scheduler.")
        worker_directory = remote_files.data_dir / PACKAGE_NAME
        spec = JobSpec(
            command=("ezhpcy", "compute", "ssh-serve", str(worker_port)),
            name="ezhpcy-worker",
            time_limit=(
                timedelta(minutes=time_limit_minutes)
                if time_limit_minutes is not None
                else None
            ),
            memory_mb=memory_mb,
            slots=effective_slots,
            queue=effective_queue,
            working_directory=worker_directory,
            stdout_path=worker_directory / "worker-%J.out",
            stderr_path=worker_directory / "worker-%J.err",
        )

        interactive_job = scheduler.submit_interactive(
            spec,
            startup_timeout=startup_timeout_seconds,
        )
        job_id = interactive_job.job_id
        typer.echo(f"Submitted worker job {job_id}.")
        if interactive_job.submission_command:
            typer.echo(
                f"Submission command: {shlex.join(interactive_job.submission_command)}"
            )
        if scheduler_type is SchedulerType.LSF:
            typer.echo(
                "Worker logs: "
                f"{str(spec.stdout_path).replace('%J', job_id)} and "
                f"{str(spec.stderr_path).replace('%J', job_id)}."
            )
        job_finished = threading.Event()
        output_stop = threading.Event()
        output_thread = threading.Thread(
            target=_drain_interactive_job,
            args=(interactive_job, output_stop),
            daemon=True,
                name="ezhpcy-compute-job-output",
        )
        output_thread.start()
        try:
            info = _wait_for_running_job(
                scheduler,
                job_id,
                timeout_seconds=queue_timeout_seconds,
                state_handler=lambda snapshot: typer.echo(
                    f"Worker job {job_id}: {snapshot.state.value} "
                    f"({snapshot.raw_state})."
                ),
            )
            worker_host = info.primary_host
            if worker_host is None:
                raise ComputeTunnelError(
                    f"scheduler did not report a host for running job {job_id}"
                )
            interactive_job.start_payload()
            destination = (worker_host, worker_port)
            typer.echo(f"Waiting for worker SSH endpoint {worker_host}:{worker_port}.")
            _wait_for_worker_endpoint(
                transport,
                destination,
                timeout_seconds=startup_timeout_seconds,
            )

            backend = create_broker_backend()
            broker = ForegroundBroker(
                transport,
                destination,
                backend,
                error_handler=lambda error: typer.echo(
                    f"Broker client error: {error}", err=True
                ),
            )
            monitor_stop = threading.Event()
            monitor_errors: list[Exception] = []
            monitor = threading.Thread(
                target=_monitor_job,
                args=(
                    scheduler,
                    job_id,
                    broker,
                    monitor_stop,
                    job_finished,
                    monitor_errors,
                ),
                daemon=True,
                name="ezhpcy-job-monitor",
            )
            monitor.start()

            typer.echo(
                f"Tunnel ready for `{WORKER_HOST_ALIAS}` via worker "
                f"{worker_host}:{worker_port} (press Ctrl+C to stop)."
            )
            typer.echo(
                f"Connect with `ssh {WORKER_HOST_ALIAS}` or select "
                f"`{WORKER_HOST_ALIAS}` in VS Code Remote-SSH."
            )
            previous_sigbreak_handler = None
            if hasattr(signal, "SIGBREAK"):
                previous_sigbreak_handler = signal.signal(
                    signal.SIGBREAK, signal.default_int_handler
                )
            try:
                broker.serve_forever()
            except KeyboardInterrupt:
                typer.echo("Stopping compute-node tunnel...", err=True)
            finally:
                monitor_stop.set()
                broker.close()
                monitor.join(timeout=1)
                if previous_sigbreak_handler is not None:
                    signal.signal(signal.SIGBREAK, previous_sigbreak_handler)

            if monitor_errors:
                raise ComputeTunnelError(
                    f"could not monitor worker job {job_id}: {monitor_errors[0]}"
                )
        finally:
            if not job_finished.is_set():
                try:
                    scheduler.cancel(job_id)
                    typer.echo(f"Cancelled worker job {job_id}.", err=True)
                except SchedulerError as error:
                    typer.echo(
                        f"Warning: could not cancel worker job {job_id}: {error}",
                        err=True,
                    )
            output_stop.set()
            interactive_job.close()
            output_thread.join(timeout=1)


@with_connection_options
def compute_cmd(
    conn_info: ConnectionInfo,
    scheduler_type: Annotated[
        SchedulerType,
        typer.Option(
            "--scheduler",
            case_sensitive=False,
            help="Scheduler used to allocate the compute node.",
        ),
    ],
    queue: Annotated[
        str | None,
        typer.Option("--queue", "-q", help="Scheduler queue for the worker job."),
    ] = None,
    slots: Annotated[
        int | None,
        typer.Option(
            "--slots",
            "-n",
            min=1,
            help="Optional scheduler slot override.",
        ),
    ] = None,
    time_limit_minutes: Annotated[
        int | None,
        typer.Option(
            "--time-limit",
            min=1,
            help="Optional worker lifetime override in minutes.",
        ),
    ] = None,
    memory: Annotated[
        str | None,
        typer.Option(
            "--memory",
            metavar="SIZE",
            help="Optional total worker memory, such as 2048MB or 256GB.",
        ),
    ] = None,
    queue_timeout_seconds: Annotated[
        float,
        typer.Option(
            "--queue-timeout",
            min=1,
            help="Maximum time to wait for the scheduler allocation.",
        ),
    ] = config.hpc.queue_timeout_seconds,
    startup_timeout_seconds: Annotated[
        float,
        typer.Option(
            "--startup-timeout",
            min=1,
            help="Maximum time to wait for worker SSH after allocation.",
        ),
    ] = config.hpc.worker_startup_timeout_seconds,
    worker_port: Annotated[
        int | None,
        typer.Option(
            "--worker-port",
            min=1024,
            max=65535,
            help="Worker SSH port; defaults to a random dynamic port.",
        ),
    ] = None,
) -> None:
    """Allocate a compute node and expose its SSH service through the broker."""
    local_machine_or_fail()
    try:
        try:
            memory_mb = _parse_memory_mb(memory)
        except ValueError as error:
            raise typer.BadParameter(str(error), param_hint="--memory") from error
        _ensure_local_worker_credentials()
        _run_compute_tunnel(
            conn_info=conn_info,
            scheduler_type=scheduler_type,
            queue=queue,
            slots=slots,
            time_limit_minutes=time_limit_minutes,
            memory_mb=memory_mb,
            queue_timeout_seconds=queue_timeout_seconds,
            startup_timeout_seconds=startup_timeout_seconds,
            worker_port=worker_port or _select_worker_port(),
        )
    except KeyboardInterrupt:
        typer.echo("Compute-node tunnel stopped.", err=True)
    except (IPCError, RuntimeError, paramiko.SSHException) as error:
        typer.echo(f"Could not start compute-node tunnel: {error}", err=True)
        raise typer.Exit(code=1) from error
