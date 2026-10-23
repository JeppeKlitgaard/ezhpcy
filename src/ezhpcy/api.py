"""
EzHPCy from Python: worker jobs on an HPC cluster, as handles.

A job is a node running EzHPCy's worker, an SSH daemon of its own, reachable
through the login node. Things are attached to a running job, and detached
again: so far, Host blocks, through which `ssh <alias>` reaches the node.

    from ezhpcy import api

    config = api.Config.load()
    profile = config.profiles["dtu-hpc"]

    async with api.reserve_alias("gpu") as gpu:
        async with api.connect(profile.login, prompts=api.terminal_prompts()) as cluster:
            async with cluster.job(profile.job.with_resources(cores=4)) as job:
                await job.publish(gpu)  # `ssh gpu` reaches the node
                await job.wait()  # until the job ends, or `job.stop()`

Two levels: a `Cluster` is the login connection (`connect`), and a `Job` is one
worker job on it (`Cluster.job`); a cluster can hold several jobs at once.
Leaving a job's block closes what is attached to it, cancels the job unless it
ended, and cleans up; leaving the cluster's closes the connection.

When the job ends or fails while its block's code runs, as `asyncio.timeout`
does, that code is cancelled, and leaving the block raises `JobEnded` (the
scheduler ended the job, e.g. at its time limit) or `JobFailed` (with the
failure as its cause). `Job.wait` instead returns a `JobEnd`, or raises
`JobFailed`. Leaving after `Job.stop` raises nothing.

Values are typed: a `Login`, a `JobConfig`, an `AliasReservation`, `pathlib`
paths, `timedelta`s. Build them directly, or read them from a configuration file
(`Config`). The API never prints, prompts or configures logging: it logs to the
`ezhpcy` logger, raises exceptions (all `MessageError`s), and asks for passwords
and host keys only through the `Prompts` it is given.

Async throughout: run it with `asyncio.run`. Cancelling a call leaves nothing
behind that its cleanup would miss. This is version 0: the API may change.
"""

from contextlib import asynccontextmanager
lazy import asyncio
lazy from collections.abc import AsyncGenerator, Awaitable, Sequence
lazy from types import TracebackType
lazy from typing import Self

lazy from pydantic import SecretStr

from ezhpcy.config import Config, Profile
from ezhpcy.job import JobEnd, JobEnded, JobFailed
from ezhpcy.job.publish import Published
from ezhpcy.job.status import JobStatus, Phase, PublishedStatus
from ezhpcy.login import ConnectionFailed, Prompts, terminal_prompts
from ezhpcy.messages import MessageError
from ezhpcy.provision_host import ProvisioningError
from ezhpcy.provision_local import MissingCredentials, missing_credentials
from ezhpcy.scheduler.base import JobInfo, JobState
from ezhpcy.scheduler.types import SchedulerType
from ezhpcy.tunnel.lock import (
    AliasInUse,
    AliasReservation,
    AliasUnreachable,
    reserve_alias,
)
from ezhpcy.tunnel.ssh_config import AliasProblem, probe_alias
from ezhpcy.types import (
    ConfigError,
    JobConfig,
    Login,
    LSFOptions,
    PBSOptions,
    Resources,
    SchedulerSettings,
    SubmissionMode,
    Timings,
)
lazy from ezhpcy import login as _login
lazy from ezhpcy.job import publish as _publish
lazy from ezhpcy.job.lifecycle import JobLifecycle, RunningWorker
lazy from ezhpcy.ssh import RemoteHost

__all__ = [
    "AliasInUse",
    "AliasProblem",
    "AliasReservation",
    "AliasUnreachable",
    "Cluster",
    "Config",
    "ConfigError",
    "ConnectionFailed",
    "Job",
    "JobConfig",
    "JobEnd",
    "JobEnded",
    "JobFailed",
    "JobInfo",
    "JobState",
    "JobStatus",
    "LSFOptions",
    "Login",
    "MessageError",
    "MissingCredentials",
    "PBSOptions",
    "Phase",
    "Profile",
    "Prompts",
    "ProvisioningError",
    "Published",
    "PublishedStatus",
    "Resources",
    "SchedulerSettings",
    "SchedulerType",
    "SubmissionMode",
    "Timings",
    "connect",
    "missing_credentials",
    "probe_alias",
    "reserve_alias",
    "terminal_prompts",
]


