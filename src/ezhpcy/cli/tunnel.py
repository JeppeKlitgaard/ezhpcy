import logging
import math
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

from ezhpcy.cli.common import (
    AliasOpt,
    CoresOpt,
    ExclusiveOpt,
    GpusOpt,
    MemoryOpt,
    OptionalProfileArg,
    QueueOpt,
    TimeLimitOpt,
    resolve_profile_config,
    with_connection,
    with_resources,
    with_scheduler,
    with_timings,
)
from ezhpcy.cli.doctor import echo_include_directive
from ezhpcy.cli.utils.bad_parameter import RichBadParameter
from ezhpcy.cli.utils.ssh import InteractiveSSHClient
from ezhpcy.config import config
from ezhpcy.constants import (
    OPENSSH_MATCHSPEC,
    SSH_DIRECTORY_NAME,
    WORKER_CLIENT_KEY_NAME,
    WORKER_HOST_KEY_NAME,
)
from ezhpcy.ipc import create_tunnel_backend
from ezhpcy.ipc.common import IPCError
from ezhpcy.permissions import FilePermissionError
from ezhpcy.provision_host import (
    provision_worker_infrastructure,
    validate_worker_infrastructure,
)
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
from ezhpcy.ssh import SFTPClient
from ezhpcy.tunnel.server import TunnelServer
from ezhpcy.tunnel.ssh_config import (
    WorkerHost,
    check_host_resolution,
    profile_hosts,
    prune_stale_active_configs,
    remove_active_host_config,
    write_active_host_config,
    write_profiles_config,
)
from ezhpcy.tunnel.sshd import (
    read_ed25519_public_key,
    retrying_sshd_command,
    retrying_sshd_script,
    sshd_config_arguments,
)
from ezhpcy.types import (
    ConnectionInfo,
    RemoteState,
    ResolvedConfig,
    ResourcesConfig,
    SchedulerConfig,
    SubmissionMode,
    TimingsConfig,
)
from ezhpcy.utils import local_machine_id, ssh_connection_id

_FIRST_DYNAMIC_PORT = 49152
_LAST_DYNAMIC_PORT = 65535
_DEFAULT_WORKER_PORT_RETRIES = 5
_SSH_BANNER_LIMIT = 255
_LEASE_FILE_RETAIN_COUNT = 2
logger = logging.getLogger(__name__)


class TunnelError(RuntimeError):
    pass


def _worker_sshd_command(
    remote_state: RemoteState,
    *,
    ports: tuple[int, ...],
    heartbeat_token: str,
    heartbeat_timeout_seconds: float,
    heartbeat_debug: bool,
    control_dir: PurePosixPath,
    read_script_from_stdin: bool,
    sshd_arguments: list[str],
) -> tuple[str, ...]:
    return (
        "env",
        f"PIXI_HOME={remote_state.pixi_home()}",
        f"PIXI_CACHE_DIR={remote_state.pixi_cache_dir()}",
        str(remote_state.pixi_executable()),
        "exec",
        f"--spec={OPENSSH_MATCHSPEC}",
        *retrying_sshd_command(
            sshd_arguments,
            ports,
            heartbeat_token=heartbeat_token,
            heartbeat_timeout_seconds=math.ceil(heartbeat_timeout_seconds),
            heartbeat_debug=heartbeat_debug,
            heartbeat_log_dir=remote_state.worker_logs_dir(),
            control_dir=control_dir,
            read_script_from_stdin=read_script_from_stdin,
        ),
    )


def _worker_client_alive(
    heartbeat_interval_seconds: float, heartbeat_timeout_seconds: float
) -> tuple[int, int]:
    """Bound how long the worker serves an SSH client that stopped answering.

    A connection that was open when the login session died must end even if
    the lease watchdog cannot signal its handler. The probes reuse the
    heartbeat cadence and give up within the heartbeat timeout.
    """
    interval_seconds = max(1, math.ceil(heartbeat_interval_seconds))
    count_max = max(1, math.floor(heartbeat_timeout_seconds / interval_seconds))
    return interval_seconds, count_max


