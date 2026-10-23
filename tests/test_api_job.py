"""
`ezhpcy.api`'s jobs: entering and leaving one, several on one host, Host blocks
attached to them, and how their end shows. Uses `ezhpcy.api` alone, with the
in-process SSH servers as the login node and the worker's sshd, and the
scheduler fake.
"""

import asyncio
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest

from ezhpcy import api
from tests.support.sshserver import SSHTestServer
from tests.support.tunnel import (
    FAST_TIMINGS,
    JOB_ID,
    RUNNING,
    FakeScheduler,
    Inspection,
    configure_tunnel,
    control_dirs,
    snapshot,
    use_scheduler,
    worker_ready_on,
)
from tests.support.worker import NODE, remove_worker_credentials, ssh_through

LOGIN = api.Login(host="login.example.com", user="alice")
JOB = api.JobConfig(
    scheduler=api.SchedulerSettings(
        type=api.SchedulerType.LSF, submission_mode=api.SubmissionMode.BATCH
    ),
    timings=api.Timings.model_validate(FAST_TIMINGS),
)
DONE = snapshot(api.JobState.SUCCEEDED, "DONE")
WAIT_SECONDS = 5
STARTING_PHASES = {
    api.Phase.PREPARING,
    api.Phase.SUBMITTING,
    api.Phase.QUEUED,
    api.Phase.STARTING,
}


class Rig:
    """The login node, the worker's sshd, and the schedulers jobs were given."""

    def __init__(
        self,
        login: SSHTestServer,
        worker: SSHTestServer,
        monkeypatch: pytest.MonkeyPatch,
        runtime: Path,
    ) -> None:
        self.login = login
        self.worker = worker
        self.monkeypatch = monkeypatch
        self.runtime = runtime
        self.worker_port = worker.port
        self.schedulers: list[FakeScheduler] = []
        self.jobs: list[api.Job] = []
        # Provisioning's commands on the login node.
        configure_tunnel(monkeypatch, login)

    def scheduling(self, inspections: list[Inspection], **options: object) -> None:
        """
        Give each job a scheduler that reports `inspections`. The job monitors'
        polls are held while a job starts: their 10 ms interval could otherwise
        end it while it connects to its node.
        """
        self.schedulers = use_scheduler(
            self.monkeypatch,
            lambda host: FakeScheduler(
                host,
                inspections,
                submitted=worker_ready_on(self.login, self.worker_port),
                held=self.starting,
                **options,  # type: ignore[arg-type]
            ),
        )

    def starting(self) -> bool:
        return any(job.status.phase in STARTING_PHASES for job in self.jobs)

    def job(self, host: api.Host, config: api.JobConfig = JOB) -> api.Job:
        self.jobs.append(host.job(config, worker_ports=[self.worker_port]))
        return self.jobs[-1]

    def host_block(self, alias: str) -> Path:
        return self.runtime / "ssh-config" / f"active-{alias}.conf"

    @property
    def cancelled(self) -> list[str]:
        return [job for scheduler in self.schedulers for job in scheduler.cancelled]


@pytest.fixture
def rig(
    login: SSHTestServer,
    worker: SSHTestServer,
    monkeypatch: pytest.MonkeyPatch,
    isolated_runtime_dir: Path,
) -> Rig:
    return Rig(login, worker, monkeypatch, isolated_runtime_dir)


async def forever() -> None:
    await asyncio.Event().wait()


async def run_until_it_ends(
    job: api.Job, *, then: Callable[[], object] = lambda: None
) -> None:
    """Enter `job`, call `then`, and run until the job ends its block."""
    async with job:
        then()
        async with asyncio.timeout(WAIT_SECONDS):
            await forever()


async def test_a_published_job_serves_then_cleans_up_in_order(rig: Rig) -> None:
    seen_when_cancelled: dict[str, object] = {}

    async def cancelling() -> None:
        # The Host block goes first, the job next, its control directory last,
        # and the login connection stays its block's.
        seen_when_cancelled.update(
            host_block=rig.host_block("gpu").exists(),
            control_dirs=len(control_dirs(rig.login)),
            connected=not login_closed.done(),
        )

    rig.scheduling([RUNNING], cancelling=cancelling)

    async with api.reserve_alias("gpu") as gpu, api.connect(LOGIN) as login_node:
        login_closed = asyncio.ensure_future(login_node.closed)
        async with rig.job(login_node) as job:
            assert (job.id, job.node) == (JOB_ID, "127.0.0.1")
            assert job.status.phase is api.Phase.READY
            published = await job.publish(gpu)
            assert published.alias == "gpu"
            assert published.problem is None
            assert rig.host_block("gpu").exists()
            assert await asyncio.to_thread(ssh_through, published.port) == f"{NODE}\n"
            [status] = job.status.attachments
            assert isinstance(status, api.PublishedStatus)
            assert (status.alias, status.port) == ("gpu", published.port)
        assert not login_closed.done()

    assert seen_when_cancelled == {
        "host_block": False,
        "control_dirs": 1,
        "connected": True,
    }
    assert rig.cancelled == [JOB_ID]
    assert control_dirs(rig.login) == []
    # The alias is free again.
    async with api.reserve_alias("gpu"):
        pass


async def test_a_host_runs_several_jobs_at_once(rig: Rig) -> None:
    rig.scheduling([RUNNING])
    entered = asyncio.Barrier(2)

    async def run_job(login_node: api.Host) -> str:
        async with rig.job(login_node) as job:
            await entered.wait()
            return job.id

    async with api.connect(LOGIN) as login_node, asyncio.TaskGroup() as tasks:
        first = tasks.create_task(run_job(login_node))
        second = tasks.create_task(run_job(login_node))

    assert (first.result(), second.result()) == (JOB_ID, JOB_ID)
    assert len(rig.schedulers) == 2
    assert rig.cancelled == [JOB_ID, JOB_ID]
    assert control_dirs(rig.login) == []


