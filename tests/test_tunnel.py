import logging
import re
import threading
from collections.abc import Callable
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import ANY, call, patch

import paramiko
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from ezhpcy.cli import app, tunnel as tunnel_module
from ezhpcy.cli.tunnel import (
    TunnelError,
    WorkerControl,
    _monitor_job,
    _run_tunnel,
    _select_worker_ports,
    _send_worker_lease_heartbeats,
    _wait_for_running_job,
    _wait_for_selected_worker_port,
    _wait_for_worker_endpoint,
    _worker_client_alive,
    _worker_sshd_command,
)
from ezhpcy.cli.utils.ssh import retrying_sshd_script, sshd_config_arguments
from ezhpcy.config import ConnectionInfo
from ezhpcy.constants import EZHPCY_VERSION, OPENSSH_MATCHSPEC, PIXI_VERSION
from ezhpcy.scheduler.base import InteractiveJob, JobInfo, JobSpec, JobState
from ezhpcy.scheduler.types import SchedulerType
from ezhpcy.tunnel.ssh_config import WorkerHost
from ezhpcy.types import (
    ProfileConfig,
    RemoteState,
    ResolvedConfig,
    ResolvedProfileConfig,
    SubmissionMode,
)

_MEBIBYTE = 1024**2


def lsf_profile() -> ResolvedConfig:
    return ResolvedConfig(
        host="login.example.com",
        user="alice",
        scheduler="LSF",
        submission_mode="interactive",
        lsf_resource_reserve_per_task=True,
        lsf_application_profile="qrsh",
        lsf_submission_environment={"LSF_QRSH": "true"},
        lsf_export_environment=["TERM", "LSF_QRSH"],
    )


def pbs_profile() -> ResolvedProfileConfig:
    return ResolvedProfileConfig(
        host="login.example.com",
        user="alice",
        scheduler="PBS",
        submission_mode="interactive",
        queue="workq",
        pbs_command_directory=PurePosixPath("/opt/pbspro/bin"),
    )


_BACKEND = SimpleNamespace(instance_id="instance-id")


def worker_host(alias: str = "gpu") -> WorkerHost:
    return WorkerHost(alias=alias, user="alice", ssh_dir=Path("ssh"))


@pytest.fixture(autouse=True)
def ssh_host_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, str, str, bool | None]]:
    """Record Host publication instead of writing files and running `ssh -G`."""
    calls: list[tuple[str, str, str, bool | None]] = []

    def publish(host: WorkerHost, *, instance_id: str, in_profiles_config: bool):
        calls.append(("publish", host.alias, instance_id, in_profiles_config))

    def withdraw(host: WorkerHost, *, instance_id: str):
        calls.append(("withdraw", host.alias, instance_id, None))

    monkeypatch.setattr(tunnel_module, "_publish_ssh_host", publish)
    monkeypatch.setattr(tunnel_module, "_withdraw_ssh_host", withdraw)
    return calls


def test_worker_sshd_command_retries_ports_through_pixi() -> None:
    remote_state = RemoteState(cache_dir=PurePosixPath("/home/alice/.cache"))
    command = _worker_sshd_command(
        remote_state,
        ports=(54321, 54322),
        heartbeat_token="LEASETOKEN",
        heartbeat_timeout_seconds=90,
        heartbeat_debug=True,
        control_dir=PurePosixPath("/home/alice/.cache/ezhpcy/control/test"),
        read_script_from_stdin=False,
        sshd_arguments=sshd_config_arguments(
            host_key=PurePosixPath("/remote/ssh_host_ed25519_key"),
            remote_username="alice",
            authorized_key=("ssh-ed25519", "WORKERKEY"),
        ),
    )

    assert command[:6] == (
        "env",
        f"PIXI_HOME=/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/pixi/{PIXI_VERSION}",
        f"PIXI_CACHE_DIR=/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/pixi_cache/"
        f"{PIXI_VERSION}",
        f"/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/pixi/{PIXI_VERSION}/bin/pixi",
        "exec",
        f"--spec={OPENSSH_MATCHSPEC}",
    )
    assert command[6:10] == (
        "bash",
        "-c",
        retrying_sshd_script(),
        "sshd",
    )
    assert command[6] == "bash"
    assert command[10:16] == (
        "54321:54322",
        "LEASETOKEN",
        "90",
        "1",
        f"/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/logs/worker",
        "/home/alice/.cache/ezhpcy/control/test",
    )
    worker_script = retrying_sshd_script()
    assert "command -v sshd" in worker_script
    assert 'ports="$1"' in worker_script
    assert "shift 6" in worker_script
    assert 'port="${ports%%:*}"' in worker_script
    assert '"$sshd_path" -D -e -p "$port" "$@"' in worker_script
    assert 'publish_state ready "$port"' in worker_script
    assert command.count("bash") == 1
    assert command.count("-c") == 1
    assert "PidFile=none" in command
    assert "HostKey=/remote/ssh_host_ed25519_key" in command
    assert "AuthorizedKeysCommand=/bin/echo ssh-ed25519 WORKERKEY" in command


@pytest.mark.parametrize(
    ("interval", "timeout", "expected"),
    [
        (30, 90, (30, 3)),
        (20, 60, (20, 3)),
        (30, 100, (30, 3)),
        (0.5, 5, (1, 5)),
        (30, 20, (30, 1)),
    ],
)
def test_worker_client_alive_gives_up_within_the_heartbeat_timeout(
    interval: float, timeout: float, expected: tuple[int, int]
) -> None:
    assert _worker_client_alive(interval, timeout) == expected


class StubScheduler:
    scheduler_type = SchedulerType.LSF

    def __init__(self, snapshots: list[JobInfo]) -> None:
        self.snapshots = snapshots
        self.submitted: list[JobSpec] = []
        self.submitted_scripts: list[str] = []
        self.cancelled: list[str] = []
        self.command_starts = 0

    def submit(self, spec: JobSpec) -> str:
        self.submitted.append(spec)
        return "42"

    def submit_script(self, spec: JobSpec, script: str) -> str:
        self.submitted.append(spec)
        self.submitted_scripts.append(script)
        return "42"

    def submit_interactive(
        self, spec: JobSpec, *, startup_timeout: float
    ) -> InteractiveJob:
        assert startup_timeout == 10
        self.submitted.append(spec)
        self.process = StubProcess()
        job = InteractiveJob(
            "42",
            self.process,
            "Job <42> is submitted",
            ("bsub", "-Is", "-q", "hpcint", "echo", "hello world"),
            command_starter=lambda: setattr(
                self, "command_starts", self.command_starts + 1
            ),
        )
        return job

    def inspect(self, _job_id: str) -> JobInfo:
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]

    def cancel(self, job_id: str) -> None:
        self.cancelled.append(job_id)


