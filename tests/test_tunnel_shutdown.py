"""
Characterization of how `ezhpcy tunnel` shuts down in each failure scenario.

These pin today's behaviour, quirks included, so that a restructuring of the
tunnel code can be checked against the same table.
"""

import logging
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal
from unittest.mock import patch

import paramiko
import pytest

from ezhpcy.cli import tunnel as tunnel_module
from ezhpcy.constants import EZHPCY_VERSION
from ezhpcy.scheduler.base import (
    InteractiveJob,
    JobInfo,
    JobSpec,
    JobState,
    SchedulerError,
)
from ezhpcy.types import ProfileConfig
from tests.support.cli import invoke
from tests.test_tunnel import (
    StubChannel,
    StubScheduler,
    StubSSH,
    StubTransport,
    StubTunnelServer,
    snapshot,
)

_WORKER_PORT = 54321
_TOKEN = "LEASETOKEN"
_CONTROL_DIR = PurePosixPath(
    f"/home/alice/.cache/ezhpcy/{EZHPCY_VERSION}/worker_control/{_TOKEN}"
)
_WAIT_SECONDS = 5

_FAST_TIMINGS = {
    "queue_timeout_seconds": 5,
    "worker_startup_timeout_seconds": 5,
    "job_poll_interval_seconds": 0.01,
    "job_monitor_interval_seconds": 0.01,
    "worker_heartbeat_interval_seconds": 0.01,
    "worker_heartbeat_timeout_seconds": 1,
}

type Inspection = JobInfo | BaseException | Callable[["Rig"], JobInfo]


class Rig:
    """The fakes of one scenario, and the events its steps coordinate with."""

    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.transport = (
            _RefusingTransport() if scenario.endpoint_refuses else StubTransport()
        )
        if scenario.banner is not None:
            self.transport.channel = StubChannel(scenario.banner)
        self.ssh = StubSSH(self.transport)
        self.scheduler = _RigScheduler(self, scenario.inspections)
        self.tunnel: _RigTunnel | None = None
        self.heartbeats_fail = threading.Event()
        self.monitor_may_fail = threading.Event()
        self.published: list[str] = []
        self.withdrawn: list[str] = []
        if scenario.heartbeats == "fail_from_start":
            self.heartbeats_fail.set()
        if scenario.worker_record is not None:
            self.ssh.files[_CONTROL_DIR / scenario.worker_record.name] = (
                f"v1 {_TOKEN} {scenario.worker_record.name} "
                f"{scenario.worker_record.detail}"
            ).strip()

    def lose_connection(self, reason: str) -> None:
        handler = self.ssh.connection_lost_handler
        assert handler is not None
        handler(paramiko.SSHException(reason))

    def make_tunnel(self, *args, **kwargs) -> _RigTunnel:
        self.tunnel = _RigTunnel(self, *args, **kwargs)
        return self.tunnel


class _RefusingTransport(StubTransport):
    """A transport on which the worker endpoint never accepts a connection."""

    def open_channel(self, _kind: str, **_kwargs):
        raise paramiko.SSHException("connection refused")


class _RigScheduler(StubScheduler):
    """Replays `inspections` in order and repeats the last one."""

    def __init__(self, rig: Rig, inspections: list[Inspection]) -> None:
        super().__init__([])
        self._rig = rig
        self._inspections = list(inspections)

    def submit_interactive(
        self, spec: JobSpec, *, startup_timeout: float
    ) -> InteractiveJob:
        # The shared stub pins the startup timeout, which scenarios vary.
        return super().submit_interactive(spec, startup_timeout=10)

    def inspect(self, _job_id: str) -> JobInfo:
        entry = (
            self._inspections.pop(0)
            if len(self._inspections) > 1
            else self._inspections[0]
        )
        if isinstance(entry, BaseException):
            raise entry
        if callable(entry):
            return entry(self._rig)
        return entry


