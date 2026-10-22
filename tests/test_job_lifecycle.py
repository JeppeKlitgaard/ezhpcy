"""
The job's lifecycle on its own, against the in-process SSH server, with the
scheduler fake and a worker that only sends sshd's banner: how the job's end
shows to the code that uses it, and its attachments.
"""

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from ezhpcy.cli.utils import ssh as cli_ssh
from ezhpcy.job import JobEnd, JobEnded, JobFailed
from ezhpcy.job.lifecycle import JobLifecycle
from ezhpcy.scheduler.base import JobInfo, JobSpec, JobState, SchedulerError
from ezhpcy.scheduler.types import SchedulerType
from ezhpcy.tunnel import TunnelError
from ezhpcy.tunnel.ssh_config import WorkerHost
from ezhpcy.tunnel.status import Phase, TunnelStatus
from ezhpcy.types import (
    ConnectionInfo,
    ResolvedConfig,
    ResolvedSchedulerConfig,
    SubmissionMode,
    TimingsConfig,
)
from ezhpcy.worker.control import WorkerControl
from tests.support.sshserver import SSHTestServer
from tests.support.tunnel import (
    BANNER,
    FAST_TIMINGS,
    JOB_ID,
    RUNNING,
    FakeScheduler,
    Inspection,
    banner,
    configure_tunnel,
    control_dirs,
    snapshot,
    tunnel_log,
    use_scheduler,
    write_worker_status,
)

DONE = snapshot(JobState.SUCCEEDED, "DONE")
# How long a test waits for something that should happen at once.
WAIT_SECONDS = 5


class Rig:
    """Makes jobs against the login server, and keeps their schedulers."""

    def __init__(self, login: SSHTestServer, monkeypatch: pytest.MonkeyPatch) -> None:
        self.login = login
        self.monkeypatch = monkeypatch
        self.worker_port = login.listen(banner(BANNER))
        self.schedulers: list[FakeScheduler] = []
        configure_tunnel(monkeypatch, login)

    def job(self, inspections: list[Inspection], **scheduler: Any) -> JobLifecycle:
        """A batch job whose scheduler reports `inspections`, as `FakeScheduler`."""

        def make(host: Any) -> FakeScheduler:
            return FakeScheduler(
                host, inspections, submitted=self.start_worker, **scheduler
            )

        self.schedulers = use_scheduler(self.monkeypatch, make)
        conn_info = ConnectionInfo(user="alice", host="login.example.com")
        return JobLifecycle(
            resolved=ResolvedConfig(
                connection=conn_info,
                scheduler=ResolvedSchedulerConfig(
                    type=SchedulerType.LSF, submission_mode=SubmissionMode.BATCH
                ),
                timings=TimingsConfig.model_validate(FAST_TIMINGS),
            ),
            credentials=WorkerHost.for_endpoint(
                "base", user="alice", host="login.example.com"
            ).credentials,
            status=TunnelStatus(alias="base", login="alice@login.example.com"),
            connect=cli_ssh.connect_interactive,
            worker_ports=(self.worker_port,),
            auto_provision=True,
        )

    def start_worker(self, _spec: JobSpec) -> None:
        write_worker_status(self.login, "ready", str(self.worker_port))

    @property
    def cancelled(self) -> list[str]:
        [scheduler] = self.schedulers
        return scheduler.cancelled


