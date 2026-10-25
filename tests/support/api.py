"""
Values and a rig for tests of `ezhpcy.api`: a plain host's login, a batch job,
and the login node, the worker's sshd and a scheduler for jobs (`JobRig`).
"""

from pathlib import Path

import pytest

from ezhpcy import api
from tests.support.login import remote_state_json
from tests.support.sshserver import SSHTestServer, reply
from tests.support.tunnel import (
    FAST_TIMINGS,
    RUNNING,
    FakeScheduler,
    configure_tunnel,
    use_scheduler,
    worker_ready_on,
)
from tests.support.worker import quick_processes

LOGIN = api.Login(host="login.example.com", user="alice")
JOB = api.JobConfig(
    scheduler=api.SchedulerSettings(
        type=api.SchedulerType.LSF, submission_mode=api.SubmissionMode.BATCH
    ),
    timings=api.Timings.model_validate(FAST_TIMINGS),
)


class JobRig:
    """
    The login node, the worker's sshd, and a job's scheduler. The worker's
    sshd answers `remote_state`'s command as the login node does: they share
    a home directory.
    """

    def __init__(
        self,
        login: SSHTestServer,
        worker: SSHTestServer,
        monkeypatch: pytest.MonkeyPatch,
        runtime: Path,
    ) -> None:
        self.login = login
        self.worker = worker
        self.runtime = runtime
        configure_tunnel(monkeypatch, login)
        quick_processes(monkeypatch)
        worker.command("bash -lc", reply(stdout=remote_state_json()))
        self.jobs: list[api.Job] = []
        self.schedulers = use_scheduler(
            monkeypatch,
            lambda host: FakeScheduler(
                host,
                [RUNNING],
                submitted=worker_ready_on(login, worker.port),
            ),
        )

    def job(self, host: api.Host, config: api.JobConfig = JOB) -> api.Job:
        return host.job(config, worker_ports=[self.worker.port])

    def host_block(self, alias: str) -> Path:
        return self.runtime / "ssh-config" / f"active-{alias}.conf"