class _RigTunnel(StubTunnelServer):
    def __init__(self, rig: Rig, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.rig = rig
        self.closed_event = threading.Event()

    def serve_forever(self) -> None:
        self.rig.scenario.serve(self.rig, self)

    def close(self) -> None:
        super().close()
        self.closed_event.set()

    def wait_until_closed(self) -> None:
        assert self.closed_event.wait(_WAIT_SECONDS), "the tunnel was never closed"


def return_at_once(_rig: Rig, _tunnel: _RigTunnel) -> None:
    pass


def block_until_closed(_rig: Rig, tunnel: _RigTunnel) -> None:
    tunnel.wait_until_closed()


def interrupt(_rig: Rig, _tunnel: _RigTunnel) -> None:
    raise KeyboardInterrupt


def lose_connection_then_return(rig: Rig, _tunnel: _RigTunnel) -> None:
    rig.lose_connection("session stopped answering")


def fail_heartbeats_and_block(rig: Rig, tunnel: _RigTunnel) -> None:
    rig.heartbeats_fail.set()
    tunnel.wait_until_closed()


def fail_monitor_then_lose_connection(rig: Rig, tunnel: _RigTunnel) -> None:
    # The monitor's error closes the tunnel before the connection is reported
    # lost; both are recorded.
    rig.monitor_may_fail.set()
    tunnel.wait_until_closed()
    rig.lose_connection("session stopped answering")


def lose_connection_then_fail_inspection(rig: Rig) -> JobInfo:
    rig.lose_connection("session stopped answering")
    raise paramiko.SSHException("socket is closed")


def fail_once_allowed(rig: Rig) -> JobInfo:
    if rig.monitor_may_fail.is_set():
        raise SchedulerError("bjobs failed")
    return RUNNING


@dataclass(frozen=True)
class WorkerRecord:
    name: Literal["ready", "failed"]
    detail: str


RUNNING = snapshot(JobState.RUNNING, "RUN", "node42")
_READY = WorkerRecord("ready", str(_WORKER_PORT))


@dataclass(frozen=True)
class Scenario:
    id: str
    # What the scheduler reports on each poll; the last entry repeats.
    inspections: list[Inspection]
    expected_reason: str
    exit_code: int
    # A substring of the error logged for the user; None if none is logged.
    message: str | None
    cancelled: bool
    withdrawn: bool
    serve: Callable[[Rig, _RigTunnel], None] = return_at_once
    worker_record: WorkerRecord | None = _READY
    heartbeats: Literal["ok", "fail_from_start", "fail_when_serving"] = "ok"
    endpoint_refuses: bool = False
    banner: bytes | None = None
    timings: dict[str, float] = field(default_factory=dict)
    info: str = ""


SCENARIOS = [
    Scenario(
        id="ctrl_c_while_serving",
        inspections=[RUNNING],
        serve=interrupt,
        expected_reason="user_interrupt",
        exit_code=0,
        message=None,
        cancelled=True,
        withdrawn=True,
    ),
    Scenario(
        id="login_connection_lost_while_serving",
        inspections=[RUNNING],
        serve=lose_connection_then_return,
        expected_reason="login_connection_lost",
        exit_code=1,
        message="login-node SSH connection lost: session stopped answering",
        cancelled=True,
        withdrawn=True,
    ),
    Scenario(
        # The handler's own error is dropped here: the failed inspection raises
        # first, so the user sees the raw paramiko error.
        id="login_connection_lost_while_waiting_for_the_job",
        inspections=[lose_connection_then_fail_inspection],
        expected_reason="login_connection_lost",
        exit_code=1,
        message="socket is closed",
        cancelled=True,
        withdrawn=False,
    ),
    Scenario(
        id="heartbeat_write_fails_while_serving",
        inspections=[RUNNING],
        serve=fail_heartbeats_and_block,
        heartbeats="fail_when_serving",
        expected_reason="heartbeat_send_failed",
        exit_code=1,
        message="worker heartbeat write failed for job 42",
        cancelled=True,
        withdrawn=True,
    ),
    Scenario(
        id="heartbeat_write_fails_during_the_endpoint_wait",
        inspections=[RUNNING],
        heartbeats="fail_from_start",
        endpoint_refuses=True,
        expected_reason="heartbeat_send_failed",
        exit_code=1,
        message="worker heartbeat write failed for job 42",
        cancelled=True,
        withdrawn=False,
    ),
    Scenario(
        id="job_monitor_sees_a_terminal_state",
        inspections=[RUNNING, snapshot(JobState.SUCCEEDED, "DONE")],
        serve=block_until_closed,
        expected_reason="worker_finished",
        exit_code=0,
        message=None,
        cancelled=False,
        withdrawn=True,
    ),
    Scenario(
        id="job_monitor_gets_a_scheduler_error",
        inspections=[RUNNING, SchedulerError("bjobs failed")],
        serve=block_until_closed,
        expected_reason="job_monitor_error",
        exit_code=1,
        message="could not monitor worker job 42: bjobs failed",
        cancelled=True,
        withdrawn=True,
    ),
    Scenario(
        id="job_monitor_sees_unknown",
        inspections=[RUNNING, snapshot(JobState.UNKNOWN, "ZOMBI")],
        serve=block_until_closed,
        expected_reason="job_monitor_error",
        exit_code=1,
        message="worker job 42 entered unknown scheduler state ZOMBI",
        cancelled=True,
        withdrawn=True,
    ),
    Scenario(
        id="job_never_starts",
        inspections=[snapshot(JobState.PENDING, "PEND")],
        timings={"queue_timeout_seconds": 0.1},
        expected_reason="starting_worker",
        exit_code=1,
        message="worker job 42 did not start within 0.1 seconds",
        cancelled=True,
        withdrawn=False,
    ),
    Scenario(
        id="worker_writes_a_failed_record",
        inspections=[RUNNING],
        worker_record=WorkerRecord("failed", "port 54321 is in use"),
        expected_reason="starting_worker",
        exit_code=1,
        message="worker SSH daemon failed to start: port 54321 is in use",
        cancelled=True,
        withdrawn=False,
    ),
    Scenario(
        id="worker_banner_never_appears",
        inspections=[RUNNING],
        banner=b"not an ssh server\n",
        timings={"worker_startup_timeout_seconds": 0.2},
        expected_reason="starting_worker",
        exit_code=1,
        message="did not become ready within 0.2 seconds",
        cancelled=True,
        withdrawn=False,
    ),
    Scenario(
        id="ctrl_c_during_startup",
        inspections=[KeyboardInterrupt()],
        expected_reason="starting_worker",
        exit_code=0,
        message=None,
        cancelled=True,
        withdrawn=False,
    ),
    Scenario(
        # The monitor's error closes the tunnel, then the connection is lost.
        # Today the connection error wins; the monitor's reaches only the log
        # and the reason stays "login_connection_lost".
        id="connection_lost_and_monitor_error_together",
        inspections=[RUNNING, fail_once_allowed],
        serve=fail_monitor_then_lose_connection,
        expected_reason="login_connection_lost",
        exit_code=1,
        message="login-node SSH connection lost: session stopped answering",
        cancelled=True,
        withdrawn=True,
    ),
]


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def tunnel_log() -> Iterator[_Records]:
    """Collect the tunnel module's log records, which `ezhpcy` does not propagate."""
    logger = logging.getLogger(tunnel_module.__name__)
    handler = _Records()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario.id)
