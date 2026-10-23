"""
Characterization of how `ezhpcy tunnel` shuts down in each failure scenario,
against the in-process SSH server and a scheduler fake. Among the outcomes:

- Each way of stopping logs one `Stopping job: <why>` line, which the table
  checks.
- A failure after the tunnel was ready reads `Tunnel stopped: …`, not `Could not
  start tunnel: …`.
- When the login connection is lost while waiting for the job, the user sees that
  loss rather than the failed scheduler query that noticed it first.
- Of two failures close together, the first one wins: a job-monitor error that
  comes before a lost connection is the one reported.
- A PBS job that the scheduler no longer lists has ended, so it isn't cancelled.
"""

import functools
import re
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import pytest

from ezhpcy.cli import tunnel as tunnel_cli
from ezhpcy.job import publish
from ezhpcy.scheduler.base import JobInfo, JobSpec, JobState, SchedulerError
from ezhpcy.ssh import RemoteHost
from ezhpcy.tunnel.lock import alias_locked
from ezhpcy.worker.control import WorkerControl
from tests.support.cli import invoke
from tests.support.sshserver import SSHTestServer
from tests.support.tunnel import (
    FAST_TIMINGS,
    JOB_ID,
    RUNNING,
    FakeScheduler,
    banner,
    configure_tunnel,
    serve_interactive_job,
    snapshot,
    tunnel_log,
    unused_port,
    use_scheduler,
    write_worker_status,
)

PENDING = snapshot(JobState.PENDING, "PEND")