class StubChannel:
    def __init__(self, banner: bytes) -> None:
        self.banner = banner
        self.timeout: float | None = None
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def recv(self, _size: int) -> bytes:
        banner, self.banner = self.banner, b""
        return banner

    def close(self) -> None:
        self.closed = True


class StubProcess:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[bytes] = []

    def recv_ready(self) -> bool:
        return False

    def recv(self, _size: int) -> bytes:
        return b""

    def recv_stderr_ready(self) -> bool:
        return False

    def recv_stderr(self, _size: int) -> bytes:
        return b""

    def exit_status_ready(self) -> bool:
        return False

    def recv_exit_status(self) -> int:
        raise AssertionError("process is still running")

    def send(self, data: bytes | str) -> int:
        encoded = data.encode() if isinstance(data, str) else data
        self.sent.append(encoded)
        return len(encoded)

    def close(self) -> None:
        self.closed = True


class OutputProcess(StubProcess):
    def __init__(self, chunks: list[bytes]) -> None:
        super().__init__()
        self.chunks = chunks

    def recv_ready(self) -> bool:
        return bool(self.chunks)

    def recv(self, _size: int) -> bytes:
        return self.chunks.pop(0)

    def exit_status_ready(self) -> bool:
        return not self.chunks


class FinishedProcess(StubProcess):
    def __init__(self, exit_status: int = 0) -> None:
        super().__init__()
        self.exit_status = exit_status

    def exit_status_ready(self) -> bool:
        return True

    def recv_exit_status(self) -> int:
        return self.exit_status


class StubTransport:
    def __init__(self) -> None:
        self.channel = StubChannel(b"SSH-2.0-OpenSSH_10.4\r\n")
        self.attempts = 0

    def is_active(self) -> bool:
        return True

    def get_exception(self):
        return None

    def open_channel(self, _kind: str, **kwargs) -> StubChannel:
        assert kwargs["dest_addr"] == ("node42", 54321)
        assert kwargs["src_addr"] == ("ezhpcy-readiness", 0)
        self.attempts += 1
        if self.attempts == 1:
            raise paramiko.SSHException("connection refused")
        return self.channel


