import logging
import queue
import threading
from contextlib import contextmanager
from datetime import timedelta
from pathlib import PurePosixPath
from unittest.mock import call, patch

import paramiko
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from ezhpcy.cli import app, compute as compute_module
from ezhpcy.cli.compute import (
    ComputeTunnelError,
    _drain_interactive_job,
    _run_compute_tunnel,
    _select_worker_ports,
    _wait_for_running_job,
    _wait_for_selected_worker_port,
    _wait_for_worker_endpoint,
    _worker_sshd_command,
)
from ezhpcy.config import ConnectionInfo
from ezhpcy.constants import EZHPCY_VERSION, OPENSSH_MATCHSPEC, PIXI_VERSION
from ezhpcy.scheduler.base import InteractiveJob, JobInfo, JobSpec, JobState
from ezhpcy.scheduler.types import SchedulerType
from ezhpcy.types import ProfileConfig, RemoteState, ResolvedProfileConfig

_MEBIBYTE = 1024**2


def lsf_profile() -> ResolvedProfileConfig:
    return ResolvedProfileConfig(
        host="login.example.com",
        user="alice",
        scheduler="LSF",
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
        queue="workq",
        pbs_command_directory=PurePosixPath("/opt/pbspro/bin"),
    )


def test_worker_sshd_command_retries_ports_through_pixi() -> None:
    remote_state = RemoteState(cache_dir=PurePosixPath("/home/alice/.cache"))
    command = _worker_sshd_command(
        remote_state,
        host_key=PurePosixPath("/remote/ssh_host_ed25519_key"),
        remote_username="alice",
        authorized_key=("ssh-ed25519", "WORKERKEY"),
        ports=(54321, 54322),
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
    assert command[6:11] == (
        "sh",
        "-c",
        command[8],
        "sshd",
        "2",
    )
    assert command[11:13] == ("54321", "54322")
    assert "command -v sshd" in command[8]
    assert '"$sshd_path" -D -e -p "$port"' in command[8]
    assert "worker sshd selected port" in command[8]
    assert command.count("sh") == 1
    assert command.count("-c") == 1
    assert "PidFile=none" in command
    assert "HostKey=/remote/ssh_host_ed25519_key" in command
    assert "AuthorizedKeysCommand=/bin/echo ssh-ed25519 WORKERKEY" in command
    assert not any("ssh-serve" in argument for argument in command)


class StubScheduler:
    scheduler_type = SchedulerType.LSF

    def __init__(self, snapshots: list[JobInfo]) -> None:
        self.snapshots = snapshots
        self.submitted: list[JobSpec] = []
        self.cancelled: list[str] = []
        self.command_starts = 0

    def submit(self, spec: JobSpec) -> str:
        self.submitted.append(spec)
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


class StubTransport:
    def __init__(self) -> None:
        self.channel = StubChannel(b"SSH-2.0-OpenSSH_10.4\r\n")
        self.attempts = 0

    def is_active(self) -> bool:
        return True

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
        self.commands: list[list[str]] = []
        self.directories: list[PurePosixPath] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        pass

    def interactive_connect(self) -> None:
        pass

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

        yield StubSFTP()

    def run(self, args: list[str]) -> str:
        self.commands.append(args)
        return ""

    def run_login_shell(self, args: list[str]) -> str:
        self.commands.append(["bash", "-lc", *args])
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

    with patch("ezhpcy.cli.compute.time.sleep"):
        info = _wait_for_running_job(
            scheduler,  # type: ignore[arg-type]
            "42",
            timeout_seconds=10,
            state_handler=lambda item: transitions.append(item.state),
        )

    assert info.primary_host == "node42"
    assert transitions == [JobState.PENDING, JobState.RUNNING]


def test_wait_for_running_job_fails_when_job_exits() -> None:
    scheduler = StubScheduler([snapshot(JobState.FAILED, "EXIT")])

    with pytest.raises(ComputeTunnelError, match="scheduler state EXIT"):
        _wait_for_running_job(
            scheduler,  # type: ignore[arg-type]
            "42",
            timeout_seconds=10,
        )


def test_select_worker_ports_returns_unique_dynamic_ports() -> None:
    with patch(
        "ezhpcy.cli.compute.secrets.randbelow",
        side_effect=[0, 0, 1, 2, 3, 4],
    ):
        ports = _select_worker_ports(5)

    assert ports == (49152, 49153, 49154, 49155, 49156)


def test_worker_output_reports_selected_retry_port() -> None:
    process = OutputProcess(
        [b"bind failed\nez", b"hpcy: worker sshd selected port 54322\n"]
    )
    job = InteractiveJob("42", process, "")
    selected_ports: queue.Queue[int] = queue.Queue()

    _drain_interactive_job(job, threading.Event(), selected_ports)

    assert (
        _wait_for_selected_worker_port(job, selected_ports, timeout_seconds=1) == 54322
    )


def test_worker_port_wait_fails_when_all_binds_fail() -> None:
    job = InteractiveJob("42", OutputProcess([]), "")

    with pytest.raises(ComputeTunnelError, match="failed to bind any candidate"):
        _wait_for_selected_worker_port(job, queue.Queue(), timeout_seconds=1)


def test_worker_endpoint_waits_for_an_ssh_banner() -> None:
    transport = StubTransport()
    transport_logger = logging.getLogger("paramiko.transport")
    previous_level = transport_logger.level
    transport_logger.setLevel(logging.WARNING)

    try:
        with patch("ezhpcy.cli.compute.time.sleep"):
            _wait_for_worker_endpoint(
                transport,  # type: ignore[arg-type]
                ("node42", 54321),
                timeout_seconds=10,
            )

        assert transport.attempts == 2
        assert transport.channel.closed
        assert transport_logger.level == logging.WARNING
    finally:
        transport_logger.setLevel(previous_level)


def test_compute_tunnel_submits_worker_starts_broker_and_cancels() -> None:
    transport = StubTransport()
    ssh = StubSSH(transport)
    scheduler = StubScheduler([snapshot(JobState.RUNNING, "RUN", "node42")])
    brokers: list[StubBroker] = []

    def make_broker(*args, **kwargs) -> StubBroker:
        broker = StubBroker(*args, **kwargs)
        brokers.append(broker)
        return broker

    with (
        patch(
            "ezhpcy.cli.compute.InteractiveSSHClient",
            return_value=ssh,
        ),
        patch("ezhpcy.cli.compute.local_machine_id", return_value="machine-id"),
        patch(
            "ezhpcy.cli.compute.LSFScheduler",
            return_value=scheduler,
        ) as scheduler_constructor,
        patch(
            "ezhpcy.cli.compute.provision_worker_infrastructure",
            return_value=None,
        ),
        patch(
            "ezhpcy.cli.compute.read_ed25519_public_key",
            return_value=("ssh-ed25519", "WORKERKEY"),
        ),
        patch(
            "ezhpcy.cli.compute._wait_for_selected_worker_port",
            return_value=54322,
        ),
        patch("ezhpcy.cli.compute._wait_for_worker_endpoint"),
        patch("ezhpcy.cli.compute.create_broker_backend", return_value=object()),
        patch("ezhpcy.cli.compute.ForegroundBroker", side_effect=make_broker),
        patch("ezhpcy.cli.compute.logger") as logger,
    ):
        _run_compute_tunnel(
            profile_name="default",
            profile=lsf_profile(),
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
            worker_ports=(54321, 54322),
            auto_provision=True,
        )

    assert len(scheduler.submitted) == 1
    spec = scheduler.submitted[0]
    assert tuple(spec.command) == _worker_sshd_command(
        ssh.get_remote_state(),
        host_key=PurePosixPath(
            f"/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/ssh/machine-id/"
            "alice@login.example.com/ssh_host_ed25519_key"
        ),
        remote_username="alice",
        authorized_key=("ssh-ed25519", "WORKERKEY"),
        ports=(54321, 54322),
    )
    assert "AuthorizedKeysCommand=/bin/echo ssh-ed25519 WORKERKEY" in spec.command
    assert not any("payload" in argument for argument in spec.command)
    assert spec.queue == "normal"
    assert spec.memory_bytes == 2048 * _MEBIBYTE
    assert spec.cores == 32
    assert spec.gpus == 2
    assert spec.exclusive
    assert scheduler_constructor.call_args.kwargs["resource_reserve_per_task"]
    assert ssh.directories == [
        PurePosixPath(f"/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/worker_cwd"),
        PurePosixPath(f"/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/logs/worker"),
    ]
    assert spec.working_directory == PurePosixPath(
        f"/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/worker_cwd"
    )
    assert spec.stdout_path == PurePosixPath(
        f"/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/logs/worker/worker-%J.out"
    )
    assert spec.stderr_path == PurePosixPath(
        f"/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/logs/worker/worker-%J.err"
    )
    assert brokers[0].destination == ("node42", 54322)
    assert brokers[0].closed
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
        "ezhpcy-worker",
        "node42",
        54322,
    )


