import logging
import queue as queue_module
import secrets
import shlex
import signal
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Annotated

import paramiko
import typer
from pydantic import ValidationError

from ezhpcy.cli.common import (
    ProfileContext,
    with_profile_options,
)
from ezhpcy.cli.provision import (
    provision_worker_infrastructure,
    validate_worker_infrastructure,
)
from ezhpcy.cli.utils.ssh import (
    SSHD_PORT_MARKER,
    InteractiveSSHClient,
    read_ed25519_public_key,
    retrying_sshd_command,
    sshd_config_arguments,
)
from ezhpcy.config import ConnectionInfo, config
from ezhpcy.constants import (
    OPENSSH_MATCHSPEC,
    SSH_DIRECTORY_NAME,
    WORKER_CLIENT_KEY_NAME,
    WORKER_HOST_ALIAS,
    WORKER_HOST_KEY_NAME,
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
from ezhpcy.types import RemoteState, ResolvedConfig, ResolvedProfileConfig
from ezhpcy.utils import local_machine_id, ssh_connection_id

_FIRST_DYNAMIC_PORT = 49152
_LAST_DYNAMIC_PORT = 65535
_DEFAULT_WORKER_PORT_RETRIES = 5
_JOB_POLL_INTERVAL = 1.0
_JOB_MONITOR_INTERVAL = 5.0
_SSH_BANNER_LIMIT = 255
logger = logging.getLogger(__name__)


class ComputeTunnelError(RuntimeError):
    pass


def _worker_sshd_command(
    remote_state: RemoteState,
    *,
    host_key: PurePosixPath,
    remote_username: str,
    authorized_key: tuple[str, str],
    ports: tuple[int, ...],
) -> tuple[str, ...]:
    sshd_arguments = [
        *sshd_config_arguments(
            host_key=host_key,
            remote_username=remote_username,
            authorized_key=authorized_key,
        ),
    ]
    return (
        "env",
        f"PIXI_HOME={remote_state.pixi_home()}",
        f"PIXI_CACHE_DIR={remote_state.pixi_cache_dir()}",
        str(remote_state.pixi_executable()),
        "exec",
        f"--spec={OPENSSH_MATCHSPEC}",
        *retrying_sshd_command(sshd_arguments, ports),
    )


def _select_worker_ports(count: int) -> tuple[int, ...]:
    if count < 1:
        raise ValueError("worker SSH port count must be positive")
    ports: list[int] = []
    while len(ports) < count:
        port = _FIRST_DYNAMIC_PORT + secrets.randbelow(
            _LAST_DYNAMIC_PORT - _FIRST_DYNAMIC_PORT + 1
        )
        if port not in ports:
            ports.append(port)
    return tuple(ports)


def _ensure_local_worker_credentials(conn_info: ConnectionInfo) -> None:
    remote_username = conn_info.user
    assert remote_username is not None
    ssh_directory = config.local_file.ssh_dir(
        local_machine_id(), user=remote_username, host=str(conn_info.host)
    )
    required_files = (
        ssh_directory / WORKER_CLIENT_KEY_NAME,
        ssh_directory / f"{WORKER_CLIENT_KEY_NAME}.pub",
        ssh_directory / "worker_known_hosts",
    )
    missing = [str(path) for path in required_files if not path.is_file()]
    if missing:
        raise ComputeTunnelError(
            "worker SSH credentials are missing; run `ezhpcy provision` first "
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
            logger.error("Could not inspect worker job %s: %s", job_id, error)
            broker.close()
            return

        if info.state.is_terminal or info.state is JobState.UNKNOWN:
            job_finished.set()
            logger.info(
                "Worker job %s ended in scheduler state %s.", job_id, info.raw_state
            )
            broker.close()
            return


def _drain_interactive_job(
    job: InteractiveJob,
    stop_requested: threading.Event,
    selected_ports: queue_module.Queue[int] | None = None,
) -> None:
    pending = bytearray()

    def handle_output(output: bytes) -> None:
        if not output:
            return
        typer.echo(output.decode(errors="replace"), nl=False, err=True)
        if selected_ports is None:
            return
        pending.extend(output)
        while b"\n" in pending:
            raw_line, _, remainder = pending.partition(b"\n")
            pending[:] = remainder
            line = raw_line.decode(errors="replace").strip()
            if line.startswith(SSHD_PORT_MARKER):
                raw_port = line.removeprefix(SSHD_PORT_MARKER)
                try:
                    selected_ports.put_nowait(int(raw_port))
                except ValueError:
                    logger.warning("Worker reported an invalid SSH port: %s", raw_port)

    while not stop_requested.wait(0.05):
        output = job.read_available()
        handle_output(output)
        if job.process.exit_status_ready():
            handle_output(job.read_available())
            return


def _wait_for_selected_worker_port(
    job: InteractiveJob,
    selected_ports: queue_module.Queue[int],
    *,
    timeout_seconds: float,
) -> int:
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ComputeTunnelError(
                "worker SSH daemon did not bind a candidate port within "
                f"{timeout_seconds:g} seconds"
            )
        try:
            return selected_ports.get(timeout=min(0.1, remaining))
        except queue_module.Empty:
            if job.process.exit_status_ready():
                raise ComputeTunnelError(
                    "worker SSH daemon failed to bind any candidate port"
                )


def _run_compute_tunnel(
    *,
    profile_name: str,
    profile: ResolvedProfileConfig,
    conn_info: ConnectionInfo,
    scheduler_type: SchedulerType,
    queue: str | None,
    cores: int,
    gpus: int,
    exclusive: bool,
    time_limit: timedelta | None,
    memory_bytes: int | None,
    queue_timeout_seconds: float,
    startup_timeout_seconds: float,
    worker_ports: tuple[int, ...],
    auto_provision: bool,
) -> None:
    machine_id = local_machine_id()
    with InteractiveSSHClient(conn_info) as ssh:
        ssh.interactive_connect()
        transport = ssh.get_transport()
        if transport is None or not transport.is_active():
            raise paramiko.SSHException("Login-node SSH session is not active")

        remote_state = ssh.get_remote_state()
        remote_username = conn_info.user
        assert remote_username is not None
        remote_host = str(conn_info.host)
        connection_id = ssh_connection_id(remote_username, remote_host)
        if auto_provision:
            provision_worker_infrastructure(
                ssh,
                remote_state,
                remote_username=remote_username,
                remote_host=remote_host,
                machine_id=machine_id,
            )
        else:
            validate_worker_infrastructure(
                ssh,
                remote_state,
                remote_username=remote_username,
                remote_host=remote_host,
                machine_id=machine_id,
            )
        client_public_key = (
            config.local_file.ssh_dir(
                machine_id, user=remote_username, host=remote_host
            )
            / f"{WORKER_CLIENT_KEY_NAME}.pub"
        )
        key_type, key_blob = read_ed25519_public_key(client_public_key)
        match scheduler_type:
            case SchedulerType.LSF:
                scheduler: Scheduler = LSFScheduler(
                    ssh.run_login_shell,
                    ssh.start_login_shell,
                    interactive_application_profile=(profile.lsf_application_profile),
                    interactive_submission_environment=(
                        profile.lsf_submission_environment
                    ),
                    interactive_export_environment=(profile.lsf_export_environment),
                    resource_reserve_per_task=(profile.lsf_resource_reserve_per_task),
                )
            case SchedulerType.PBS:
                scheduler = PBSScheduler(
                    ssh.run_login_shell,
                    ssh.start_login_shell,
                    command_directory=profile.pbs_command_directory,
                )
            case _:
                raise UnsupportedSchedulerError(scheduler_type)
        logger.info(
            "Using profile %r with %s scheduler.", profile_name, scheduler_type.value
        )
        logger.debug(
            "Worker request: queue=%r cores=%d gpus=%d exclusive=%s "
            "time_limit=%s memory_bytes=%s ports=%s",
            queue,
            cores,
            gpus,
            exclusive,
            time_limit,
            memory_bytes,
            worker_ports,
        )
        worker_cwd_dir = remote_state.worker_cwd_dir()
        worker_logs_dir = remote_state.worker_logs_dir()
        remote_host_key = (
            remote_state.package_cache_dir()
            / SSH_DIRECTORY_NAME
            / machine_id
            / connection_id
            / WORKER_HOST_KEY_NAME
        )

        with ssh.sftp_client() as sftp:
            sftp.mkdir(worker_cwd_dir, parents=True, exist_ok=True)
            sftp.mkdir(worker_logs_dir, parents=True, exist_ok=True)

        spec = JobSpec(
            command=_worker_sshd_command(
                remote_state,
                host_key=remote_host_key,
                remote_username=remote_username,
                authorized_key=(key_type, key_blob),
                ports=worker_ports,
            ),
            name="ezhpcy-worker",
            time_limit=time_limit,
            memory_bytes=memory_bytes,
            cores=cores,
            gpus=gpus,
            exclusive=exclusive,
            queue=queue,
            working_directory=worker_cwd_dir,
            stdout_path=worker_logs_dir / "worker-%J.out",
            stderr_path=worker_logs_dir / "worker-%J.err",
        )

        interactive_job = scheduler.submit_interactive(
            spec,
            startup_timeout=startup_timeout_seconds,
        )
        job_id = interactive_job.job_id
        logger.info("Submitted worker job %s.", job_id)
        if interactive_job.submission_command:
            logger.debug(
                "Submission command: %s",
                shlex.join(interactive_job.submission_command),
            )
        if scheduler_type is SchedulerType.LSF:
            logger.info(
                "Worker logs: %s and %s.",
                str(spec.stdout_path).replace("%J", job_id),
                str(spec.stderr_path).replace("%J", job_id),
            )
        job_finished = threading.Event()
        output_stop = threading.Event()
        selected_ports: queue_module.Queue[int] = queue_module.Queue()
        output_thread = threading.Thread(
            target=_drain_interactive_job,
            args=(interactive_job, output_stop, selected_ports),
            daemon=True,
            name="ezhpcy-compute-job-output",
        )
        output_thread.start()
        try:
            info = _wait_for_running_job(
                scheduler,
                job_id,
                timeout_seconds=queue_timeout_seconds,
                state_handler=lambda snapshot: logger.info(
                    "Worker job %s: %s (%s).",
                    job_id,
                    snapshot.state.value,
                    snapshot.raw_state,
                ),
            )
            worker_host = info.primary_host
            if worker_host is None:
                raise ComputeTunnelError(
                    f"scheduler did not report a host for running job {job_id}"
                )
            interactive_job.start_command()
            worker_port = _wait_for_selected_worker_port(
                interactive_job,
                selected_ports,
                timeout_seconds=startup_timeout_seconds,
            )
            destination = (worker_host, worker_port)
            logger.info(
                "Waiting for worker SSH endpoint %s:%d.", worker_host, worker_port
            )
            _wait_for_worker_endpoint(
                transport,
                destination,
                timeout_seconds=startup_timeout_seconds,
            )

            backend = create_broker_backend(profile=profile_name)
            broker = ForegroundBroker(
                transport,
                destination,
                backend,
                error_handler=lambda error: logger.error(
                    "Broker client error: %s", error
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

            logger.info(
                "Tunnel ready for %s via worker %s:%d (press Ctrl+C to stop).",
                WORKER_HOST_ALIAS,
                worker_host,
                worker_port,
            )
            logger.info(
                "Connect with `ssh %s` or select `%s` in VS Code Remote-SSH.",
                WORKER_HOST_ALIAS,
                WORKER_HOST_ALIAS,
            )
            previous_sigbreak_handler = None
            if hasattr(signal, "SIGBREAK"):
                previous_sigbreak_handler = signal.signal(
                    signal.SIGBREAK, signal.default_int_handler
                )
            try:
                broker.serve_forever()
            except KeyboardInterrupt:
                logger.info("Stopping compute-node tunnel...")
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
                    logger.info("Cancelled worker job %s.", job_id)
                except SchedulerError as error:
                    logger.warning("Could not cancel worker job %s: %s", job_id, error)
            output_stop.set()
            interactive_job.close()
            output_thread.join(timeout=1)


@with_profile_options
def compute_cmd(
    profile_context: ProfileContext,
    scheduler_type: Annotated[
        SchedulerType | None,
        typer.Option(
            "--scheduler",
            case_sensitive=False,
            help="Scheduler used to allocate the compute node.",
        ),
    ] = None,
    queue: Annotated[
        str | None,
        typer.Option("--queue", "-q", help="Scheduler queue for the worker job."),
    ] = None,
    cores: Annotated[
        int | None,
        typer.Option(
            "--cores",
            "-n",
            min=1,
            help="Scheduler CPU cores reserved on the worker host.",
        ),
    ] = None,
    gpus: Annotated[
        int | None,
        typer.Option(
            "--gpus",
            min=0,
            help="Number of GPUs reserved on the worker host.",
        ),
    ] = None,
    exclusive: Annotated[
        bool | None,
        typer.Option(
            "--exclusive/--shared",
            help="Reserve the worker host exclusively.",
        ),
    ] = None,
    time_limit: Annotated[
        str | None,
        typer.Option(
            "--time-limit",
            metavar="H:MM",
            help="Optional worker lifetime override.",
        ),
    ] = None,
    memory: Annotated[
        str | None,
        typer.Option(
            "--memory",
            metavar="SIZE",
            help=(
                "Optional total worker memory parsed as a Pydantic byte size. "
                "Bare values are bytes; SI (GB) and IEC (GiB) units differ."
            ),
        ),
    ] = None,
    queue_timeout_seconds: Annotated[
        float | None,
        typer.Option(
            "--queue-timeout",
            min=1,
            help="Maximum time to wait for the scheduler allocation.",
        ),
    ] = None,
    startup_timeout_seconds: Annotated[
        float | None,
        typer.Option(
            "--startup-timeout",
            min=1,
            help="Maximum time to wait for worker SSH after allocation.",
        ),
    ] = None,
    worker_port: Annotated[
        int | None,
        typer.Option(
            "--worker-port",
            min=1024,
            max=65535,
            help="Worker SSH port; defaults to a random dynamic port.",
        ),
    ] = None,
    worker_port_retries: Annotated[
        int,
        typer.Option(
            "--worker-port-retries",
            min=0,
            help=(
                "Alternate random worker SSH ports to try after a bind failure; "
                "ignored with --worker-port."
            ),
        ),
    ] = _DEFAULT_WORKER_PORT_RETRIES,
    auto_provision: Annotated[
        bool,
        typer.Option(
            "--auto-provision",
            help="Provision or repair worker infrastructure before submission.",
        ),
    ] = False,
    no_auto_provision: Annotated[
        bool,
        typer.Option(
            "--no-auto-provision",
            help="Do not provision or repair worker infrastructure before submission.",
        ),
    ] = False,
) -> None:
    """Allocate a compute node and expose its SSH service through the broker."""
    try:
        profile = profile_context.profile
        try:
            resolved = ResolvedConfig.model_validate(
                {
                    **profile.model_dump(),
                    "scheduler": scheduler_type or profile.scheduler,
                    "queue": queue if queue is not None else profile.queue,
                    "cores": cores if cores is not None else profile.cores,
                    "gpus": gpus if gpus is not None else profile.gpus,
                    "exclusive": (
                        exclusive if exclusive is not None else profile.exclusive
                    ),
                    "time_limit": (
                        time_limit if time_limit is not None else profile.time_limit
                    ),
                    "memory": memory if memory is not None else profile.memory,
                    "queue_timeout_seconds": (
                        queue_timeout_seconds
                        if queue_timeout_seconds is not None
                        else profile.queue_timeout_seconds
                    ),
                    "worker_startup_timeout_seconds": (
                        startup_timeout_seconds
                        if startup_timeout_seconds is not None
                        else profile.worker_startup_timeout_seconds
                    ),
                }
            )
        except ValidationError as error:
            raise typer.BadParameter(str(error)) from error
        if resolved.scheduler is None:
            raise typer.BadParameter(
                "scheduler must be set by --scheduler or the selected profile",
                param_hint="--scheduler",
            )
        if auto_provision and no_auto_provision:
            raise typer.BadParameter(
                "--auto-provision and --no-auto-provision cannot be used together",
                param_hint="--auto-provision/--no-auto-provision",
            )
        if auto_provision:
            auto_provision_enabled = True
        elif no_auto_provision:
            auto_provision_enabled = False
        else:
            auto_provision_enabled = config.auto_provision
        if not auto_provision_enabled:
            _ensure_local_worker_credentials(profile_context.connection)
        worker_ports = (
            (worker_port,)
            if worker_port is not None
            else _select_worker_ports(worker_port_retries + 1)
        )
        _run_compute_tunnel(
            profile_name=profile_context.name,
            profile=resolved,
            conn_info=profile_context.connection,
            scheduler_type=resolved.scheduler,
            queue=resolved.queue,
            cores=resolved.cores,
            gpus=resolved.gpus,
            exclusive=resolved.exclusive,
            time_limit=resolved.time_limit_delta,
            memory_bytes=int(resolved.memory) if resolved.memory is not None else None,
            queue_timeout_seconds=resolved.queue_timeout_seconds,
            startup_timeout_seconds=resolved.worker_startup_timeout_seconds,
            worker_ports=worker_ports,
            auto_provision=auto_provision_enabled,
        )
    except KeyboardInterrupt:
        logger.info("Compute-node tunnel stopped.")
    except (IPCError, RuntimeError, paramiko.SSHException) as error:
        logger.error(
            "Could not start compute-node tunnel: %s",
            error,
            exc_info=logger.isEnabledFor(logging.DEBUG),
        )
        raise typer.Exit(code=1) from error