class StubSSH:
    def __init__(self, transport: StubTransport) -> None:
        self.transport = transport
        self.connection_lost_handler: Callable[[Exception], None] | None = None
        self.commands: list[list[str]] = []
        self.directories: list[PurePosixPath] = []
        self.files: dict[PurePosixPath, str] = {}
        self.chmods: list[tuple[PurePosixPath, int]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        pass

    def interactive_connect(self) -> None:
        pass

    def set_connection_lost_handler(self, handler) -> None:
        self.connection_lost_handler = handler

    def get_transport(self) -> StubTransport:
        return self.transport

    def get_remote_state(self) -> RemoteState:
        return RemoteState(
            cache_dir=PurePosixPath("/home/alice/.cache"),
        )

    @contextmanager
    def sftp_client(self):
        ssh = self

        class StubSFTP:
            def mkdir(self, path, **_kwargs) -> None:
                ssh.directories.append(PurePosixPath(path))

            def write_text(self, path, content) -> int:
                ssh.files[PurePosixPath(path)] = content
                return len(content)

            def read_text(self, path) -> str:
                try:
                    return ssh.files[PurePosixPath(path)]
                except KeyError as error:
                    raise FileNotFoundError(path) from error

            def chmod(self, path, mode) -> None:
                ssh.chmods.append((PurePosixPath(path), mode))

            def posix_rename(self, source, destination) -> None:
                ssh.files[PurePosixPath(destination)] = ssh.files.pop(
                    PurePosixPath(source)
                )

            def remove(self, path) -> None:
                try:
                    del ssh.files[PurePosixPath(path)]
                except KeyError as error:
                    raise FileNotFoundError(path) from error

            def rmdir(self, _path) -> None:
                pass

        yield StubSFTP()

    def run(self, args: list[str]) -> str:
        self.commands.append(args)
        return ""

    def run_login_shell(self, args: list[str]) -> str:
        self.commands.append(["bash", "-lc", *args])
        return ""

    def run_login_shell_with_input(self, args: list[str], stdin: str) -> str:
        self.commands.append(["bash", "-lc", *args, stdin])
        return ""

    def start_login_shell(self, _args: list[str]) -> StubProcess:
        return StubProcess()


class StubBroker:
    def __init__(self, _transport, destination, _backend, **_kwargs) -> None:
        self.destination = destination
        self.closed = False

    def serve_forever(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def snapshot(state: JobState, raw_state: str, host: str | None = None) -> JobInfo:
    return JobInfo(
        job_id="42",
        state=state,
        raw_state=raw_state,
        execution_hosts=(host,) if host is not None else (),
    )


@pytest.mark.parametrize(
    ("value", "expected_bytes"),
    [
        (None, None),
        ("2048", 2048),
        ("2048MB", 2_048_000_000),
        ("256GB", 256_000_000_000),
        ("1.5 GiB", 1_610_612_736),
        ("1TB", 1_000_000_000_000),
        ("1B", 1),
    ],
)
def test_parse_memory_normalizes_sizes_to_bytes(
    value: str | None, expected_bytes: int | None
) -> None:
    memory = ResolvedProfileConfig(memory=value).memory
    assert (int(memory) if memory is not None else None) == expected_bytes


@pytest.mark.parametrize("value", ["0", "-1GB", "GB", "12QQ", "lots"])
def test_parse_memory_rejects_invalid_sizes(value: str) -> None:
    with pytest.raises(ValidationError, match="memory"):
        ResolvedProfileConfig(memory=value)


def test_wait_for_running_job_reports_transitions_and_returns_host() -> None:
    scheduler = StubScheduler(
        [
            snapshot(JobState.PENDING, "PEND"),
            snapshot(JobState.RUNNING, "RUN", "node42"),
        ]
    )
    transitions: list[JobState] = []

    with patch("ezhpcy.cli.tunnel.time.sleep"):
        info = _wait_for_running_job(
            scheduler,  # type: ignore[arg-type]
            "42",
            timeout_seconds=10,
            poll_interval=2.5,
            state_handler=lambda item: transitions.append(item.state),
        )

    assert info.primary_host == "node42"
    assert transitions == [JobState.PENDING, JobState.RUNNING]


def test_wait_for_running_job_fails_when_job_exits() -> None:
    scheduler = StubScheduler([snapshot(JobState.FAILED, "EXIT")])

    with pytest.raises(TunnelError, match="scheduler state EXIT"):
        _wait_for_running_job(
            scheduler,  # type: ignore[arg-type]
            "42",
            timeout_seconds=10,
            poll_interval=2.5,
        )


def test_select_worker_ports_returns_unique_dynamic_ports() -> None:
    with patch(
        "ezhpcy.cli.tunnel.secrets.randbelow",
        side_effect=[0, 0, 1, 2, 3, 4],
    ):
        ports = _select_worker_ports(5)

    assert ports == (49152, 49153, 49154, 49155, 49156)


def test_worker_control_reports_ready_port_and_bounds_leases() -> None:
    ssh = StubSSH(StubTransport())
    with ssh.sftp_client() as sftp:
        control = WorkerControl(
            sftp,
            PurePosixPath("/control/run"),
            "LEASETOKEN",
            ports=(54321, 54322),
        )
        control.create()
        for _ in range(4):
            control.publish_lease()
        ssh.files[control.directory / "ready"] = "v1 LEASETOKEN ready 54322\n"

        assert _wait_for_selected_worker_port(control, timeout_seconds=1) == 54322
        assert sorted(
            path.name for path in ssh.files if path.name.startswith("lease.")
        ) == [
            "lease.00000000000000000003",
            "lease.00000000000000000004",
        ]


def test_worker_control_rejects_invalid_ready_port() -> None:
    ssh = StubSSH(StubTransport())
    with ssh.sftp_client() as sftp:
        control = WorkerControl(
            sftp,
            PurePosixPath("/control/run"),
            "LEASETOKEN",
            ports=(54321,),
        )
        control.create()
        ssh.files[control.directory / "ready"] = "v1 LEASETOKEN ready 54322\n"

        with pytest.raises(TunnelError, match="unexpected SSH port"):
            _wait_for_selected_worker_port(control, timeout_seconds=1)


def test_job_monitor_uses_interactive_process_without_scheduler_polling() -> None:
    job = InteractiveJob("42", FinishedProcess(0), "")
    broker = StubBroker(None, ("node42", 54321), None)
    job_finished = threading.Event()
    errors: list[TunnelError] = []

    _monitor_job(
        job, broker, threading.Event(), job_finished, errors, monitor_interval=0
    )

    assert job_finished.is_set()
    assert not errors
    assert broker.closed


def test_job_monitor_does_not_treat_missing_exit_status_as_job_completion() -> None:
    job = InteractiveJob("42", FinishedProcess(-1), "")
    broker = StubBroker(None, ("node42", 54321), None)
    job_finished = threading.Event()
    errors: list[TunnelError] = []

    _monitor_job(
        job, broker, threading.Event(), job_finished, errors, monitor_interval=0
    )

    assert not job_finished.is_set()
    assert len(errors) == 1
    assert "closed without an exit status" in str(errors[0])
    assert broker.closed


def test_worker_heartbeat_sender_logs_sequence_without_token() -> None:
    stop_requested = threading.Event()

    class OneHeartbeatControl:
        def publish_lease(self) -> int:
            stop_requested.set()
            return 1

    errors: list[TunnelError] = []

    with patch("ezhpcy.cli.tunnel.logger") as logger:
        _send_worker_lease_heartbeats(
            OneHeartbeatControl(),  # type: ignore[arg-type]
            StubTransport(),  # type: ignore[arg-type]
            job_id="42",
            interval_seconds=30,
            stop_requested=stop_requested,
            failed=threading.Event(),
            errors=errors,
            failure_handler=lambda: None,
        )

    assert not errors
    rendered_calls = repr(logger.method_calls)
    assert "sequence=%d" in rendered_calls
    assert "SECRET-LEASE-TOKEN" not in rendered_calls


def test_worker_heartbeat_sender_reports_filesystem_failure() -> None:
    class FailedHeartbeatControl:
        def publish_lease(self) -> int:
            raise OSError("channel closed")

    failed = threading.Event()
    errors: list[TunnelError] = []
    failure_handled = threading.Event()

    _send_worker_lease_heartbeats(
        FailedHeartbeatControl(),  # type: ignore[arg-type]
        StubTransport(),  # type: ignore[arg-type]
        job_id="42",
        interval_seconds=30,
        stop_requested=threading.Event(),
        failed=failed,
        errors=errors,
        failure_handler=failure_handled.set,
    )

    assert failed.is_set()
    assert failure_handled.is_set()
    assert len(errors) == 1
    assert "sequence 0" in str(errors[0])
    assert "SECRET-LEASE-TOKEN" not in str(errors[0])


def test_worker_endpoint_waits_for_an_ssh_banner() -> None:
    transport = StubTransport()
    transport_logger = logging.getLogger("paramiko.transport")
    previous_level = transport_logger.level
    transport_logger.setLevel(logging.WARNING)

    try:
        with patch("ezhpcy.cli.tunnel.time.sleep"):
            _wait_for_worker_endpoint(
                transport,  # type: ignore[arg-type]
                ("node42", 54321),
                timeout_seconds=10,
                poll_interval=2.5,
            )

        assert transport.attempts == 2
        assert transport.channel.closed
        assert transport_logger.level == logging.WARNING
    finally:
        transport_logger.setLevel(previous_level)


def test_tunnel_submits_worker_starts_broker_and_cancels(
    ssh_host_publication: list[tuple[str, str, str, bool | None]],
) -> None:
    transport = StubTransport()
    ssh = StubSSH(transport)
    scheduler = StubScheduler([snapshot(JobState.RUNNING, "RUN", "node42")])
    brokers: list[StubBroker] = []
    configuration = lsf_profile()

    def make_broker(*args, **kwargs) -> StubBroker:
        broker = StubBroker(*args, **kwargs)
        brokers.append(broker)
        return broker

    with (
        patch(
            "ezhpcy.cli.tunnel.InteractiveSSHClient",
            return_value=ssh,
        ),
        patch("ezhpcy.cli.tunnel.local_machine_id", return_value="machine-id"),
        patch("ezhpcy.cli.tunnel.secrets.token_hex", return_value="LEASETOKEN"),
        patch(
            "ezhpcy.cli.tunnel.LSFScheduler",
            return_value=scheduler,
        ) as scheduler_constructor,
        patch(
            "ezhpcy.cli.tunnel.provision_worker_infrastructure",
            return_value=None,
        ),
        patch(
            "ezhpcy.cli.tunnel.read_ed25519_public_key",
            return_value=("ssh-ed25519", "WORKERKEY"),
        ),
        patch(
            "ezhpcy.cli.tunnel._wait_for_selected_worker_port",
            return_value=54322,
        ),
        patch("ezhpcy.cli.tunnel._wait_for_worker_endpoint"),
        patch(
            "ezhpcy.cli.tunnel.create_broker_backend", return_value=_BACKEND
        ) as create_backend,
        patch("ezhpcy.cli.tunnel.ForegroundBroker", side_effect=make_broker),
        patch("ezhpcy.cli.tunnel.logger") as logger,
    ):
        logger.isEnabledFor.return_value = False
        _run_tunnel(
            profile_name=None,
            profile=configuration,
            ssh_host=worker_host(),
            ssh_host_in_profiles_config=False,
            conn_info=ConnectionInfo(user="alice", host="login.example.com"),
            scheduler_type=SchedulerType.LSF,
            queue="normal",
            cores=32,
            gpus=2,
            exclusive=True,
            time_limit=timedelta(minutes=90),
            memory_bytes=2048 * _MEBIBYTE,
            queue_timeout_seconds=10,
            startup_timeout_seconds=10,
            job_poll_interval_seconds=configuration.job_poll_interval_seconds,
            job_monitor_interval_seconds=configuration.job_monitor_interval_seconds,
            worker_ports=(54321, 54322),
            auto_provision=True,
            submission_mode=SubmissionMode.INTERACTIVE,
        )

    assert len(scheduler.submitted) == 1
    create_backend.assert_called_once_with(alias="gpu", debug=False)
    spec = scheduler.submitted[0]
    assert tuple(spec.command) == _worker_sshd_command(
        ssh.get_remote_state(),
        ports=(54321, 54322),
        heartbeat_token="LEASETOKEN",
        heartbeat_timeout_seconds=90,
        heartbeat_debug=False,
        control_dir=PurePosixPath(
            f"/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/worker_control/LEASETOKEN"
        ),
        read_script_from_stdin=False,
        sshd_arguments=sshd_config_arguments(
            host_key=PurePosixPath(
                f"/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/ssh/machine-id/"
                "alice@login.example.com/ssh_host_ed25519_key"
            ),
            remote_username="alice",
            authorized_key=("ssh-ed25519", "WORKERKEY"),
            client_alive=(30, 3),
        ),
    )
    assert "AuthorizedKeysCommand=/bin/echo ssh-ed25519 WORKERKEY" in spec.command
    assert "ClientAliveInterval=30" in spec.command
    assert "ClientAliveCountMax=3" in spec.command
    assert spec.queue == "normal"
    assert spec.memory_bytes == 2048 * _MEBIBYTE
    assert spec.cores == 32
    assert spec.gpus == 2
    assert spec.exclusive
    assert scheduler_constructor.call_args.kwargs["resource_reserve_per_task"]
    assert ssh.directories[:2] == [
        PurePosixPath(f"/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/worker_cwd"),
        PurePosixPath(f"/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/logs/worker"),
    ]
    assert (
        PurePosixPath(f"/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/worker_control"),
        0o700,
    ) in ssh.chmods
    assert spec.working_directory == PurePosixPath(
        f"/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/worker_cwd"
    )
    assert spec.stdout_path == PurePosixPath(
        f"/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/logs/worker/worker-LEASETOKEN.out"
    )
    assert spec.stderr_path == PurePosixPath(
        f"/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/logs/worker/worker-LEASETOKEN.err"
    )
    assert brokers[0].destination == ("node42", 54322)
    assert brokers[0].closed
    # The Host block is published once the broker is up and withdrawn on exit.
    assert ssh_host_publication == [
        ("publish", "gpu", "instance-id", False),
        ("withdraw", "gpu", "instance-id", None),
    ]
    assert scheduler.cancelled == ["42"]
    assert scheduler.process.closed
    assert scheduler.command_starts == 1
    assert ssh.commands == []
    logger.debug.assert_any_call(
        "Submission command: %s", "bsub -Is -q hpcint echo 'hello world'"
    )
    logger.info.assert_any_call("Submitted worker job %s.", "42")
    logger.info.assert_any_call(
        "Tunnel ready for %s via worker %s:%d (press Ctrl+C to stop).",
        "gpu",
        "node42",
        54322,
    )


def test_tunnel_cancels_job_when_worker_startup_fails() -> None:
    transport = StubTransport()
    ssh = StubSSH(transport)
    scheduler = StubScheduler([snapshot(JobState.RUNNING, "RUN", "node42")])

    with (
        patch(
            "ezhpcy.cli.tunnel.InteractiveSSHClient",
            return_value=ssh,
        ),
        patch("ezhpcy.cli.tunnel.local_machine_id", return_value="machine-id"),
        patch(
            "ezhpcy.cli.tunnel.LSFScheduler",
            return_value=scheduler,
        ),
        patch(
            "ezhpcy.cli.tunnel.provision_worker_infrastructure",
            return_value=None,
        ),
        patch(
            "ezhpcy.cli.tunnel.read_ed25519_public_key",
            return_value=("ssh-ed25519", "WORKERKEY"),
        ),
        patch(
            "ezhpcy.cli.tunnel._wait_for_selected_worker_port",
            return_value=54321,
        ),
        patch(
            "ezhpcy.cli.tunnel._wait_for_worker_endpoint",
            side_effect=TunnelError("worker did not listen"),
        ),
        pytest.raises(TunnelError, match="did not listen"),
    ):
        _run_tunnel(
            profile_name="base",
            profile=lsf_profile(),
            ssh_host=worker_host(),
            ssh_host_in_profiles_config=False,
            conn_info=ConnectionInfo(user="alice", host="login.example.com"),
            scheduler_type=SchedulerType.LSF,
            queue=None,
            cores=4,
            gpus=0,
            exclusive=False,
            time_limit=timedelta(minutes=60),
            memory_bytes=1024 * _MEBIBYTE,
            queue_timeout_seconds=10,
            startup_timeout_seconds=10,
            job_poll_interval_seconds=2.5,
            job_monitor_interval_seconds=60,
            worker_ports=(54321,),
            auto_provision=True,
            submission_mode=SubmissionMode.INTERACTIVE,
        )

    assert scheduler.cancelled == ["42"]
    assert scheduler.process.closed


def test_tunnel_stops_when_the_login_connection_is_lost() -> None:
    transport = StubTransport()
    ssh = StubSSH(transport)
    scheduler = StubScheduler([snapshot(JobState.RUNNING, "RUN", "node42")])
    brokers: list[StubBroker] = []

    class LosingBroker(StubBroker):
        def serve_forever(self) -> None:
            assert ssh.connection_lost_handler is not None
            ssh.connection_lost_handler(
                paramiko.SSHException("session stopped answering")
            )

    def make_broker(*args, **kwargs) -> StubBroker:
        broker = LosingBroker(*args, **kwargs)
        brokers.append(broker)
        return broker

    with (
        patch("ezhpcy.cli.tunnel.InteractiveSSHClient", return_value=ssh),
        patch("ezhpcy.cli.tunnel.local_machine_id", return_value="machine-id"),
        patch("ezhpcy.cli.tunnel.LSFScheduler", return_value=scheduler),
        patch(
            "ezhpcy.cli.tunnel.provision_worker_infrastructure",
            return_value=None,
        ),
        patch(
            "ezhpcy.cli.tunnel.read_ed25519_public_key",
            return_value=("ssh-ed25519", "WORKERKEY"),
        ),
        patch(
            "ezhpcy.cli.tunnel._wait_for_selected_worker_port",
            return_value=54321,
        ),
        patch("ezhpcy.cli.tunnel._wait_for_worker_endpoint"),
        patch("ezhpcy.cli.tunnel.create_broker_backend", return_value=_BACKEND),
        patch("ezhpcy.cli.tunnel.ForegroundBroker", side_effect=make_broker),
        pytest.raises(TunnelError, match="login-node SSH connection lost"),
    ):
        _run_tunnel(
            profile_name="base",
            profile=lsf_profile(),
            ssh_host=worker_host(),
            ssh_host_in_profiles_config=False,
            conn_info=ConnectionInfo(user="alice", host="login.example.com"),
            scheduler_type=SchedulerType.LSF,
            queue=None,
            cores=4,
            gpus=0,
            exclusive=False,
            time_limit=timedelta(minutes=60),
            memory_bytes=1024 * _MEBIBYTE,
            queue_timeout_seconds=10,
            startup_timeout_seconds=10,
            job_poll_interval_seconds=2.5,
            job_monitor_interval_seconds=60,
            worker_ports=(54321,),
            auto_provision=True,
            submission_mode=SubmissionMode.INTERACTIVE,
        )

    assert brokers[0].closed
    assert scheduler.cancelled == ["42"]


def test_tunnel_uses_explicit_pbs_and_linuxsh_defaults() -> None:
    transport = StubTransport()
    ssh = StubSSH(transport)
    scheduler = StubScheduler([snapshot(JobState.RUNNING, "R", "node42")])

    with (
        patch("ezhpcy.cli.tunnel.InteractiveSSHClient", return_value=ssh),
        patch("ezhpcy.cli.tunnel.local_machine_id", return_value="machine-id"),
        patch(
            "ezhpcy.cli.tunnel.PBSScheduler", return_value=scheduler
        ) as scheduler_constructor,
        patch(
            "ezhpcy.cli.tunnel.provision_worker_infrastructure",
            return_value=None,
        ),
        patch(
            "ezhpcy.cli.tunnel.read_ed25519_public_key",
            return_value=("ssh-ed25519", "WORKERKEY"),
        ),
        patch(
            "ezhpcy.cli.tunnel._wait_for_selected_worker_port",
            return_value=54321,
        ),
        patch("ezhpcy.cli.tunnel._wait_for_worker_endpoint"),
        patch("ezhpcy.cli.tunnel.create_broker_backend", return_value=_BACKEND),
        patch("ezhpcy.cli.tunnel.ForegroundBroker", StubBroker),
        patch("ezhpcy.cli.tunnel.logger") as logger,
    ):
        _run_tunnel(
            profile_name="pbs",
            profile=pbs_profile(),
            ssh_host=worker_host(),
            ssh_host_in_profiles_config=False,
            conn_info=ConnectionInfo(user="alice", host="login.example.com"),
            scheduler_type=SchedulerType.PBS,
            queue="workq",
            cores=1,
            gpus=0,
            exclusive=False,
            time_limit=None,
            memory_bytes=None,
            queue_timeout_seconds=10,
            startup_timeout_seconds=10,
            job_poll_interval_seconds=2.5,
            job_monitor_interval_seconds=60,
            worker_ports=(54321,),
            auto_provision=True,
            submission_mode=SubmissionMode.INTERACTIVE,
        )

    assert scheduler.submitted[0].cores == 1
    assert scheduler.submitted[0].gpus == 0
    assert not scheduler.submitted[0].exclusive
    assert scheduler_constructor.call_args.kwargs["command_directory"] == PurePosixPath(
        "/opt/pbspro/bin"
    )
    assert scheduler.submitted[0].queue == "workq"
    assert scheduler.submitted[0].memory_bytes is None
    assert scheduler.command_starts == 1
    assert call("Using profile %r with %s scheduler.", "pbs", "PBS") in (
        logger.info.call_args_list
    )
    assert any(
        log_call.args[0].startswith("Worker logs:")
        for log_call in logger.info.call_args_list
    )


def test_tunnel_submits_batch_job_without_interactive_shell() -> None:
    transport = StubTransport()
    ssh = StubSSH(transport)
    scheduler = StubScheduler([snapshot(JobState.RUNNING, "RUN", "node42")])

    with (
        patch("ezhpcy.cli.tunnel.InteractiveSSHClient", return_value=ssh),
        patch("ezhpcy.cli.tunnel.local_machine_id", return_value="machine-id"),
        patch("ezhpcy.cli.tunnel.secrets.token_hex", return_value="LEASETOKEN"),
        patch("ezhpcy.cli.tunnel.LSFScheduler", return_value=scheduler),
        patch("ezhpcy.cli.tunnel.provision_worker_infrastructure"),
        patch(
            "ezhpcy.cli.tunnel.read_ed25519_public_key",
            return_value=("ssh-ed25519", "WORKERKEY"),
        ),
        patch("ezhpcy.cli.tunnel._wait_for_selected_worker_port", return_value=54321),
        patch("ezhpcy.cli.tunnel._wait_for_worker_endpoint"),
        patch("ezhpcy.cli.tunnel.create_broker_backend", return_value=_BACKEND),
        patch("ezhpcy.cli.tunnel.ForegroundBroker", StubBroker),
    ):
        _run_tunnel(
            profile_name="batch",
            profile=lsf_profile(),
            ssh_host=worker_host(),
            ssh_host_in_profiles_config=False,
            conn_info=ConnectionInfo(user="alice", host="login.example.com"),
            scheduler_type=SchedulerType.LSF,
            submission_mode=SubmissionMode.BATCH,
            queue="gpul40s",
            cores=8,
            gpus=1,
            exclusive=False,
            time_limit=timedelta(hours=1),
            memory_bytes=None,
            queue_timeout_seconds=10,
            startup_timeout_seconds=10,
            job_poll_interval_seconds=2.5,
            job_monitor_interval_seconds=60,
            worker_ports=(54321,),
            auto_provision=True,
        )

    assert len(scheduler.submitted) == 1
    assert scheduler.submitted_scripts[0].startswith("#!/usr/bin/env bash\nexec ")
    assert (
        "AuthorizedKeysCommand=/bin/echo ssh-ed25519 WORKERKEY"
        in (scheduler.submitted_scripts[0])
    )
    assert scheduler.command_starts == 0
    assert scheduler.cancelled == ["42"]


def test_tunnel_help_exposes_scheduler_and_resource_options() -> None:
    result = CliRunner().invoke(app, ["tunnel", "--help"], terminal_width=160)
    alias_result = CliRunner().invoke(app, ["t", "--help"], terminal_width=160)

    assert result.exit_code == 0
    assert alias_result.exit_code == 0
    assert "--scheduler" in result.stdout
    assert "--submission-mode" in result.stdout
    assert "--queue" in result.stdout
    assert "--cores" in result.stdout
    assert "--gpus" in result.stdout
    assert "--exclusive" in result.stdout
    assert "--time-limit" in result.stdout
    assert "--memory" in result.stdout
    assert "--queue-timeout" in result.stdout
    assert "--startup-timeout" in result.stdout
    assert "--job-poll-interval" in result.stdout
    assert "--job-monitor-inte" in result.stdout
    assert "--worker-heartbeat" in result.stdout
    assert "--interactive-subm" in result.stdout
    assert "--worker-port" in result.stdout
    # Rich abbreviates long option names in its fixed-width option column.
    assert "--worker-port-retr" in result.stdout
    assert "--auto-provision" in result.stdout
    assert "--no-auto-provision" in result.stdout
    assert "PROFILE" in result.stdout


def test_tunnel_command_requires_a_resolvable_configuration() -> None:
    result = CliRunner().invoke(app, ["tunnel"])

    assert result.exit_code == 2
    assert "user" in result.stderr
    assert "--user" in result.stderr


def test_tunnel_command_accepts_anonymous_cli_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        tunnel_module,
        "_run_tunnel",
        lambda **kwargs: captured.update(kwargs),
    )

    result = CliRunner().invoke(
        app,
        [
            "tunnel",
            "--host",
            "login.example.com",
            "--user",
            "alice",
            "--scheduler",
            "LSF",
            "--submission-mode",
            "interactive",
            "--queue",
            "gpu",
            "--cores",
            "8",
            "--gpus",
            "1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["profile_name"] is None
    assert captured["scheduler_type"] is SchedulerType.LSF
    assert captured["queue"] == "gpu"
    assert captured["cores"] == 8
    assert captured["gpus"] == 1
    assert captured["job_poll_interval_seconds"] == 2.5
    assert captured["job_monitor_interval_seconds"] == 60
    assert captured["conn_info"] == ConnectionInfo(
        host="login.example.com", user="alice"
    )
    resolved = captured["profile"]
    assert getattr(resolved, "user") == "alice"
    assert str(getattr(resolved, "host")) == "login.example.com"


def test_tunnel_command_requires_submission_mode() -> None:
    result = CliRunner().invoke(
        app,
        [
            "tunnel",
            "--host",
            "login.example.com",
            "--user",
            "alice",
            "--scheduler",
            "LSF",
        ],
    )

    assert result.exit_code == 2
    assert "submission_mode must be set" in result.output


def test_tunnel_command_accepts_batch_submission_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        tunnel_module,
        "_run_tunnel",
        lambda **kwargs: captured.update(kwargs),
    )

    result = CliRunner().invoke(
        app,
        [
            "tunnel",
            "--host",
            "login.example.com",
            "--user",
            "alice",
            "--scheduler",
            "LSF",
            "--submission-mode",
            "batch",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["submission_mode"] is SubmissionMode.BATCH


def test_tunnel_command_rejects_interactive_wrapper_in_batch_mode() -> None:
    result = CliRunner().invoke(
        app,
        [
            "tunnel",
            "--host",
            "login.example.com",
            "--user",
            "alice",
            "--scheduler",
            "LSF",
            "--submission-mode",
            "batch",
            "--interactive-submission-command",
            "/site/bin/interactive",
        ],
    )

    assert result.exit_code == 2
    assert "requires submission_mode" in result.output


def test_tunnel_command_accepts_cli_interactive_submission_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        tunnel_module,
        "_run_tunnel",
        lambda **kwargs: captured.update(kwargs),
    )

    result = CliRunner().invoke(
        app,
        [
            "tunnel",
            "--host",
            "login.example.com",
            "--user",
            "alice",
            "--scheduler",
            "LSF",
            "--submission-mode",
            "interactive",
            "--interactive-submission-command",
            "/lsf/local/bin/a100sh --constraint 'gpu node'",
        ],
    )

    assert result.exit_code == 0, result.output
    resolved = captured["profile"]
    assert getattr(resolved, "interactive_submission_command") == [
        "/lsf/local/bin/a100sh",
        "--constraint",
        "gpu node",
    ]


def test_tunnel_command_rejects_resources_with_cli_interactive_command() -> None:
    result = CliRunner().invoke(
        app,
        [
            "tunnel",
            "--host",
            "login.example.com",
            "--user",
            "alice",
            "--scheduler",
            "LSF",
            "--submission-mode",
            "interactive",
            "--interactive-submission-command",
            "/lsf/local/bin/a100sh",
            "--queue",
            "gpu",
        ],
    )

    assert result.exit_code == 2
    assert "cannot be combined with submission options" in result.output
    assert "--queue" in result.output


def test_tunnel_command_rejects_malformed_interactive_command_quoting() -> None:
    result = CliRunner().invoke(
        app,
        [
            "tunnel",
            "--host",
            "login.example.com",
            "--user",
            "alice",
            "--scheduler",
            "LSF",
            "--submission-mode",
            "interactive",
            "--interactive-submission-command",
            "'/lsf/local/bin/a100sh",
        ],
    )

    assert result.exit_code == 2
    assert "No closing quotation" in result.output


def test_tunnel_command_resolves_profile_and_applies_cli_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tunnel_module.config,
        "profile",
        {
            "base": ProfileConfig(
                host="login.example.com",
                user="alice",
                scheduler="LSF",
                submission_mode="interactive",
                queue="normal",
                cores=4,
                gpus=1,
                exclusive=True,
                time_limit="1:00",
                memory="32GB",
                ssh_keepalive_interval_seconds=75,
                worker_heartbeat_interval_seconds=20,
                worker_heartbeat_timeout_seconds=60,
                job_poll_interval_seconds=4,
                job_monitor_interval_seconds=120,
            ),
            "gpu": ProfileConfig(inherit="base", queue="gpu", cores=8),
        },
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        tunnel_module, "_ensure_local_worker_credentials", lambda _connection: None
    )
    monkeypatch.setattr(
        tunnel_module,
        "_run_tunnel",
        lambda **kwargs: captured.update(kwargs),
    )

    result = CliRunner().invoke(
        app,
        [
            "tunnel",
            "gpu",
            "--cores",
            "12",
            "--memory",
            "64GB",
            "--shared",
            "--worker-heartbeat-interval",
            "12",
            "--worker-heartbeat-timeout",
            "30",
            "--job-poll-interval",
            "1.5",
            "--job-monitor-interval",
            "45",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["profile_name"] == "gpu"
    assert captured["scheduler_type"] is SchedulerType.LSF
    assert captured["queue"] == "gpu"
    assert captured["cores"] == 12
    assert captured["gpus"] == 1
    assert captured["exclusive"] is False
    assert captured["memory_bytes"] == 64_000_000_000
    assert captured["time_limit"] == timedelta(hours=1)
    assert captured["conn_info"] == ConnectionInfo(
        host="login.example.com",
        user="alice",
        ssh_keepalive_interval_seconds=75,
    )
    assert captured["auto_provision"] is True
    assert captured["heartbeat_interval_seconds"] == 12
    assert captured["heartbeat_timeout_seconds"] == 30
    assert captured["job_poll_interval_seconds"] == 1.5
    assert captured["job_monitor_interval_seconds"] == 45
    worker_ports = captured["worker_ports"]
    assert isinstance(worker_ports, tuple)
    assert len(worker_ports) == 6
    assert len(set(worker_ports)) == 6

    captured.clear()
    result = CliRunner().invoke(
        app,
        [
            "tunnel",
            "base",
            "--worker-port",
            "55000",
            "--worker-port-retries",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["worker_ports"] == (55000,)
    assert captured["job_poll_interval_seconds"] == 4
    assert captured["job_monitor_interval_seconds"] == 120


def test_tunnel_command_allows_wrapper_with_implicit_resource_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tunnel_module.config,
        "profile",
        {
            "base": ProfileConfig(
                host="login.example.com",
                user="alice",
                scheduler="LSF",
                submission_mode="interactive",
                interactive_submission_command=["/site/bin/interactive-lsf"],
            )
        },
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        tunnel_module,
        "_run_tunnel",
        lambda **kwargs: captured.update(kwargs),
    )

    result = CliRunner().invoke(app, ["tunnel", "base"])

    assert result.exit_code == 0, result.output
    assert captured["queue"] is None
    assert captured["cores"] == 1
    assert captured["gpus"] == 0
    assert captured["exclusive"] is False
    profile = captured["profile"]
    assert getattr(profile, "interactive_submission_command") == [
        "/site/bin/interactive-lsf"
    ]


def test_tunnel_command_logs_inherited_submission_options_ignored_by_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tunnel_module.config,
        "profile",
        {
            "base": ProfileConfig(
                host="login.example.com",
                user="alice",
                scheduler="LSF",
                submission_mode="interactive",
                queue="normal",
                lsf_application_profile="qrsh",
            ),
            "wrapper": ProfileConfig(
                inherit="base",
                interactive_submission_command=["/site/bin/interactive-lsf"],
            ),
        },
    )
    monkeypatch.setattr(tunnel_module, "_run_tunnel", lambda **_kwargs: None)

    with patch("ezhpcy.cli.tunnel.logger") as logger:
        result = CliRunner().invoke(app, ["tunnel", "wrapper"])

    assert result.exit_code == 0, result.output
    logger.info.assert_called_once_with(
        "Ignoring inherited scheduler submission options for "
        "interactive_submission_command: %s",
        "lsf_application_profile, queue",
    )


@pytest.mark.parametrize(
    ("arguments", "option"),
    [
        (("--queue", "normal"), "--queue"),
        (("--cores", "1"), "--cores"),
        (("--gpus", "0"), "--gpus"),
        (("--shared",), "--exclusive/--shared"),
        (("--time-limit", "1:00"), "--time-limit"),
        (("--memory", "1GB"), "--memory"),
    ],
)
def test_tunnel_command_rejects_cli_resources_with_wrapper(
    monkeypatch: pytest.MonkeyPatch,
    arguments: tuple[str, ...],
    option: str,
) -> None:
    monkeypatch.setattr(
        tunnel_module.config,
        "profile",
        {
            "base": ProfileConfig(
                host="login.example.com",
                user="alice",
                scheduler="LSF",
                submission_mode="interactive",
                interactive_submission_command=["/site/bin/interactive-lsf"],
            )
        },
    )

    result = CliRunner().invoke(app, ["tunnel", "base", *arguments])

    assert result.exit_code == 2
    assert "cannot be combined with submission options" in result.output
    assert option in result.output


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("queue", "normal"),
        ("cores", 1),
        ("gpus", 0),
        ("exclusive", False),
        ("time_limit", "1:00"),
        ("memory", "1GB"),
        ("lsf_resource_reserve_per_task", False),
        ("lsf_application_profile", "qrsh"),
        ("lsf_submission_environment", {"LSF_QRSH": "true"}),
        ("lsf_export_environment", ["TERM"]),
    ],
)
def test_tunnel_command_rejects_configured_submission_options_with_wrapper(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    monkeypatch.setattr(
        tunnel_module.config,
        "profile",
        {
            "base": ProfileConfig(
                host="login.example.com",
                user="alice",
                scheduler="LSF",
                submission_mode="interactive",
                interactive_submission_command=["/site/bin/interactive-lsf"],
                **{field: value},
            )
        },
    )

    result = CliRunner().invoke(app, ["tunnel", "base"])

    assert result.exit_code == 2
    assert "cannot be combined with submission options" in result.output
    assert f"profile.base.{field}" in result.output


def test_tunnel_command_can_disable_auto_provision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tunnel_module.config,
        "profile",
        {
            "base": ProfileConfig(
                host="login.example.com",
                user="alice",
                scheduler="LSF",
                submission_mode="interactive",
            )
        },
    )
    credentials_checked = False
    captured: dict[str, object] = {}

    def check_credentials(_connection: ConnectionInfo) -> None:
        nonlocal credentials_checked
        credentials_checked = True

    monkeypatch.setattr(
        tunnel_module, "_ensure_local_worker_credentials", check_credentials
    )
    monkeypatch.setattr(
        tunnel_module,
        "_run_tunnel",
        lambda **kwargs: captured.update(kwargs),
    )

    result = CliRunner().invoke(app, ["tunnel", "base", "--no-auto-provision"])

    assert result.exit_code == 0, result.output
    assert credentials_checked
    assert captured["auto_provision"] is False


def test_tunnel_command_can_enable_auto_provision_when_config_disables_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tunnel_module.config, "auto_provision", False)
    monkeypatch.setattr(
        tunnel_module.config,
        "profile",
        {
            "base": ProfileConfig(
                host="login.example.com",
                user="alice",
                scheduler="LSF",
                submission_mode="interactive",
            )
        },
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        tunnel_module,
        "_run_tunnel",
        lambda **kwargs: captured.update(kwargs),
    )

    result = CliRunner().invoke(app, ["tunnel", "base", "--auto-provision"])

    assert result.exit_code == 0, result.output
    assert captured["auto_provision"] is True


def test_tunnel_command_rejects_conflicting_auto_provision_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tunnel_module.config,
        "profile",
        {
            "base": ProfileConfig(
                host="login.example.com",
                user="alice",
                scheduler="LSF",
                submission_mode="interactive",
            )
        },
    )

    result = CliRunner().invoke(
        app,
        [
            "tunnel",
            "base",
            "--auto-provision",
            "--no-auto-provision",
        ],
    )

    assert result.exit_code == 2
    assert "cannot be used together" in result.output