def test_compute_tunnel_cancels_job_when_worker_startup_fails() -> None:
    transport = StubTransport()
    ssh = StubSSH(transport)
    scheduler = StubScheduler([snapshot(JobState.RUNNING, "RUN", "node42")])

    with (
        patch(
            "ezhpcy.cli.compute.InteractiveSSHClient",
            return_value=ssh,
        ),
        patch("ezhpcy.cli.compute.local_machine_id", return_value="machine-id"),
        patch(
            "ezhpcy.cli.compute.LSFScheduler",
            return_value=scheduler,
        ),
        patch(
            "ezhpcy.cli.compute.provision_worker_infrastructure",
            return_value=None,
        ),
        patch(
            "ezhpcy.cli.compute.read_ed25519_public_key",
            return_value=("ssh-ed25519", "WORKERKEY"),
        ),
        patch(
            "ezhpcy.cli.compute._wait_for_selected_worker_port",
            return_value=54321,
        ),
        patch(
            "ezhpcy.cli.compute._wait_for_worker_endpoint",
            side_effect=ComputeTunnelError("worker did not listen"),
        ),
        pytest.raises(ComputeTunnelError, match="did not listen"),
    ):
        _run_compute_tunnel(
            profile_name="default",
            profile=lsf_profile(),
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
            worker_ports=(54321,),
            auto_provision=True,
        )

    assert scheduler.cancelled == ["42"]
    assert scheduler.process.closed