def test_tunnel_shutdown(
    scenario: Scenario,
    monkeypatch: pytest.MonkeyPatch,
    tunnel_log: _Records,
) -> None:
    rig = Rig(scenario)
    monkeypatch.setattr(
        tunnel_module.config,
        "profile",
        {
            "base": ProfileConfig.model_validate(
                {
                    "connection": {"host": "login.example.com", "user": "alice"},
                    "scheduler": {"type": "LSF", "submission_mode": "interactive"},
                    "timings": {**_FAST_TIMINGS, **scenario.timings},
                }
            )
        },
    )
    monkeypatch.setattr(tunnel_module.config, "auto_provision", True)
    monkeypatch.setattr(tunnel_module, "profile_hosts", list)
    monkeypatch.setattr(tunnel_module, "write_profiles_config", lambda _hosts: None)
    monkeypatch.setattr(
        tunnel_module, "_select_worker_ports", lambda _n: (_WORKER_PORT,)
    )
    monkeypatch.setattr(
        tunnel_module,
        "_publish_ssh_host",
        lambda host, **_kwargs: rig.published.append(host.alias),
    )
    monkeypatch.setattr(
        tunnel_module,
        "_withdraw_ssh_host",
        lambda host, **_kwargs: rig.withdrawn.append(host.alias),
    )

    real_publish_lease = tunnel_module.WorkerControl.publish_lease
    lease_calls = 0

    def publish_lease(control: tunnel_module.WorkerControl) -> int:
        nonlocal lease_calls
        lease_calls += 1
        # The first lease is written before submission; later ones come from the
        # heartbeat thread.
        if lease_calls > 1 and rig.heartbeats_fail.is_set():
            raise OSError("sftp write failed")
        return real_publish_lease(control)

    with (
        patch("ezhpcy.cli.tunnel.InteractiveSSHClient", return_value=rig.ssh),
        patch("ezhpcy.cli.tunnel.local_machine_id", return_value="machine-id"),
        patch("ezhpcy.cli.tunnel.secrets.token_hex", return_value=_TOKEN),
        patch("ezhpcy.cli.tunnel.LSFScheduler", return_value=rig.scheduler),
        patch("ezhpcy.cli.tunnel.provision_worker_infrastructure"),
        patch(
            "ezhpcy.cli.tunnel.read_ed25519_public_key",
            return_value=("ssh-ed25519", "WORKERKEY"),
        ),
        patch(
            "ezhpcy.cli.tunnel.create_tunnel_backend",
            return_value=type("Backend", (), {"instance_id": "instance-id"})(),
        ),
        patch("ezhpcy.cli.tunnel.TunnelServer", side_effect=rig.make_tunnel),
        patch.object(tunnel_module.WorkerControl, "publish_lease", publish_lease),
    ):
        result = invoke(["tunnel", "base"])

    cleanup = [
        record.getMessage()
        for record in tunnel_log.records
        if record.getMessage().startswith("Tunnel cleanup:")
    ]
    assert len(cleanup) == 1, (cleanup, result)
    assert re.search(r"reason=(\S+)", cleanup[0]).group(1) == scenario.expected_reason  # type: ignore[union-attr]

    assert result.exit_code == scenario.exit_code, result
    assert result.exception is None, result
    errors = [
        record.getMessage()
        for record in tunnel_log.records
        if record.levelno >= logging.ERROR and "Could not start tunnel" in record.msg
    ]
    if scenario.message is None:
        assert errors == []
    else:
        assert len(errors) == 1, errors
        assert scenario.message in errors[0]

    assert rig.scheduler.cancelled == (["42"] if scenario.cancelled else [])
    assert rig.withdrawn == (["base"] if scenario.withdrawn else [])
    # A block is only withdrawn if it was published, and never left behind.
    assert rig.published == rig.withdrawn