def test_tunnel_command_rejects_unknown_profile_before_starting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tunnel_module.config,
        "profile",
        {"base": ProfileConfig(host="login.example.com", user="alice")},
    )
    started = False

    def start(_connection: ConnectionInfo) -> None:
        nonlocal started
        started = True

    monkeypatch.setattr(tunnel_module, "_ensure_local_worker_credentials", start)

    result = CliRunner().invoke(app, ["tunnel", "missing"])

    assert result.exit_code == 2
    assert "unknown profile 'missing'" in result.stderr
    assert not started


def _alias_profiles() -> dict[str, ProfileConfig]:
    return {
        name: ProfileConfig(
            host="login.example.com",
            user="alice",
            scheduler="LSF",
            submission_mode="interactive",
        )
        for name in ("cpu", "gpu")
    }


def _capture_run_tunnel(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        tunnel_module, "_run_tunnel", lambda **kwargs: captured.update(kwargs)
    )
    return captured


_ANONYMOUS_TUNNEL_ARGS = [
    "tunnel",
    "--host",
    "login.example.com",
    "--user",
    "alice",
    "--scheduler",
    "LSF",
    "--submission-mode",
    "interactive",
]


def test_profile_tunnel_alias_defaults_to_its_profiles_conf_block(
    monkeypatch: pytest.MonkeyPatch, isolated_runtime_dir: Path
) -> None:
    monkeypatch.setattr(tunnel_module.config, "profile", _alias_profiles())
    captured = _capture_run_tunnel(monkeypatch)

    result = CliRunner().invoke(app, ["tunnel", "gpu"])

    assert result.exit_code == 0, result.output
    ssh_host = captured["ssh_host"]
    assert isinstance(ssh_host, WorkerHost)
    assert (ssh_host.alias, ssh_host.user) == ("gpu", "alice")
    assert captured["ssh_host_in_profiles_config"] is True
    profiles_conf = isolated_runtime_dir / "ssh-config" / "profiles.conf"
    assert "Host cpu\n" in profiles_conf.read_text(encoding="utf-8")
    assert "Host gpu\n" in profiles_conf.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("arguments", "alias", "user"),
    [
        (["--user", "bob"], "gpu", "bob"),
        (["--alias", "my-gpu"], "my-gpu", "alice"),
    ],
)
def test_profile_tunnel_that_differs_from_profiles_conf_needs_its_own_block(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str], alias: str, user: str
) -> None:
    monkeypatch.setattr(tunnel_module.config, "profile", _alias_profiles())
    captured = _capture_run_tunnel(monkeypatch)

    result = CliRunner().invoke(app, ["tunnel", "gpu", *arguments])

    assert result.exit_code == 0, result.output
    ssh_host = captured["ssh_host"]
    assert isinstance(ssh_host, WorkerHost)
    assert (ssh_host.alias, ssh_host.user) == (alias, user)
    assert captured["ssh_host_in_profiles_config"] is False


