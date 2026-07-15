import logging
from datetime import timedelta
from pathlib import PurePosixPath
from unittest.mock import patch

import paramiko
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from ezhpcy.cli import app
from ezhpcy.cli.tunnel import compute as compute_module
from ezhpcy.cli.tunnel.compute import (
    ComputeTunnelError,
    _run_compute_tunnel,
    _wait_for_running_job,
    _wait_for_worker_endpoint,
)
from ezhpcy.config import (
    ConnectionInfo,
    RemoteFileConfig,
)
from ezhpcy.scheduler.base import InteractiveJob, JobInfo, JobSpec, JobState
from ezhpcy.scheduler.types import SchedulerType
from ezhpcy.types import ProfileConfig, ResolvedProfileConfig

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


class StubScheduler:
    scheduler_type = SchedulerType.LSF

    def __init__(self, snapshots: list[JobInfo]) -> None:
        self.snapshots = snapshots
        self.submitted: list[JobSpec] = []
        self.cancelled: list[str] = []
        self.payload_starts = 0

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
            payload_starter=lambda: setattr(
                self, "payload_starts", self.payload_starts + 1
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

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        pass

    def interactive_connect(self) -> None:
        pass

    def get_transport(self) -> StubTransport:
        return self.transport

    def get_file_config(self) -> RemoteFileConfig:
        return RemoteFileConfig(
            cache_dir=PurePosixPath("/home/alice/.cache"),
            config_dir=PurePosixPath("/home/alice/.config"),
            data_dir=PurePosixPath("/home/alice/.local/share"),
            runtime_dir=PurePosixPath("/tmp/ezhpcy-1000"),
        )

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

    with patch("ezhpcy.cli.tunnel.compute.time.sleep"):
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


def test_worker_endpoint_waits_for_an_ssh_banner() -> None:
    transport = StubTransport()
    transport_logger = logging.getLogger("paramiko.transport")
    previous_level = transport_logger.level
    transport_logger.setLevel(logging.WARNING)

    try:
        with patch("ezhpcy.cli.tunnel.compute.time.sleep"):
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


def test_compute_tunnel_submits_worker_starts_broker_and_cancels(capsys) -> None:
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
            "ezhpcy.cli.tunnel.compute.InteractiveSSHClient",
            return_value=ssh,
        ),
        patch(
            "ezhpcy.cli.tunnel.compute.LSFScheduler",
            return_value=scheduler,
        ) as scheduler_constructor,
        patch("ezhpcy.cli.tunnel.compute._wait_for_worker_endpoint"),
        patch("ezhpcy.cli.tunnel.compute.create_broker_backend", return_value=object()),
        patch("ezhpcy.cli.tunnel.compute.ForegroundBroker", side_effect=make_broker),
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
            worker_port=54321,
        )

    assert len(scheduler.submitted) == 1
    spec = scheduler.submitted[0]
    assert tuple(spec.command) == ("ezhpcy", "compute", "ssh-serve", "54321")
    assert spec.queue == "normal"
    assert spec.memory_bytes == 2048 * _MEBIBYTE
    assert spec.cores == 32
    assert spec.gpus == 2
    assert spec.exclusive
    assert scheduler_constructor.call_args.kwargs["resource_reserve_per_task"]
    assert spec.working_directory == PurePosixPath("/home/alice/.local/share/ezhpcy")
    assert spec.stdout_path == PurePosixPath(
        "/home/alice/.local/share/ezhpcy/worker-%J.out"
    )
    assert brokers[0].destination == ("node42", 54321)
    assert brokers[0].closed
    assert scheduler.cancelled == ["42"]
    assert scheduler.process.closed
    assert scheduler.payload_starts == 1
    assert ssh.commands == [["ezhpcy", "compute", "ssh-serve", "--help"]]
    assert (
        "Submission command: bsub -Is -q hpcint echo 'hello world'"
        in capsys.readouterr().out
    )


def test_compute_tunnel_cancels_job_when_worker_startup_fails() -> None:
    transport = StubTransport()
    ssh = StubSSH(transport)
    scheduler = StubScheduler([snapshot(JobState.RUNNING, "RUN", "node42")])

    with (
        patch(
            "ezhpcy.cli.tunnel.compute.InteractiveSSHClient",
            return_value=ssh,
        ),
        patch(
            "ezhpcy.cli.tunnel.compute.LSFScheduler",
            return_value=scheduler,
        ),
        patch(
            "ezhpcy.cli.tunnel.compute._wait_for_worker_endpoint",
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
            worker_port=54321,
        )

    assert scheduler.cancelled == ["42"]
    assert scheduler.process.closed


def test_compute_tunnel_uses_explicit_pbs_and_linuxsh_defaults(capsys) -> None:
    transport = StubTransport()
    ssh = StubSSH(transport)
    scheduler = StubScheduler([snapshot(JobState.RUNNING, "R", "node42")])

    with (
        patch("ezhpcy.cli.tunnel.compute.InteractiveSSHClient", return_value=ssh),
        patch(
            "ezhpcy.cli.tunnel.compute.PBSScheduler", return_value=scheduler
        ) as scheduler_constructor,
        patch("ezhpcy.cli.tunnel.compute._wait_for_worker_endpoint"),
        patch("ezhpcy.cli.tunnel.compute.create_broker_backend", return_value=object()),
        patch("ezhpcy.cli.tunnel.compute.ForegroundBroker", StubBroker),
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
            worker_port=54321,
        )

    assert scheduler.submitted[0].cores == 1
    assert scheduler.submitted[0].gpus == 0
    assert not scheduler.submitted[0].exclusive
    assert scheduler_constructor.call_args.kwargs["command_directory"] == PurePosixPath(
        "/opt/pbspro/bin"
    )
    assert scheduler.submitted[0].queue == "workq"
    assert scheduler.submitted[0].memory_bytes is None
    assert scheduler.payload_starts == 1
    output = capsys.readouterr().out
    assert "Using profile 'pbs' with PBS scheduler." in output
    assert "Worker logs:" not in output


def test_compute_tunnel_help_exposes_scheduler_and_resource_options() -> None:
    result = CliRunner().invoke(app, ["tunnel", "compute", "--help"])

    assert result.exit_code == 0
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
    assert "--profile" in result.stdout


def test_compute_command_resolves_profile_and_applies_cli_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(compute_module.get_config(), "default_profile", "default")
    monkeypatch.setattr(
        compute_module.get_config(),
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
        compute_module, "_ensure_local_worker_credentials", lambda: None
    )
    monkeypatch.setattr(
        compute_module,
        "_run_compute_tunnel",
        lambda **kwargs: captured.update(kwargs),
    )

    result = CliRunner().invoke(
        app,
        [
            "tunnel",
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


def test_compute_command_rejects_unknown_profile_before_starting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(compute_module.get_config(), "default_profile", "default")
    monkeypatch.setattr(
        compute_module.get_config(),
        "profile",
        {"default": ProfileConfig(host="login.example.com", user="alice")},
    )
    started = False

    def start() -> None:
        nonlocal started
        started = True

    monkeypatch.setattr(compute_module, "_ensure_local_worker_credentials", start)

    result = CliRunner().invoke(app, ["tunnel", "compute", "--profile", "missing"])

    assert result.exit_code == 2
    assert "unknown profile 'missing'" in result.stderr
    assert not started