def _worker_job_script(command: tuple[str, ...], worker_script: str) -> str:
    """Wrap the worker shell program in a batch script fed to LSF over stdin."""
    delimiter = "EZHPCY_WORKER_SCRIPT"
    if f"\n{delimiter}\n" in worker_script:
        raise ValueError("worker script conflicts with its here-document delimiter")
    return (
        "#!/usr/bin/env bash\n"
        f"exec {shlex.join(command)} <<'{delimiter}'\n"
        f"{worker_script.rstrip()}\n"
        f"{delimiter}\n"
    )


class WorkerControl:
    """A bounded, filesystem-backed control mailbox for one worker job."""

    def __init__(
        self,
        sftp: SFTPClient,
        directory: PurePosixPath,
        token: str,
        *,
        ports: tuple[int, ...],
    ) -> None:
        self._sftp = sftp
        self.directory = directory
        self._token = token
        self._ports = frozenset(ports)
        self._sequence = 0
        self._leases: list[PurePosixPath] = []
        self._lock = threading.Lock()

    def create(self) -> None:
        with self._lock:
            self._sftp.mkdir(self.directory, mode=0o700, parents=True)

    def publish_lease(self) -> int:
        with self._lock:
            self._sequence += 1
            filename = f"lease.{self._sequence:020d}"
            temporary = self.directory / f".{filename}.tmp"
            lease = self.directory / filename
            try:
                self._sftp.write_text(temporary, f"{self._token} {self._sequence}\n")
                self._sftp.chmod(str(temporary), 0o600)
                self._sftp.posix_rename(str(temporary), str(lease))
            except Exception:
                try:
                    self._sftp.remove(str(temporary))
                except OSError:
                    pass
                raise
            self._leases.append(lease)
            while len(self._leases) > _LEASE_FILE_RETAIN_COUNT:
                stale = self._leases.pop(0)
                try:
                    self._sftp.remove(str(stale))
                except OSError as error:
                    logger.debug(
                        "Could not remove stale worker lease %s: %r", stale, error
                    )
            return self._sequence

    def read_ready_port(self) -> int | None:
        with self._lock:
            try:
                value = self._sftp.read_text(self.directory / "ready").strip()
            except OSError:
                return None
        fields = value.split()
        if len(fields) != 4 or fields[:3] != ["v1", self._token, "ready"]:
            raise TunnelError("worker wrote an invalid ready record")
        try:
            port = int(fields[3])
        except ValueError as error:
            raise TunnelError("worker reported a non-numeric SSH port") from error
        if port not in self._ports:
            raise TunnelError(f"worker reported unexpected SSH port {port}")
        return port

    def read_failure(self) -> str | None:
        with self._lock:
            try:
                value = self._sftp.read_text(self.directory / "failed").strip()
            except OSError:
                return None
        fields = value.split()
        if len(fields) < 3 or fields[:3] != ["v1", self._token, "failed"]:
            return "worker wrote an invalid failure record"
        return " ".join(fields[3:]) or "worker startup failed"

    def close(self) -> None:
        with self._lock:
            for path in (
                *self._leases,
                self.directory / "ready",
                self.directory / "failed",
                self.directory / "stopped",
            ):
                try:
                    self._sftp.remove(str(path))
                except OSError:
                    pass
            try:
                self._sftp.rmdir(str(self.directory))
            except OSError:
                pass


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
        raise TunnelError(
            "worker SSH credentials are missing; run `ezhpcy provision` first "
            f"(missing: {', '.join(missing)})"
        )