def test_compute_tunnel_uses_explicit_pbs_and_linuxsh_defaults() -> None:
    transport = StubTransport()
    ssh = StubSSH(transport)
    scheduler = StubScheduler([snapshot(JobState.RUNNING, "R", "node42")])

    with (
        patch("ezhpcy.cli.compute.InteractiveSSHClient", return_value=ssh),
        patch("ezhpcy.cli.compute.local_machine_id", return_value="machine-id"),
        patch(
            "ezhpcy.cli.compute.PBSScheduler", return_value=scheduler
        ) as scheduler_constructor,
        patch(
            "ezhpcy.cli.compute.provision_worker_infrastructure",
            return_value=None,
        ),
        patch(
            "ezhpcy.cli.compute.read_ed25519_public_key",
            return_value=("ssh-ed25519", "WORKERKEY"),
        ),
        patch(
            "ezhpcy.cli.compute._wait_for_selected_worker_port",
            return_value=54321,
        ),
        patch("ezhpcy.cli.compute._wait_for_worker_endpoint"),
        patch("ezhpcy.cli.compute.create_broker_backend", return_value=object()),
        patch("ezhpcy.cli.compute.ForegroundBroker", StubBroker),
        patch("ezhpcy.cli.compute.logger") as logger,
    ):
        _run_compute_tunnel(
            profile_name="pbs",
            profile=pbs_profile(),
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
            worker_ports=(54321,),
            auto_provision=True,
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
    assert not any(
        log_call.args[0].startswith("Worker logs:")
        for log_call in logger.info.call_args_list
    )


def test_compute_tunnel_help_exposes_scheduler_and_resource_options() -> None:
    result = CliRunner().invoke(app, ["compute", "--help"], terminal_width=160)
    alias_result = CliRunner().invoke(app, ["c", "--help"], terminal_width=160)

    assert result.exit_code == 0
    assert alias_result.exit_code == 0
    assert "--scheduler" in result.stdout
    assert "--queue" in result.stdout
    assert "--cores" in result.stdout
    assert "--gpus" in result.stdout
    assert "--exclusive" in result.stdout
    assert "--time-limit" in result.stdout
    assert "--memory" in result.stdout
    assert "--queue-timeout" in result.stdout
    assert "--startup-timeout" in result.stdout
    assert "--worker-port" in result.stdout
    # Rich abbreviates long option names in its fixed-width option column.
    assert "--worker-port-retr" in result.stdout
    assert "--auto-provision" in result.stdout
    assert "--no-auto-provision" in result.stdout
    assert "--profile" in result.stdout


def test_compute_command_resolves_profile_and_applies_cli_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(compute_module.config, "default_profile", "default")
    monkeypatch.setattr(
        compute_module.config,
        "profile",
        {
            "default": ProfileConfig(
                host="login.example.com",
                user="alice",
                scheduler="LSF",
                queue="normal",
                cores=4,
                gpus=1,
                exclusive=True,
                time_limit="1:00",
                memory="32GB",
            ),
            "gpu": ProfileConfig(inherit="default", queue="gpu", cores=8),
        },
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        compute_module, "_ensure_local_worker_credentials", lambda _connection: None
    )
    monkeypatch.setattr(
        compute_module,
        "_run_compute_tunnel",
        lambda **kwargs: captured.update(kwargs),
    )

    result = CliRunner().invoke(
        app,
        [
            "compute",
            "--profile",
            "gpu",
            "--cores",
            "12",
            "--memory",
            "64GB",
            "--shared",
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
        host="login.example.com", user="alice"
    )
    assert captured["auto_provision"] is True
    worker_ports = captured["worker_ports"]
    assert isinstance(worker_ports, tuple)
    assert len(worker_ports) == 6
    assert len(set(worker_ports)) == 6

    captured.clear()
    result = CliRunner().invoke(
        app,
        [
            "compute",
            "--worker-port",
            "55000",
            "--worker-port-retries",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["worker_ports"] == (55000,)


def test_compute_command_can_disable_auto_provision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(compute_module.config, "default_profile", "default")
    monkeypatch.setattr(
        compute_module.config,
        "profile",
        {
            "default": ProfileConfig(
                host="login.example.com", user="alice", scheduler="LSF"
            )
        },
    )
    credentials_checked = False
    captured: dict[str, object] = {}

    def check_credentials(_connection: ConnectionInfo) -> None:
        nonlocal credentials_checked
        credentials_checked = True

    monkeypatch.setattr(
        compute_module, "_ensure_local_worker_credentials", check_credentials
    )
    monkeypatch.setattr(
        compute_module,
        "_run_compute_tunnel",
        lambda **kwargs: captured.update(kwargs),
    )

    result = CliRunner().invoke(app, ["compute", "--no-auto-provision"])

    assert result.exit_code == 0, result.output
    assert credentials_checked
    assert captured["auto_provision"] is False


def test_compute_command_can_enable_auto_provision_when_config_disables_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(compute_module.config, "default_profile", "default")
    monkeypatch.setattr(compute_module.config, "auto_provision", False)
    monkeypatch.setattr(
        compute_module.config,
        "profile",
        {
            "default": ProfileConfig(
                host="login.example.com", user="alice", scheduler="LSF"
            )
        },
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        compute_module,
        "_run_compute_tunnel",
        lambda **kwargs: captured.update(kwargs),
    )

    result = CliRunner().invoke(app, ["compute", "--auto-provision"])

    assert result.exit_code == 0, result.output
    assert captured["auto_provision"] is True


def test_compute_command_rejects_conflicting_auto_provision_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(compute_module.config, "default_profile", "default")
    monkeypatch.setattr(
        compute_module.config,
        "profile",
        {
            "default": ProfileConfig(
                host="login.example.com", user="alice", scheduler="LSF"
            )
        },
    )

    result = CliRunner().invoke(
        app,
        ["compute", "--auto-provision", "--no-auto-provision"],
    )

    assert result.exit_code == 2
    assert "cannot be used together" in result.output


def test_compute_command_rejects_unknown_profile_before_starting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(compute_module.config, "default_profile", "default")
    monkeypatch.setattr(
        compute_module.config,
        "profile",
        {"default": ProfileConfig(host="login.example.com", user="alice")},
    )
    started = False

    def start(_connection: ConnectionInfo) -> None:
        nonlocal started
        started = True

    monkeypatch.setattr(compute_module, "_ensure_local_worker_credentials", start)

    result = CliRunner().invoke(app, ["compute", "--profile", "missing"])

    assert result.exit_code == 2
    assert "unknown profile 'missing'" in result.stderr
    assert not started
