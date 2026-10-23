"""
The job's lifecycle on its own, against the in-process SSH servers, with the
scheduler fake and the worker's sshd: how the job's end shows to the code that
uses it, and its attachments.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, Callable
from contextlib import AsyncExitStack
from typing import Any

import pytest

from ezhpcy import login as login_module
from ezhpcy.job import JobEnd, JobEnded, JobFailed
from ezhpcy.job.lifecycle import JobLifecycle
from ezhpcy.job.status import Phase
from ezhpcy.scheduler.base import JobInfo, JobSpec, JobState, SchedulerError
from ezhpcy.scheduler.types import SchedulerType
from ezhpcy.tunnel import TunnelError
from ezhpcy.types import (
    JobConfig,
    Login,
    SchedulerSettings,
    SubmissionMode,
    Timings,
)
from ezhpcy.worker.control import WorkerControl
from tests.support.sshserver import SSHTestServer
from tests.support.tunnel import (
    CONTROL_ROOT,
    FAST_TIMINGS,
    JOB_ID,
    RUNNING,
    FakeScheduler,
    Inspection,
    configure_tunnel,
    control_dirs,
    snapshot,
    tunnel_log,
    use_scheduler,
    write_worker_status,
)
from tests.support.worker import NODE

DONE = snapshot(JobState.SUCCEEDED, "DONE")
# How long a test waits for something that should happen at once.
WAIT_SECONDS = 5


class Rig:
    """
    Makes jobs against the login server, each on a login connection of its own,
    closed after the test, and keeps their schedulers.
    """

    def __init__(
        self,
        login: SSHTestServer,
        worker: SSHTestServer,
        monkeypatch: pytest.MonkeyPatch,
        connections: AsyncExitStack,
    ) -> None:
        self.login = login
        self.monkeypatch = monkeypatch
        self.connections = connections
        self.worker_port = worker.port
        self.schedulers: list[FakeScheduler] = []
        configure_tunnel(monkeypatch, login)

    async def job(
        self, inspections: list[Inspection], **scheduler: Any
    ) -> JobLifecycle:
        """
        A batch job whose scheduler reports `inspections`, as `FakeScheduler`.
        The job monitor's polls are held until the job is entered: its 10 ms
        interval could otherwise end the job while it connects to its node.
        """
        jobs: list[JobLifecycle] = []

        def make(host: Any) -> FakeScheduler:
            return FakeScheduler(
                host,
                inspections,
                submitted=self.start_worker,
                held=lambda: jobs[0].node_host is None,
                **scheduler,
            )

        self.schedulers = use_scheduler(self.monkeypatch, make)
        host = await self.connections.enter_async_context(
            await login_module.connect(Login(user="alice", host="login.example.com"))
        )
        jobs.append(
            JobLifecycle(
                host=host,
                config=JobConfig(
                    scheduler=SchedulerSettings(
                        type=SchedulerType.LSF, submission_mode=SubmissionMode.BATCH
                    ),
                    timings=Timings.model_validate(FAST_TIMINGS),
                ),
                worker_ports=(self.worker_port,),
            )
        )
        return jobs[0]

    def start_worker(self, _spec: JobSpec) -> None:
        write_worker_status(self.login, "ready", str(self.worker_port))

    @property
    def cancelled(self) -> list[str]:
        [scheduler] = self.schedulers
        return scheduler.cancelled


@pytest.fixture
async def rig(
    login: SSHTestServer, worker: SSHTestServer, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[Rig]:
    async with AsyncExitStack() as connections:
        yield Rig(login, worker, monkeypatch, connections)


async def forever() -> None:
    await asyncio.Event().wait()


def cancellations() -> int:
    task = asyncio.current_task()
    assert task is not None
    return task.cancelling()


async def test_entering_returns_once_connected_to_the_node(rig: Rig) -> None:
    job = await rig.job([RUNNING])

    with tunnel_log() as log:
        async with job:
            assert job.worker is not None
            assert job.worker.sshd.port == rig.worker_port
            assert job.status.phase is Phase.READY
            assert job.node_host is not None
            assert await job.node_host.run(["hostname"]) == f"{NODE}\n"
            node_closed = job.node_host.closed
            [control] = control_dirs(rig.login)
            record = json.loads((control / "job.json").read_text(encoding="utf-8"))

    # Closed with the job, not lost.
    assert node_closed.done()
    assert node_closed.result() is None

    # For a later client to find the job by.
    assert record == {
        "job_id": JOB_ID,
        "scheduler": "LSF",
        "submission_mode": "batch",
        "sshd_port": rig.worker_port,
    }
    assert "Stopping job: its block was left" in log.messages()
    assert rig.cancelled == [JOB_ID]
    assert control_dirs(rig.login) == []


async def test_a_failure_while_the_block_runs_cancels_it_and_raises_job_failed(
    rig: Rig,
) -> None:
    job = await rig.job([RUNNING, SchedulerError("bjobs failed")])
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
    job = await rig.job([RUNNING, DONE])

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
    job = await rig.job([RUNNING, JobInfo(JobState.SUCCEEDED, "DONE", exit_code=0)])

    async with job:
        end = await job.wait()

    assert end == JobEnd(
        reason="worker job 42 ended in scheduler state DONE with exit code 0",
        by_scheduler=True,
        exit_code=0,
    )
    assert rig.cancelled == []


async def test_a_failure_during_wait_is_raised_by_wait(rig: Rig) -> None:
    job = await rig.job([RUNNING, SchedulerError("bjobs failed")])
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
    job = await rig.job([RUNNING])
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
    assert "Stopping job: enough" in log.messages()
    assert rig.cancelled == [JOB_ID]
    assert control_dirs(rig.login) == []


async def test_stop_during_wait_returns_from_wait(rig: Rig) -> None:
    job = await rig.job([RUNNING])

    async with job:
        asyncio.get_running_loop().call_soon(job.stop, t"enough")
        end = await job.wait()

    assert end == JobEnd(reason="enough", by_scheduler=False)
    assert rig.cancelled == [JOB_ID]


async def test_another_task_waiting_too_doesnt_get_the_host_cancelled(
    rig: Rig,
) -> None:
    job = await rig.job([RUNNING])
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
    job = await rig.job([RUNNING])
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


async def test_stop_before_entering_does_nothing_on_the_cluster(rig: Rig) -> None:
    job = await rig.job([RUNNING])
    job.stop(t"never mind")

    with pytest.raises(JobEnded) as ended:
        async with job:
            pytest.fail("entered")

    assert ended.value.end == JobEnd(reason="never mind", by_scheduler=False)
    # No scheduler was made, and no control directory.
    assert rig.schedulers == []
    assert not rig.login.local_path(CONTROL_ROOT).exists()


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
    jobs.append(await rig.job([RUNNING], **scheduler))

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

    job = await rig.job([RUNNING], cancelling=cancelling)

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
    job = await rig.job([RUNNING])
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