def test_tunnel_falls_back_to_its_own_block_when_profiles_conf_cannot_be_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tunnel_module.config, "profile", _alias_profiles())
    captured = _capture_run_tunnel(monkeypatch)

    def fail(_hosts: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(tunnel_module, "write_profiles_config", fail)

    with patch("ezhpcy.cli.tunnel.logger") as logger:
        result = CliRunner().invoke(app, ["tunnel", "gpu"])

    assert result.exit_code == 0, result.output
    assert captured["ssh_host_in_profiles_config"] is False
    logger.warning.assert_any_call(
        "Could not write the profile SSH configuration: %s", ANY
    )


def test_anonymous_tunnel_alias_is_stable_for_identical_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_run_tunnel(monkeypatch)

    def alias_for(*extra: str) -> str:
        result = CliRunner().invoke(app, [*_ANONYMOUS_TUNNEL_ARGS, *extra])
        assert result.exit_code == 0, result.output
        assert captured["ssh_host_in_profiles_config"] is False
        ssh_host = captured["ssh_host"]
        assert isinstance(ssh_host, WorkerHost)
        return ssh_host.alias

    first = alias_for("--queue", "gpu")

    assert re.fullmatch(r"ezhpcy-[0-9a-f]{12}", first)
    assert alias_for("--queue", "gpu") == first
    assert alias_for("--queue", "cpu") != first


def test_anonymous_tunnel_accepts_an_explicit_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_run_tunnel(monkeypatch)

    result = CliRunner().invoke(app, [*_ANONYMOUS_TUNNEL_ARGS, "--alias", "scratch"])

    assert result.exit_code == 0, result.output
    ssh_host = captured["ssh_host"]
    assert isinstance(ssh_host, WorkerHost)
    assert ssh_host.alias == "scratch"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["tunnel", "gpu", "--alias", "cpu"], "is the name of profile"),
        ([*_ANONYMOUS_TUNNEL_ARGS, "--alias", "gpu"], "is the name of profile"),
        (["tunnel", "gpu", "--alias", "two words"], "may contain only"),
        (["tunnel", "gpu", "--alias", "wild*"], "may contain only"),
    ],
)
def test_tunnel_rejects_unusable_aliases(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str], message: str
) -> None:
    monkeypatch.setattr(tunnel_module.config, "profile", _alias_profiles())
    captured = _capture_run_tunnel(monkeypatch)

    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 2
    assert message in result.stderr
    assert captured == {}