async def test_an_alias_has_one_host_block_at_a_time(rig: Rig) -> None:
    rig.scheduling([RUNNING])

    async with (
        api.reserve_alias("gpu") as gpu,
        api.connect(LOGIN) as login_node,
        rig.job(login_node) as job,
    ):
        published = await job.publish(gpu)
        with pytest.raises(ValueError, match="published under 'gpu' already"):
            await job.publish(gpu)
        assert rig.host_block("gpu").exists()

        await published.withdraw()
        again = await job.publish(gpu)

        assert rig.host_block("gpu").exists()
        assert again.port != published.port


async def test_a_job_published_under_two_aliases_withdraws_each(rig: Rig) -> None:
    rig.scheduling([RUNNING])

    async with (
        api.reserve_alias("gpu") as gpu,
        api.reserve_alias("cpu") as cpu,
        api.connect(LOGIN) as login_node,
        rig.job(login_node) as job,
    ):
        first = await job.publish(gpu)
        second = await job.publish(cpu)
        assert {status.alias for status in job.status.attachments} == {"gpu", "cpu"}
        assert first.port != second.port

        await first.withdraw()

        assert not rig.host_block("gpu").exists()
        assert rig.host_block("cpu").exists()
        assert [status.alias for status in job.status.attachments] == ["cpu"]
        # Once only.
        await first.withdraw()

    assert not rig.host_block("cpu").exists()


async def test_the_scheduler_ending_the_job_ends_its_block_with_job_ended(
    rig: Rig,
) -> None:
    rig.scheduling([RUNNING, DONE])

    async with api.connect(LOGIN) as login_node:
        with pytest.raises(api.JobEnded) as ended:
            await run_until_it_ends(rig.job(login_node))

    assert ended.value.end.by_scheduler
    assert "ended in scheduler state DONE" in ended.value.end.reason
    # It ended by itself: nothing to cancel.
    assert rig.cancelled == []


async def test_wait_returns_how_the_job_ended(rig: Rig) -> None:
    rig.scheduling([RUNNING, api.JobInfo(api.JobState.SUCCEEDED, "DONE", exit_code=0)])

    async with api.connect(LOGIN) as login_node, rig.job(login_node) as job:
        async with asyncio.timeout(WAIT_SECONDS):
            end = await job.wait()

    assert end.by_scheduler
    assert end.exit_code == 0


async def test_stop_from_another_task_leaves_the_block_quietly(rig: Rig) -> None:
    rig.scheduling([RUNNING])

    async with api.connect(LOGIN) as login_node, rig.job(login_node) as job:
        asyncio.get_running_loop().call_later(0.05, job.stop, "enough")
        async with asyncio.timeout(WAIT_SECONDS):
            await forever()

    assert rig.cancelled == [JOB_ID]


async def test_stop_while_waiting_returns_the_reason(rig: Rig) -> None:
    rig.scheduling([RUNNING])

    async with api.connect(LOGIN) as login_node, rig.job(login_node) as job:
        asyncio.get_running_loop().call_later(0.05, job.stop, "enough")
        async with asyncio.timeout(WAIT_SECONDS):
            end = await job.wait()

    assert end == api.JobEnd(reason="enough", by_scheduler=False)


async def test_stop_before_entering_ends_entering(rig: Rig) -> None:
    rig.scheduling([RUNNING])

    async with api.connect(LOGIN) as login_node:
        job = rig.job(login_node)
        job.stop("never mind")
        with pytest.raises(api.JobEnded):
            async with job:
                pytest.fail("entered")

    assert rig.schedulers == []


async def test_before_entering_the_status_stays_put(rig: Rig) -> None:
    rig.scheduling([RUNNING])

    async with api.connect(LOGIN) as login_node:
        job = rig.job(login_node)
        before = job.status
        assert job.status is before
        assert before.phase is api.Phase.PREPARING
        async with job:
            with pytest.raises(RuntimeError, match="entered once"):
                await run_until_it_ends(job)


async def test_losing_the_login_connection_fails_the_job(rig: Rig) -> None:
    rig.scheduling([RUNNING])

    async with api.connect(LOGIN) as login_node:
        with pytest.raises(api.JobFailed, match="login-node SSH connection lost"):
            await run_until_it_ends(rig.job(login_node), then=rig.login.drop)


async def test_a_job_without_provisioning_needs_the_worker_credentials(
    rig: Rig,
) -> None:
    rig.scheduling([RUNNING])
    config = JOB.model_copy(update={"auto_provision": False})
    remove_worker_credentials()

    async with api.connect(LOGIN) as login_node:
        with pytest.raises(api.JobFailed, match="ezhpcy provision") as failed:
            async with rig.job(login_node, config):
                pytest.fail("entered")

    assert isinstance(failed.value.__cause__, api.MissingCredentials)
    assert failed.value.__cause__.missing
    # Before submitting anything.
    assert rig.schedulers == []


async def test_a_job_that_never_starts_fails_entering(rig: Rig) -> None:
    rig.scheduling([snapshot(api.JobState.PENDING, "PEND")])
    config = JOB.with_timings(queue_timeout_seconds=timedelta(seconds=0.1))

    async with api.connect(LOGIN) as login_node:
        with pytest.raises(api.JobFailed, match=r"did not start within 0\.1 seconds"):
            async with rig.job(login_node, config):
                pytest.fail("entered")

    assert rig.cancelled == [JOB_ID]