class Rig:
    """The fakes of one scenario, and the flag its steps set."""

    def __init__(
        self,
        scenario: Scenario,
        server: SSHTestServer,
        worker: SSHTestServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self.scenario = scenario
        self.server = server
        self.heartbeats_fail = scenario.heartbeats == "fail_from_start"
        self.published: list[str] = []
        self.withdrawn: list[str] = []
        if scenario.endpoint_refuses:
            self.worker_port = unused_port()
        elif scenario.banner is not None:
            self.worker_port = server.listen(banner(scenario.banner))
        else:
            self.worker_port = worker.port
        self.runs: list[tunnel_cli.TunnelRun] = []
        init = tunnel_cli.TunnelRun.__init__

        def recording_init(run: tunnel_cli.TunnelRun, *args: Any) -> None:
            init(run, *args)
            self.runs.append(run)

        monkeypatch.setattr(tunnel_cli.TunnelRun, "__init__", recording_init)

    def scheduler(self, host: RemoteHost) -> FakeScheduler:
        inspections = [
            functools.partial(entry, self) if callable(entry) else entry
            for entry in self.scenario.inspections
        ]
        # A scenario that publishes its Host block plays the job monitor's polls
        # once the tunnel serves: its 10 ms interval could otherwise beat it.
        return FakeScheduler(
            host,
            inspections,
            submitted=self.start_worker,
            held=lambda: self.scenario.withdrawn and not self.serving(),
        )

    def serving(self) -> bool:
        return any(run.served for run in self.runs)

    def start_worker(self, _spec: JobSpec) -> None:
        status = self.scenario.worker_status
        if status == "ready":
            write_worker_status(self.server, "ready", str(self.worker_port))
        elif status is not None:
            write_worker_status(self.server, *status.split())


type Step = Callable[[Rig], JobInfo]


def interrupt_while(info: JobInfo) -> Step:
    """Press Ctrl+C, then report `info`."""

    def step(_rig: Rig) -> JobInfo:
        signal.raise_signal(signal.SIGINT)
        return info

    return step


def lose_connection_while(info: JobInfo) -> Step:
    def step(rig: Rig) -> JobInfo:
        rig.server.drop()
        return info

    return step


def fail_heartbeats(rig: Rig) -> JobInfo:
    rig.heartbeats_fail = True
    return RUNNING


def stop_sshd(rig: Rig) -> JobInfo:
    write_worker_status(rig.server, "stopped", "sshd", "255")
    return RUNNING


def fail_then_lose_connection(rig: Rig) -> JobInfo:
    # The client can't see the connection go before the error is handled: nothing
    # awaits in between.
    rig.server.drop()
    raise SchedulerError("bjobs failed")


@dataclass(frozen=True)
class Scenario:
    id: str
    # What the scheduler reports on each poll; the last entry repeats. The first
    # poll that reports RUNNING ends the wait for the job; the job monitor's polls
    # come after it.
    inspections: list[JobInfo | Exception | Step]
    exit_code: int
    # Matches the start of the error the command reports (a regular expression);
    # None if it reports none.
    message: str | None
    cancelled: bool
    withdrawn: bool
    # Why the tunnel stopped, in its `Stopping job: <why>` line, if it didn't
    # fail; a failure's `why` is its error.
    why: str | None = None
    # The status the worker writes once submitted: "ready" (with the worker's
    # port), another state and its fields, or None for none.
    worker_status: str | None = "ready"
    heartbeats: Literal["ok", "fail_from_start"] = "ok"
    endpoint_refuses: bool = False
    # What the worker's port sends, if it isn't the worker's sshd.
    banner: bytes | None = None
    timings: dict[str, float] = field(default_factory=dict)


SCENARIOS = [
    Scenario(
        id="ctrl_c_while_serving",
        inspections=[RUNNING, interrupt_while(RUNNING)],
        exit_code=0,
        message=None,
        why="Ctrl+C",
        cancelled=True,
        withdrawn=True,
    ),
    Scenario(
        id="login_connection_lost_while_serving",
        inspections=[RUNNING, lose_connection_while(RUNNING)],
        exit_code=1,
        message="Tunnel stopped: login-node SSH connection lost: ",
        cancelled=True,
        withdrawn=True,
    ),
    Scenario(
        # The loss is reported, not the failed scheduler query that noticed it.
        id="login_connection_lost_while_waiting_for_the_job",
        inspections=[lose_connection_while(PENDING), PENDING],
        exit_code=1,
        message="Could not start tunnel: login-node SSH connection lost: ",
        cancelled=True,
        withdrawn=False,
    ),
    Scenario(
        id="heartbeat_write_fails_while_serving",
        inspections=[RUNNING, fail_heartbeats],
        exit_code=1,
        message="Tunnel stopped: worker heartbeat write failed for job 42",
        cancelled=True,
        withdrawn=True,
    ),
    Scenario(
        id="heartbeat_write_fails_during_the_endpoint_wait",
        inspections=[RUNNING],
        heartbeats="fail_from_start",
        endpoint_refuses=True,
        exit_code=1,
        message="Could not start tunnel: worker heartbeat write failed for job 42",
        cancelled=True,
        withdrawn=False,
    ),
    Scenario(
        id="job_monitor_sees_a_terminal_state",
        inspections=[RUNNING, snapshot(JobState.SUCCEEDED, "DONE")],
        exit_code=0,
        message=None,
        why="worker job 42 ended in scheduler state DONE",
        cancelled=False,
        withdrawn=True,
    ),
    Scenario(
        # PBS no longer lists the job: it has ended.
        id="job_monitor_sees_that_pbs_dropped_the_job",
        inspections=[RUNNING, snapshot(JobState.ENDED, "")],
        exit_code=0,
        message=None,
        why="worker job 42 ended",
        cancelled=False,
        withdrawn=True,
    ),
    Scenario(
        id="job_monitor_gets_a_scheduler_error",
        inspections=[RUNNING, SchedulerError("bjobs failed")],
        exit_code=1,
        message="Tunnel stopped: could not monitor worker job 42: bjobs failed",
        cancelled=True,
        withdrawn=True,
    ),
    Scenario(
        id="job_monitor_sees_unknown",
        inspections=[RUNNING, snapshot(JobState.UNKNOWN, "ZOMBI")],
        exit_code=1,
        message="Tunnel stopped: worker job 42 entered unknown scheduler state ZOMBI",
        cancelled=True,
        withdrawn=True,
    ),
    Scenario(
        id="job_never_starts",
        inspections=[PENDING],
        timings={"queue_timeout_seconds": 0.1},
        exit_code=1,
        message="Could not start tunnel: worker job 42 did not start within 0.1 seconds",
        cancelled=True,
        withdrawn=False,
    ),
    Scenario(
        id="worker_writes_a_failed_status",
        inspections=[RUNNING],
        worker_status="failed port 54321 is in use",
        exit_code=1,
        message=(
            "Could not start tunnel: worker SSH daemon failed to start: "
            "port 54321 is in use"
        ),
        cancelled=True,
        withdrawn=False,
    ),
    Scenario(
        # New: the tunnel waited out the startup timeout, then cancelled the job.
        id="worker_exits_before_writing_a_status",
        inspections=[RUNNING, snapshot(JobState.FAILED, "EXIT")],
        worker_status=None,
        exit_code=1,
        message="Could not start tunnel: worker job 42 ended in scheduler state EXIT",
        cancelled=False,
        withdrawn=False,
    ),
    Scenario(
        # New: likewise.
        id="worker_job_ends_during_the_endpoint_wait",
        inspections=[RUNNING, snapshot(JobState.FAILED, "EXIT")],
        endpoint_refuses=True,
        exit_code=1,
        message="Could not start tunnel: worker job 42 ended in scheduler state EXIT",
        cancelled=False,
        withdrawn=False,
    ),
    Scenario(
        # New: sshd exits after `ready`; the worker's `stopped` status says so.
        id="worker_sshd_stops_during_the_endpoint_wait",
        inspections=[RUNNING, stop_sshd, RUNNING],
        endpoint_refuses=True,
        exit_code=1,
        message="Could not start tunnel: worker SSH daemon exited with status 255",
        cancelled=True,
        withdrawn=False,
    ),
    Scenario(
        id="worker_banner_never_appears",
        inspections=[RUNNING],
        banner=b"not an ssh server\n",
        timings={"worker_startup_timeout_seconds": 0.2},
        exit_code=1,
        message=(
            r"Could not start tunnel: worker SSH endpoint 127\.0\.0\.1:\d+ did not "
            r"become ready within 0\.2 seconds"
        ),
        cancelled=True,
        withdrawn=False,
    ),
    Scenario(
        id="ctrl_c_during_startup",
        inspections=[interrupt_while(PENDING), PENDING],
        exit_code=0,
        message=None,
        why="Ctrl+C",
        cancelled=True,
        withdrawn=False,
    ),
    Scenario(
        # The first failure wins: the monitor's, not the connection's.
        id="monitor_error_then_connection_lost",
        inspections=[RUNNING, fail_then_lose_connection],
        exit_code=1,
        message="Tunnel stopped: could not monitor worker job 42: bjobs failed",
        cancelled=True,
        withdrawn=True,
    ),
]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario.id)
def test_tunnel_shutdown(
    scenario: Scenario,
    login: SSHTestServer,
    worker: SSHTestServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = Rig(scenario, login, worker, monkeypatch)
    configure_tunnel(monkeypatch, login, timings=FAST_TIMINGS | scenario.timings)
    serve_interactive_job(login)
    schedulers = use_scheduler(monkeypatch, rig.scheduler)
    monkeypatch.setattr(
        publish, "publish_host", lambda host, _port: rig.published.append(host.alias)
    )
    monkeypatch.setattr(
        publish, "withdraw_host", lambda host: rig.withdrawn.append(host.alias)
    )

    publish_lease = WorkerControl.publish_lease
    lease_calls = 0

    async def failing_publish_lease(control: WorkerControl) -> int:
        nonlocal lease_calls
        lease_calls += 1
        # The first lease is written before submission; later ones are heartbeats.
        if lease_calls > 1 and rig.heartbeats_fail:
            raise OSError("sftp write failed")
        return await publish_lease(control)

    monkeypatch.setattr(WorkerControl, "publish_lease", failing_publish_lease)

    with tunnel_log() as log:
        result = invoke(["tunnel", "base", "--worker-port", str(rig.worker_port)])

    assert result.exit_code == scenario.exit_code, result
    assert result.exception is None, result
    errors = log.command_errors()
    [stopping] = log.starting("Stopping job: ")
    why = stopping.removeprefix("Stopping job: ")
    if scenario.message is None:
        assert errors == []
        assert why == scenario.why
    else:
        [error] = errors
        assert re.match(scenario.message, error), error
        # Logged when the tunnel decided to stop, and again as the command exits.
        assert error.endswith(f": {why}")

    [scheduler] = schedulers
    assert scheduler.cancelled == ([JOB_ID] if scenario.cancelled else [])
    assert rig.withdrawn == (["base"] if scenario.withdrawn else [])
    # A block is only withdrawn if it was published, and never left behind.
    assert rig.published == rig.withdrawn
    assert not alias_locked("base")