@pytest.fixture
def rig(login: SSHTestServer, monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(login, monkeypatch)


async def forever() -> None:
    await asyncio.Event().wait()


def cancellations() -> int:
    task = asyncio.current_task()
    assert task is not None
    return task.cancelling()


async def test_entering_returns_once_the_worker_answers(rig: Rig) -> None:
    job = rig.job([RUNNING])

    with tunnel_log() as log:
        async with job:
            assert job.worker is not None
            assert job.worker.sshd.port == rig.worker_port
            assert job.status.phase is Phase.STARTING
            [control] = control_dirs(rig.login)
            record = json.loads((control / "job.json").read_text(encoding="utf-8"))

    # For a later client to find the job by.
    assert record == {
        "job_id": JOB_ID,
        "scheduler": "LSF",
        "submission_mode": "batch",
        "sshd_port": rig.worker_port,
    }
    assert "Stopping tunnel: its block was left" in log.messages()
    assert rig.cancelled == [JOB_ID]
    assert control_dirs(rig.login) == []


async def test_a_failure_while_the_block_runs_cancels_it_and_raises_job_failed(
    rig: Rig,
) -> None:
    job = rig.job([RUNNING, SchedulerError("bjobs failed")])
    cancelled: list[bool] = []

    async def use() -> None:
        async with job:
            try:
                await forever()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

    with pytest.raises(JobFailed) as failed:
        await use()

    assert cancelled == [True]
    assert str(failed.value) == "could not monitor worker job 42: bjobs failed"
    assert isinstance(failed.value.__cause__, TunnelError)
    # The job's cancellation was taken back.
    assert cancellations() == 0
    assert rig.cancelled == [JOB_ID]
    assert control_dirs(rig.login) == []


async def test_the_job_ending_while_the_block_runs_raises_job_ended(
    rig: Rig,
) -> None:
    job = rig.job([RUNNING, DONE])

    with pytest.raises(JobEnded) as ended:
        async with job:
            await forever()

    assert str(ended.value) == "worker job 42 ended in scheduler state DONE"
    assert ended.value.end == JobEnd(
        reason="worker job 42 ended in scheduler state DONE", by_scheduler=True
    )
    assert cancellations() == 0
    # It ended by itself, so it isn't cancelled.
    assert rig.cancelled == []


async def test_the_job_ending_during_wait_returns_how_it_ended(rig: Rig) -> None:
    job = rig.job([RUNNING, JobInfo(JobState.SUCCEEDED, "DONE", exit_code=0)])

    async with job:
        end = await job.wait()

    assert end == JobEnd(
        reason="worker job 42 ended in scheduler state DONE with exit code 0",
        by_scheduler=True,
        exit_code=0,
    )
    assert rig.cancelled == []


async def test_a_failure_during_wait_is_raised_by_wait(rig: Rig) -> None:
    job = rig.job([RUNNING, SchedulerError("bjobs failed")])
    from_wait: list[JobFailed] = []

    async def use() -> None:
        async with job:
            try:
                await job.wait()
            except JobFailed as error:
                from_wait.append(error)
                raise

    with pytest.raises(JobFailed) as failed:
        await use()

    # Passed on as it was, not wrapped again.
    assert from_wait == [failed.value]


async def test_stop_from_another_task_ends_the_block_without_an_error(
    rig: Rig,
) -> None:
    job = rig.job([RUNNING])
    cancelled: list[bool] = []

    with tunnel_log() as log:
        async with job:
            asyncio.get_running_loop().call_soon(job.stop, t"enough")
            try:
                await forever()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

    assert cancelled == [True]
    assert cancellations() == 0
    assert "Stopping tunnel: enough" in log.messages()
    assert rig.cancelled == [JOB_ID]
    assert control_dirs(rig.login) == []


async def test_stop_during_wait_returns_from_wait(rig: Rig) -> None:
    job = rig.job([RUNNING])

    async with job:
        asyncio.get_running_loop().call_soon(job.stop, t"enough")
        end = await job.wait()

    assert end == JobEnd(reason="enough", by_scheduler=False)
    assert rig.cancelled == [JOB_ID]


async def test_another_task_waiting_too_doesnt_get_the_host_cancelled(
    rig: Rig,
) -> None:
    job = rig.job([RUNNING])
    others: list[asyncio.Task[JobEnd]] = []
    ends: list[JobEnd] = []

    async with job:
        loop = asyncio.get_running_loop()
        # Once this task waits, another starts waiting; then the job stops.
        loop.call_soon(lambda: others.append(asyncio.create_task(job.wait())))
        loop.call_later(0.05, job.stop, t"enough")
        ends.append(await job.wait())

    [other] = others
    assert ends == [await other] == [JobEnd(reason="enough", by_scheduler=False)]


@pytest.mark.parametrize("also_stopped", [False, True], ids=["alone", "with-stop"])
async def test_an_outer_cancellation_is_passed_on(rig: Rig, also_stopped: bool) -> None:
    """Even when the job stops at the same moment: it isn't the job's to take."""
    job = rig.job([RUNNING])
    entered = asyncio.Event()

    async def use() -> None:
        async with job:
            entered.set()
            await forever()

    task = asyncio.create_task(use())
    async with asyncio.timeout(WAIT_SECONDS):
        await entered.wait()
    if also_stopped:
        job.stop(t"enough")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert rig.cancelled == [JOB_ID]
    assert control_dirs(rig.login) == []


async def test_stop_before_entering_connects_to_nothing(rig: Rig) -> None:
    job = rig.job([RUNNING])
    job.stop(t"never mind")

    with pytest.raises(JobEnded) as ended:
        async with job:
            pytest.fail("entered")

    assert ended.value.end == JobEnd(reason="never mind", by_scheduler=False)
    assert rig.login.auth_attempts == []


def _stop_after_creating_the_control_directory(
    rig: Rig, job: Callable[[], JobLifecycle]
) -> dict[str, Any]:
    create = WorkerControl.create

    async def create_then_stop(control: WorkerControl) -> None:
        await create(control)
        job().stop(t"Ctrl+C")
        await asyncio.sleep(0.05)

    rig.monkeypatch.setattr(WorkerControl, "create", create_then_stop)
    return {}


def _stop_during_submission(
    _rig: Rig, job: Callable[[], JobLifecycle]
) -> dict[str, Any]:
    async def stop_then_finish() -> None:
        job().stop(t"Ctrl+C")
        await asyncio.sleep(0.05)

    return {"submitting": stop_then_finish}


@pytest.mark.parametrize(
    "stop_during_step",
    [_stop_after_creating_the_control_directory, _stop_during_submission],
    ids=["control-directory", "submission"],
)
async def test_stop_during_a_step_still_cleans_up_after_it(
    rig: Rig,
    stop_during_step: Callable[[Rig, Callable[[], JobLifecycle]], dict[str, Any]],
) -> None:
    """Cancelled midway, the step would still take effect, with no cleanup for it."""
    jobs: list[JobLifecycle] = []
    scheduler = stop_during_step(rig, lambda: jobs[0])
    jobs.append(rig.job([RUNNING], **scheduler))

    with pytest.raises(JobEnded) as ended:
        async with jobs[0]:
            pytest.fail("entered")

    assert ended.value.end == JobEnd(reason="Ctrl+C", by_scheduler=False)
    assert control_dirs(rig.login) == []
    if scheduler:
        # Submitted after all, so it is cancelled.
        assert rig.cancelled == [JOB_ID]


async def test_an_attachment_closed_early_is_not_closed_again(rig: Rig) -> None:
    events: list[str] = []

    async def cancelling() -> None:
        events.append("cancel the job")

    job = rig.job([RUNNING], cancelling=cancelling)

    def step(name: str) -> Callable[[], Any]:
        async def run() -> None:
            events.append(name)

        return run

    async with job:
        early, late = job.attach(), job.attach()
        early.push("close the first", step("early: first"))
        early.push("close the second", step("early: second"))
        late.push("close it", step("late"))
        # Twice at once, and once more: the steps run once, the newest first.
        await asyncio.gather(early.close(), early.close())
        await early.close()
        assert early.closed
        assert events == ["early: second", "early: first"]

    # The rest when the job is left, before the job itself is cleaned up.
    assert events == ["early: second", "early: first", "late", "cancel the job"]


async def test_a_cancelled_close_still_closes(rig: Rig) -> None:
    job = rig.job([RUNNING])
    closed = asyncio.Event()
    started = asyncio.Event()

    async def slow() -> None:
        started.set()
        await asyncio.sleep(0.05)
        closed.set()

    async with job:
        attachment = job.attach()
        attachment.push("close it slowly", slow)
        closing = asyncio.create_task(attachment.close())
        await started.wait()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert not closed.is_set()

    # Leaving the job waited for it.
    assert closed.is_set()