@asynccontextmanager
async def connect(
    login: Login,
    *,
    prompts: Prompts | None = None,
    password: SecretStr | None = None,
) -> AsyncGenerator[Cluster]:
    """
    Connect to `login`'s node, and close the connection when the block is left.

    The password is `password`, else the first of `login`'s sources that has
    one, read now. Public-key authentication is tried first; a password given or
    read is tried once. Only if both fail, and the server offers a password, is
    `prompts.ask_password` awaited, up to three times, unless
    `login.password_prompt` is false. An unknown host key is trusted only if
    `prompts.accept_host_key` accepts it, and is then saved to EzHPCy's known
    hosts file; a host key that changed is always refused.

    Raises `ConnectionFailed`, saying what was missing if `prompts` were needed,
    `ConfigError` if a password source can't be read, and whatever the prompts
    raise.
    """
    host = await _login.connect(login, prompts=prompts, password=password)
    async with host:
        yield Cluster(host)


class Cluster:
    """
    A connection to a login node (`connect`), on which jobs run (`job`).
    """

    def __init__(self, host: RemoteHost) -> None:
        # Use `connect`.
        self._host = host

    @property
    def login(self) -> Login:
        return self._host.login

    @property
    def closed(self) -> Awaitable[Exception | None]:
        """
        Resolves when the connection ends: to the error that ended it, or to None
        when it was closed.
        """
        return asyncio.shield(self._host.closed)

    def job(self, config: JobConfig, *, worker_ports: Sequence[int] = ()) -> Job:
        """
        A worker job, run while `async with` it (see `Job`).

        `worker_ports` are the ports on the node that the worker's SSH daemon
        tries, in order, until one is free; by default a few random ones from
        the dynamic range.
        """
        return Job(self._host, config, tuple(worker_ports))


class Job:
    """
    A worker job, from `Cluster.job`: entering `async with` submits it, and
    returns once the worker's SSH daemon is ready and its heartbeats run;
    leaving closes what is attached to it, newest first, cancels the job unless
    it ended, and removes its files on the cluster, each step under a timeout.

    Entering raises `JobFailed` if the job fails to start (its cause a
    `MissingCredentials` if the worker's credentials are missing and the config
    doesn't provision; see `missing_credentials`), and `JobEnded` if it ends
    first, or `stop` is called. For how its end shows while it runs, see the
    module. A job is entered once.

    `status` says where it is, and what is attached, at any time.
    """

    def __init__(
        self, host: RemoteHost, config: JobConfig, worker_ports: tuple[int, ...]
    ) -> None:
        # Use `Cluster.job`.
        self._lifecycle = JobLifecycle(
            host=host, config=config, worker_ports=worker_ports
        )

    @property
    def config(self) -> JobConfig:
        return self._lifecycle.config

    @property
    def status(self) -> JobStatus:
        """A snapshot of where the job is, replaced whole at each change."""
        return self._lifecycle.status

    @property
    def id(self) -> str:
        """The scheduler's ID for the job, once entered."""
        return self._worker().job_id

    @property
    def node(self) -> str:
        """The node the job runs on, as the login node reaches it, once entered."""
        return self._worker().node

    async def __aenter__(self) -> Self:
        await self._lifecycle.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return await self._lifecycle.__aexit__(exc_type, exc, traceback)

    async def wait(self) -> JobEnd:
        """
        Wait until the job ends, or `stop` is called, and return how it ended.
        Raises `JobFailed` if it failed. The block's code isn't cancelled while it
        waits here.
        """
        return await self._lifecycle.wait()

    def stop(self, reason: str = "stopped") -> None:
        """
        End the job, saying why in the log; leaving its block then raises
        nothing. Call it from the event loop's thread, e.g. from another task, or
        from a signal handler through `loop.call_soon_threadsafe`. Before the job
        is entered, entering then raises `JobEnded`.
        """
        self._lifecycle.stop(t"{reason}")

    async def publish(self, reservation: AliasReservation) -> Published:
        """
        Make `ssh <alias>` reach the job, under the reserved alias: forward a
        loopback port to the worker's SSH daemon, and write the Host block that
        points OpenSSH there. Withdrawn by `Published.withdraw`, or when the job
        is left. A job may be published under several aliases.

        Raises `ValueError` if the reservation was released, or a job is
        published under it already: an alias has one Host block at a time.
        """
        self._worker()
        return await _publish.publish(self._lifecycle, reservation)

    def _worker(self) -> RunningWorker:
        if (worker := self._lifecycle.worker) is None:
            raise RuntimeError("the job is not ready")
        return worker