def _wait_for_running_job(
    scheduler: Scheduler,
    job_id: str,
    *,
    timeout_seconds: float,
    poll_interval: float,
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
            raise TunnelError(
                f"worker job {job_id} ended in scheduler state "
                f"{info.raw_state}{exit_detail}"
            )
        if info.state is JobState.UNKNOWN:
            raise TunnelError(
                f"worker job {job_id} entered unknown scheduler state {info.raw_state}"
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TunnelError(
                f"worker job {job_id} did not start within {timeout_seconds:g} seconds"
            )
        time.sleep(min(poll_interval, remaining))


def _wait_for_worker_endpoint(
    transport: paramiko.Transport,
    destination: tuple[str, int],
    *,
    timeout_seconds: float,
    poll_interval: float,
    failure_check: Callable[[], None] | None = None,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    transport_logger = logging.getLogger("paramiko.transport")
    previous_log_level = transport_logger.level
    transport_logger.setLevel(logging.CRITICAL + 1)

    try:
        while True:
            if failure_check is not None:
                failure_check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                detail = f": {last_error}" if last_error is not None else ""
                raise TunnelError(
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
                last_error = TunnelError(f"unexpected worker banner {bytes(banner)!r}")
                logger.debug(
                    "Worker readiness returned an unexpected banner: "
                    "destination=%s:%d banner=%r",
                    destination[0],
                    destination[1],
                    bytes(banner),
                )
            except (EOFError, OSError, paramiko.SSHException) as error:
                last_error = error
                logger.debug(
                    "Worker readiness attempt failed: destination=%s:%d "
                    "transport_active=%s error=%r",
                    destination[0],
                    destination[1],
                    transport.is_active(),
                    error,
                )
            finally:
                if channel is not None:
                    channel.close()

            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(poll_interval, remaining))
    finally:
        transport_logger.setLevel(previous_log_level)


def _send_worker_lease_heartbeats(
    control: WorkerControl,
    transport: paramiko.Transport,
    *,
    job_id: str,
    interval_seconds: float,
    stop_requested: threading.Event,
    failed: threading.Event,
    errors: list[TunnelError],
    failure_handler: Callable[[], None],
) -> None:
    """Renew the filesystem lease until shutdown or the login transport fails."""
    sequence = 0
    logger.debug(
        "Worker heartbeat sender started: job=%s interval_seconds=%g",
        job_id,
        interval_seconds,
    )
    while not stop_requested.is_set():
        started = time.monotonic()
        try:
            sequence = control.publish_lease()
        except Exception as error:
            if stop_requested.is_set():
                break
            transport_error = transport.get_exception()
            heartbeat_error = TunnelError(
                f"worker heartbeat write failed for job {job_id} at "
                f"sequence {sequence}: {error}"
            )
            errors.append(heartbeat_error)
            failed.set()
            logger.error(
                "Worker heartbeat send failed, stopping the tunnel: job=%s "
                "sequence=%d elapsed_seconds=%.3f transport_active=%s "
                "transport_error=%r error=%r",
                job_id,
                sequence,
                time.monotonic() - started,
                transport.is_active(),
                transport_error,
                error,
                exc_info=logger.isEnabledFor(logging.DEBUG),
            )
            failure_handler()
            return
        elapsed = time.monotonic() - started
        logger.debug(
            "Worker heartbeat published: job=%s sequence=%d elapsed_seconds=%.3f "
            "transport_active=%s",
            job_id,
            sequence,
            elapsed,
            transport.is_active(),
        )
        if elapsed > interval_seconds:
            logger.warning(
                "Worker heartbeat is slower than its interval: job=%s sequence=%d "
                "elapsed_seconds=%.3f interval_seconds=%g",
                job_id,
                sequence,
                elapsed,
                interval_seconds,
            )
        if stop_requested.wait(interval_seconds):
            break
    logger.debug(
        "Worker heartbeat sender stopped: job=%s last_sequence=%d",
        job_id,
        sequence,
    )


def _monitor_scheduler_job(
    scheduler: Scheduler,
    job_id: str,
    tunnel: TunnelServer,
    stop_requested: threading.Event,
    job_finished: threading.Event,
    errors: list[TunnelError],
    monitor_interval: float,
) -> None:
    """Close the tunnel when the scheduler reports that its job has ended."""
    polls = 0
    while not stop_requested.wait(monitor_interval):
        polls += 1
        try:
            info = scheduler.inspect(job_id)
        except SchedulerError as error:
            # Single-strike teardown: record everything needed to tell a real
            # job failure apart from a transient scheduler-query failure.
            logger.error(
                "Job monitor stopping the tunnel after a scheduler query failure: "
                "job=%s poll=%d error=%r",
                job_id,
                polls,
                error,
                exc_info=logger.isEnabledFor(logging.DEBUG),
            )
            errors.append(
                TunnelError(f"could not monitor worker job {job_id}: {error}")
            )
            tunnel.close()
            return
        logger.debug(
            "Job monitor poll: job=%s poll=%d state=%s raw_state=%s hosts=%s "
            "exit_code=%s",
            job_id,
            polls,
            info.state.name,
            info.raw_state,
            info.execution_hosts,
            info.exit_code,
        )
        if info.state.is_terminal:
            job_finished.set()
            logger.info(
                "Worker job %s ended in scheduler state %s.", job_id, info.raw_state
            )
            tunnel.close()
            return
        if info.state is JobState.UNKNOWN:
            logger.error(
                "Job monitor stopping the tunnel after an unknown scheduler state: "
                "job=%s poll=%d raw_state=%s. A scheduler that reports this state "
                "transiently will still end the tunnel here.",
                job_id,
                polls,
                info.raw_state,
            )
            errors.append(
                TunnelError(
                    f"worker job {job_id} entered unknown scheduler state {info.raw_state}"
                )
            )
            tunnel.close()
            return


def _raise_heartbeat_failure(errors: list[TunnelError]) -> None:
    if errors:
        raise errors[0]


def _drain_interactive_job(
    job: InteractiveJob, stop_requested: threading.Event
) -> None:
    """Forward diagnostic output from an interactive scheduler shell."""
    while not stop_requested.wait(0.05):
        output = job.read_available()
        if output:
            typer.echo(output.decode(errors="replace"), nl=False, err=True)
        if job.process.exit_status_ready():
            output = job.read_available()
            if output:
                typer.echo(output.decode(errors="replace"), nl=False, err=True)
            return


def _wait_for_selected_worker_port(
    control: WorkerControl,
    *,
    timeout_seconds: float,
) -> int:
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TunnelError(
                "worker SSH daemon did not bind a candidate port within "
                f"{timeout_seconds:g} seconds"
            )
        port = control.read_ready_port()
        if port is not None:
            return port
        if failure := control.read_failure():
            raise TunnelError(f"worker SSH daemon failed to start: {failure}")
        time.sleep(min(0.1, remaining))


def _publish_ssh_host(
    host: WorkerHost, *, instance_id: str, in_profiles_config: bool
) -> None:
    """Expose a running tunnel's Host block and check OpenSSH resolves it."""
    try:
        # Pruning also drops an older tunnel's block for this alias, which
        # would otherwise shadow the identical one in profiles.conf.
        prune_stale_active_configs()
        if not in_profiles_config:
            write_active_host_config(host, instance_id=instance_id)
    except (OSError, FilePermissionError) as error:
        logger.warning(
            "Could not write the SSH configuration for %s: %s", host.alias, error
        )
        return
    problem = check_host_resolution(host)
    if problem is not None:
        logger.warning(
            "`ssh %s` will not reach this tunnel: %s. If ~/.ssh/config does not "
            "include EzHPCy's SSH profiles yet, copy the two highlighted lines "
            "below to its top, before any Host or Match block:",
            host.alias,
            problem,
        )
        typer.echo(err=True)
        echo_include_directive(err=True)


def _withdraw_ssh_host(host: WorkerHost, *, instance_id: str) -> None:
    try:
        remove_active_host_config(host.alias, instance_id=instance_id)
    except OSError as error:
        logger.warning(
            "Could not remove the SSH configuration for %s: %s", host.alias, error
        )


def _resolve_ssh_host(
    alias: str | None,
    *,
    profile_name: str | None,
    resolved: ResolvedConfig,
) -> WorkerHost:
    """Pick the Host alias this tunnel serves; profiles default to their name."""
    if alias is None:
        # Identical anonymous options yield the same alias across restarts, so
        # saved hosts (e.g. in VS Code) keep working.
        alias = profile_name or f"ezhpcy-{resolved.descriptor_digest()[:12]}"
    if alias != profile_name and alias in config.profile:
        raise RichBadParameter(
            f"alias [bold red]{alias}[/bold red] is the name of profile "
            f"[bold blue]{alias}[/bold blue]; choose another alias",
            param_hint="--alias",
        )
    try:
        return WorkerHost.for_endpoint(
            alias, user=resolved.connection.user, host=str(resolved.connection.host)
        )
    except ValueError as error:
        raise RichBadParameter(str(error), param_hint="--alias") from error


def _run_tunnel(
    *,
    profile_name: str | None,
    profile: ResolvedConfig,
    ssh_host: WorkerHost,
    ssh_host_in_profiles_config: bool,
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
    job_poll_interval_seconds: float,
    job_monitor_interval_seconds: float,
    heartbeat_interval_seconds: float = 30,
    heartbeat_timeout_seconds: float = 90,
    worker_ports: tuple[int, ...],
    auto_provision: bool,
    submission_mode: SubmissionMode,
) -> None:
    machine_id = local_machine_id()
    with InteractiveSSHClient(
        conn_info,
        password_prompt=conn_info.password_prompt,
    ) as ssh:
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
                    ssh.run_login_shell_with_input,
                    interactive_application_profile=profile.lsf.application_profile,
                    interactive_submission_command=(
                        profile.scheduler.interactive_submission_command
                    ),
                    interactive_submission_environment=(
                        profile.lsf.submission_environment
                    ),
                    interactive_export_environment=profile.lsf.export_environment,
                    resource_reserve_per_task=profile.lsf.resource_reserve_per_task,
                )
            case SchedulerType.PBS:
                scheduler = PBSScheduler(
                    ssh.run_login_shell,
                    ssh.start_login_shell,
                    command_directory=profile.pbs.command_directory,
                    interactive_submission_command=(
                        profile.scheduler.interactive_submission_command
                    ),
                )
            case _:
                raise UnsupportedSchedulerError(scheduler_type)
        if profile_name is None:
            logger.info(
                "Using anonymous configuration with %s scheduler.", scheduler_type.value
            )
        else:
            logger.info(
                "Using profile %r with %s scheduler.",
                profile_name,
                scheduler_type.value,
            )
        logger.debug(
            "Worker request: queue=%r cores=%d gpus=%d exclusive=%s "
            "time_limit=%s memory_bytes=%s ports=%s heartbeat_interval=%g "
            "heartbeat_timeout=%g",
            queue,
            cores,
            gpus,
            exclusive,
            time_limit,
            memory_bytes,
            worker_ports,
            heartbeat_interval_seconds,
            heartbeat_timeout_seconds,
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

        heartbeat_token = secrets.token_hex(16)
        control_dir = (
            remote_state.package_cache_dir() / "worker_control" / heartbeat_token
        )
        with ssh.sftp_client() as sftp:
            sftp.mkdir(worker_cwd_dir, parents=True, exist_ok=True)
            sftp.mkdir(worker_logs_dir, parents=True, exist_ok=True)
            sftp.mkdir(control_dir.parent, mode=0o700, parents=True, exist_ok=True)
            sftp.chmod(str(control_dir.parent), 0o700)
            control = WorkerControl(
                sftp,
                control_dir,
                heartbeat_token,
                ports=worker_ports,
            )
            stream_lsf_script = (
                scheduler_type is SchedulerType.LSF
                and submission_mode is SubmissionMode.BATCH
            )
            control.create()
            sshd_arguments = sshd_config_arguments(
                host_key=remote_host_key,
                remote_username=remote_username,
                authorized_key=(key_type, key_blob),
                client_alive=_worker_client_alive(
                    heartbeat_interval_seconds, heartbeat_timeout_seconds
                ),
            )
            control.publish_lease()
            spec = JobSpec(
                command=_worker_sshd_command(
                    remote_state,
                    ports=worker_ports,
                    heartbeat_token=heartbeat_token,
                    heartbeat_timeout_seconds=heartbeat_timeout_seconds,
                    heartbeat_debug=logger.isEnabledFor(logging.DEBUG),
                    control_dir=control_dir,
                    read_script_from_stdin=stream_lsf_script,
                    sshd_arguments=sshd_arguments,
                ),
                name="ezhpcy-worker",
                time_limit=time_limit,
                memory_bytes=memory_bytes,
                cores=cores,
                gpus=gpus,
                exclusive=exclusive,
                queue=queue,
                working_directory=worker_cwd_dir,
                stdout_path=worker_logs_dir / f"worker-{heartbeat_token}.out",
                stderr_path=worker_logs_dir / f"worker-{heartbeat_token}.err",
            )

            interactive_job: InteractiveJob | None = None
            try:
                if submission_mode is SubmissionMode.INTERACTIVE:
                    interactive_job = scheduler.submit_interactive(
                        spec,
                        startup_timeout=startup_timeout_seconds,
                    )
                    job_id = interactive_job.job_id
                elif stream_lsf_script:
                    job_id = scheduler.submit_script(
                        spec, _worker_job_script(spec.command, retrying_sshd_script())
                    )
                else:
                    job_id = scheduler.submit(spec)
                logger.info("Submitted worker job %s.", job_id)
                logger.info(
                    "Worker heartbeat enabled for job %s: interval=%g seconds, "
                    "timeout=%g seconds.",
                    job_id,
                    heartbeat_interval_seconds,
                    heartbeat_timeout_seconds,
                )
                logger.info(
                    "Worker heartbeat log: %s.",
                    worker_logs_dir / f"heartbeat-{job_id}.log",
                )
                if interactive_job is not None and interactive_job.submission_command:
                    logger.debug(
                        "Submission command: %s",
                        shlex.join(interactive_job.submission_command),
                    )
                logger.info(
                    "Worker logs: %s and %s.", spec.stdout_path, spec.stderr_path
                )
                job_finished = threading.Event()
                output_stop = threading.Event()
                heartbeat_stop = threading.Event()
                heartbeat_failed = threading.Event()
                heartbeat_errors: list[TunnelError] = []
                heartbeat_thread: threading.Thread | None = None
                tunnel: TunnelServer | None = None
                shutdown_reason = "startup_failure"
                connection_errors: list[TunnelError] = []
                output_thread: threading.Thread | None = None

                def handle_connection_lost(error: paramiko.SSHException) -> None:
                    nonlocal shutdown_reason
                    shutdown_reason = "login_connection_lost"
                    connection_errors.append(
                        TunnelError(f"login-node SSH connection lost: {error}")
                    )
                    if tunnel is not None:
                        tunnel.close()

                # The keepalive sender has already closed the transport by the
                # time this runs; stop the tunnel instead of serving a worker
                # that can no longer be reached.
                ssh.set_connection_lost_handler(handle_connection_lost)
                if interactive_job is not None:
                    output_thread = threading.Thread(
                        target=_drain_interactive_job,
                        args=(interactive_job, output_stop),
                        daemon=True,
                        name="ezhpcy-job-output",
                    )
                    output_thread.start()
                try:
                    if interactive_job is not None:
                        interactive_job.start_command()
                    shutdown_reason = "starting_worker"
                    logger.debug("Worker command queued: job=%s", job_id)
                    info = _wait_for_running_job(
                        scheduler,
                        job_id,
                        timeout_seconds=queue_timeout_seconds,
                        poll_interval=job_poll_interval_seconds,
                        state_handler=lambda snapshot: logger.info(
                            "Worker job %s: %s (%s).",
                            job_id,
                            snapshot.state.value,
                            snapshot.raw_state,
                        ),
                    )
                    worker_host = info.primary_host
                    if worker_host is None:
                        raise TunnelError(
                            f"scheduler did not report a host for running job {job_id}"
                        )
                    worker_port = _wait_for_selected_worker_port(
                        control,
                        timeout_seconds=startup_timeout_seconds,
                    )

                    def handle_heartbeat_failure() -> None:
                        nonlocal shutdown_reason
                        shutdown_reason = "heartbeat_send_failed"
                        if tunnel is not None:
                            tunnel.close()

                    heartbeat_thread = threading.Thread(
                        target=_send_worker_lease_heartbeats,
                        args=(control, transport),
                        kwargs={
                            "job_id": job_id,
                            "interval_seconds": heartbeat_interval_seconds,
                            "stop_requested": heartbeat_stop,
                            "failed": heartbeat_failed,
                            "errors": heartbeat_errors,
                            "failure_handler": handle_heartbeat_failure,
                        },
                        daemon=True,
                        name="ezhpcy-worker-heartbeat",
                    )
                    heartbeat_thread.start()
                    destination = (worker_host, worker_port)
                    logger.info(
                        "Waiting for worker SSH endpoint %s:%d.",
                        worker_host,
                        worker_port,
                    )
                    _wait_for_worker_endpoint(
                        transport,
                        destination,
                        timeout_seconds=startup_timeout_seconds,
                        poll_interval=job_poll_interval_seconds,
                        failure_check=lambda: _raise_heartbeat_failure(
                            heartbeat_errors
                        ),
                    )

                    backend = create_tunnel_backend(
                        alias=ssh_host.alias,
                        debug=logger.isEnabledFor(logging.DEBUG),
                    )
                    tunnel = TunnelServer(
                        transport,
                        destination,
                        backend,
                        error_handler=lambda error: logger.error(
                            "Tunnel client error: %s", error
                        ),
                    )
                    monitor_stop = threading.Event()
                    monitor_errors: list[TunnelError] = []
                    monitor = threading.Thread(
                        target=_monitor_scheduler_job,
                        args=(
                            scheduler,
                            job_id,
                            tunnel,
                            monitor_stop,
                            job_finished,
                            monitor_errors,
                            job_monitor_interval_seconds,
                        ),
                        daemon=True,
                        name="ezhpcy-job-monitor",
                    )
                    monitor.start()

                    _publish_ssh_host(
                        ssh_host,
                        instance_id=backend.instance_id,
                        in_profiles_config=ssh_host_in_profiles_config,
                    )
                    logger.info(
                        "Tunnel ready for %s via worker %s:%d (press Ctrl+C to stop).",
                        ssh_host.alias,
                        worker_host,
                        worker_port,
                    )
                    logger.info(
                        "Connect with `ssh %s` or select `%s` in VS Code Remote-SSH.",
                        ssh_host.alias,
                        ssh_host.alias,
                    )
                    shutdown_reason = "tunnel_stopped"
                    previous_sigbreak_handler = None
                    if hasattr(signal, "SIGBREAK"):
                        previous_sigbreak_handler = signal.signal(
                            signal.SIGBREAK, signal.default_int_handler
                        )
                    try:
                        tunnel.serve_forever()
                    except KeyboardInterrupt:
                        shutdown_reason = "user_interrupt"
                        logger.info("Stopping tunnel...")
                    finally:
                        monitor_stop.set()
                        # Withdraw the Host block before the tunnel descriptor,
                        # so a block never outlives the tunnel it points at.
                        _withdraw_ssh_host(ssh_host, instance_id=backend.instance_id)
                        tunnel.close()
                        monitor.join(timeout=1)
                        if previous_sigbreak_handler is not None:
                            signal.signal(signal.SIGBREAK, previous_sigbreak_handler)
                    if connection_errors:
                        raise connection_errors[0]
                    if monitor_errors:
                        shutdown_reason = "job_monitor_error"
                        raise monitor_errors[0]
                    if job_finished.is_set():
                        shutdown_reason = "worker_finished"
                    _raise_heartbeat_failure(heartbeat_errors)
                finally:
                    heartbeat_stop.set()
                    if heartbeat_thread is not None:
                        heartbeat_thread.join(timeout=1)
                        if heartbeat_thread.is_alive():
                            logger.warning(
                                "Worker heartbeat sender did not stop promptly: job=%s",
                                job_id,
                            )
                    transport_error = transport.get_exception()
                    logger.debug(
                        "Tunnel cleanup: job=%s reason=%s job_finished=%s "
                        "heartbeat_failed=%s transport_active=%s transport_error=%r",
                        job_id,
                        shutdown_reason,
                        job_finished.is_set(),
                        heartbeat_failed.is_set(),
                        transport.is_active(),
                        transport_error,
                    )
                    if not job_finished.is_set():
                        try:
                            scheduler.cancel(job_id)
                            logger.info("Cancelled worker job %s.", job_id)
                        except SchedulerError as error:
                            logger.warning(
                                "Could not cancel worker job %s: %s", job_id, error
                            )
                    output_stop.set()
                    if interactive_job is not None:
                        interactive_job.close()
                    if output_thread is not None:
                        output_thread.join(timeout=1)
            finally:
                control.close()


@with_connection
@with_scheduler
@with_resources
@with_timings
def tunnel_cmd(
    connection: ConnectionInfo,
    scheduler: SchedulerConfig,
    resources: ResourcesConfig,
    timings: TimingsConfig,
    profile: OptionalProfileArg = None,
    # Shared with `with_resources`, to tell resources given on the command line
    # apart from configured ones.
    queue: QueueOpt = None,
    cores: CoresOpt = None,
    gpus: GpusOpt = None,
    exclusive: ExclusiveOpt = None,
    time_limit: TimeLimitOpt = None,
    memory: MemoryOpt = None,
    alias: AliasOpt = None,
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
    """Allocate a compute node and expose its SSH service through a tunnel."""
    try:
        profile_config = resolve_profile_config(profile)
        resolved = ResolvedConfig(
            description=profile_config.description,
            connection=connection,
            scheduler=scheduler,
            resources=resources,
            timings=timings,
            lsf=profile_config.lsf,
            pbs=profile_config.pbs,
        )
        if scheduler.type is None:
            raise typer.BadParameter(
                "scheduler.type must be set by --scheduler or the selected profile",
                param_hint="--scheduler",
            )
        if scheduler.submission_mode is None:
            raise typer.BadParameter(
                "scheduler.submission_mode must be set by --submission-mode or the "
                "selected profile",
                param_hint="--submission-mode",
            )
        if (
            scheduler.submission_mode is SubmissionMode.BATCH
            and scheduler.interactive_submission_command is not None
        ):
            raise RichBadParameter(
                "scheduler.interactive_submission_command requires "
                "scheduler.submission_mode = 'interactive'",
                param_hint="interactive_submission_command",
            )
        if scheduler.interactive_submission_command is not None:
            submission_sections = ["resources"]
            if scheduler.type is SchedulerType.LSF:
                submission_sections.append("lsf")
            configured_submission_options = {
                f"{section}.{field}"
                for section in submission_sections
                for field in getattr(profile_config, section).model_fields_set
            }
            directly_configured_submission_options = (
                {
                    f"{section}.{field}"
                    for section in submission_sections
                    for field in getattr(
                        config.profile[profile], section
                    ).model_fields_set
                }
                if profile is not None
                else set()
            )
            cli_submission_options = {
                f"[bold red]{option}[/bold red]=[bold blue]{value}[/bold blue]"
                for option, value in (
                    ("--queue", queue),
                    ("--cores", cores),
                    ("--gpus", gpus),
                    ("--exclusive/--shared", exclusive),
                    ("--time-limit", time_limit),
                    ("--memory", memory),
                )
                if value is not None
            }
            conflicts = [
                *(
                    f"[bold blue]profile.{profile}[/bold blue].[bold red]{option}[/bold red]"
                    for option in sorted(directly_configured_submission_options)
                ),
                *sorted(cli_submission_options),
            ]
            if conflicts:
                raise RichBadParameter(
                    "scheduler.interactive_submission_command replaces the "
                    "scheduler-generated request and cannot be combined with "
                    "submission options: " + ", ".join(conflicts),
                    param_hint="interactive_submission_command",
                )
            inherited_submission_options = (
                configured_submission_options - directly_configured_submission_options
            )
            if inherited_submission_options:
                logger.info(
                    "Ignoring inherited scheduler submission options for "
                    "scheduler.interactive_submission_command: %s",
                    ", ".join(sorted(inherited_submission_options)),
                )
        ssh_host = _resolve_ssh_host(alias, profile_name=profile, resolved=resolved)
        written_profile_hosts = profile_hosts()
        try:
            write_profiles_config(written_profile_hosts)
        except (OSError, FilePermissionError) as error:
            logger.warning("Could not write the profile SSH configuration: %s", error)
            written_profile_hosts = []
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
            _ensure_local_worker_credentials(connection)
        worker_ports = (
            (worker_port,)
            if worker_port is not None
            else _select_worker_ports(worker_port_retries + 1)
        )
        _run_tunnel(
            profile_name=profile,
            profile=resolved,
            ssh_host=ssh_host,
            # A profile tunnel without user/host overrides renders exactly the
            # block profiles.conf already holds, so it needs no active file.
            ssh_host_in_profiles_config=ssh_host in written_profile_hosts,
            conn_info=connection,
            scheduler_type=scheduler.type,
            submission_mode=scheduler.submission_mode,
            queue=resources.queue,
            cores=resources.cores,
            gpus=resources.gpus,
            exclusive=resources.exclusive,
            time_limit=resources.time_limit_delta,
            memory_bytes=(
                int(resources.memory) if resources.memory is not None else None
            ),
            queue_timeout_seconds=timings.queue_timeout_seconds,
            startup_timeout_seconds=timings.worker_startup_timeout_seconds,
            job_poll_interval_seconds=timings.job_poll_interval_seconds,
            job_monitor_interval_seconds=timings.job_monitor_interval_seconds,
            heartbeat_interval_seconds=timings.worker_heartbeat_interval_seconds,
            heartbeat_timeout_seconds=timings.worker_heartbeat_timeout_seconds,
            worker_ports=worker_ports,
            auto_provision=auto_provision_enabled,
        )
    except KeyboardInterrupt:
        logger.info("Tunnel stopped.")
    except (IPCError, RuntimeError, paramiko.SSHException) as error:
        logger.error(
            "Could not start tunnel: %s",
            error,
            exc_info=logger.isEnabledFor(logging.DEBUG),
        )
        raise typer.Exit(code=1) from error
