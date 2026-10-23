"""
EzHPCy from Python: SSH hosts, and worker jobs on an HPC cluster, as handles.

Everything EzHPCy does on a remote machine, it does through a `Host`: one SSH
connection to one machine. `connect` gives one, such as a cluster's login node.
A `Job` is a worker job submitted through a host's scheduler (`Host.job`): a
node running EzHPCy's worker, an SSH daemon of its own, reachable through the
login node, and a host of its own once it runs (`Job.host`).

    from ezhpcy import api

    config = api.Config.load()
    profile = config.profiles["dtu-hpc"]

    async with api.reserve_alias("gpu") as gpu:
        async with api.connect(profile.login, prompts=api.terminal_prompts()) as login_node:
            async with login_node.job(profile.job.with_resources(cores=4)) as job:
                await job.publish(gpu)  # `ssh gpu` reaches the node
                print(await job.host.run(["nvidia-smi", "-L"]))
                server = await job.host.start(["python", "-m", "http.server", "8000"])
                forward = await job.host.forward(api.TCPDestination("127.0.0.1", 8000))
                print(f"http://127.0.0.1:{forward.port}/")
                await job.wait()  # until the job ends, or `job.stop()`

Every host has the same operations: commands (`Host.run`), files (`Host.put`,
`Host.get`), processes started detached (`Host.start`), forwarded ports
(`Host.forward`, `Host.reverse_forward`), apps (`Host.run_app`), and jobs
(`Host.job`). Processes, ports and apps are attached to the host they were made
on, and closed, newest first, when its connection closes, unless they were
closed before. A host can run several jobs at once. Leaving a job's block
closes its host, and with it what is attached there, withdraws its Host blocks,
cancels the job unless it ended, and cleans up; leaving a connection's block
closes it.

Apps, such as JupyterLab, are described by manifests (`Manifest`), which a
configuration loads (`Config.load(manifests=…)`, `Config.app`). An app runs as
a process on its host, through EzHPCy's own Pixi, in an environment installed
there, and is forwarded to a loopback port here:

    config = api.Config.load(manifests=[Path("examples/apps/notebooks")])
    lab = await job.host.run_app(config.app("lab"))
    print(lab.url)

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
lazy from collections.abc import AsyncGenerator, Awaitable, Mapping, Sequence
lazy from pathlib import Path, PurePosixPath
lazy from types import TracebackType
lazy from typing import Self

lazy from pydantic import SecretStr

from ezhpcy.app import AppFailed, ManifestError
from ezhpcy.app.manifest import App, Manifest
from ezhpcy.app.run import AppState, AppStatus, RunningApp
from ezhpcy.config import Config, Profile
from ezhpcy.forward import Forward, ForwardStatus
from ezhpcy.job import JobEnd, JobEnded, JobFailed
from ezhpcy.job.publish import Published
from ezhpcy.job.status import JobStatus, Phase, PublishedStatus
from ezhpcy.login import ConnectionFailed, Prompts, terminal_prompts
from ezhpcy.messages import MessageError
from ezhpcy.process import Process, ProcessStatus
from ezhpcy.provision_host import ProvisioningError
from ezhpcy.provision_local import MissingCredentials, missing_credentials
from ezhpcy.scheduler.base import JobInfo, JobState
from ezhpcy.scheduler.types import SchedulerType
from ezhpcy.ssh import CommandFailed, Destination, SocketDestination, TCPDestination
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
lazy from ezhpcy import forward as _forward, login as _login, process as _process
lazy from ezhpcy.app import run as _app_run
lazy from ezhpcy.job import publish as _publish
lazy from ezhpcy.job.lifecycle import JobLifecycle, RunningWorker
lazy from ezhpcy.job.status import AttachmentStatus
lazy from ezhpcy.ssh import RemoteHost

__all__ = [
    "AliasInUse",
    "AliasProblem",
    "AliasReservation",
    "AliasUnreachable",
    "App",
    "AppFailed",
    "AppState",
    "AppStatus",
    "CommandFailed",
    "Config",
    "ConfigError",
    "ConnectionFailed",
    "Destination",
    "Forward",
    "ForwardStatus",
    "Host",
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
    "Manifest",
    "ManifestError",
    "MessageError",
    "MissingCredentials",
    "PBSOptions",
    "Phase",
    "Process",
    "ProcessStatus",
    "Profile",
    "Prompts",
    "ProvisioningError",
    "Published",
    "PublishedStatus",
    "Resources",
    "RunningApp",
    "SchedulerSettings",
    "SchedulerType",
    "SocketDestination",
    "SubmissionMode",
    "TCPDestination",
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
) -> AsyncGenerator[Host]:
    """
    Connect to `login`'s node, and close the connection when the block is left,
    after what is attached to it, newest first.

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
        yield Host(host)


class Host:
    """
    One SSH connection to one machine: a login node (`connect`), or a job's node
    (`Job.host`). Every host has the same operations.

    What is made on a host (processes, forwarded ports) is attached to it, and
    closed, newest first, when its connection closes: when the block of
    `connect` is left, or, for `Job.host`, the job's. Each one's own `stop` or
    `close` closes it earlier. `status` says what is attached.
    """

    def __init__(self, host: RemoteHost) -> None:
        # Use `connect`, or `Job.host`.
        self._host = host

    @property
    def login(self) -> Login:
        """The login node this connection is to, or through."""
        return self._host.login

    @property
    def closed(self) -> Awaitable[Exception | None]:
        """
        Resolves when the connection ends: to the error that ended it, or to None
        when it was closed.
        """
        return asyncio.shield(self._host.closed)

    @property
    def status(self) -> tuple[AttachmentStatus, ...]:
        """What is attached to the host, oldest first, as snapshots."""
        return self._host.attachments.statuses

    def job(self, config: JobConfig, *, worker_ports: Sequence[int] = ()) -> Job:
        """
        A worker job, submitted through this host's scheduler, run while `async
        with` it (see `Job`). A host without the scheduler fails to enter it.

        `worker_ports` are the ports on the node that the worker's SSH daemon
        tries, in order, until one is free; by default a few random ones from
        the dynamic range.
        """
        return Job(self._host, config, tuple(worker_ports))

    async def run(self, argv: Sequence[str], *, stdin: str | None = None) -> str:
        """
        Run a command, quoted for the host's shell, and return its stdout.
        `stdin`, if given, is sent to it, followed by end-of-file. Not through a
        login shell. Raises `CommandFailed`, with its stderr, unless it exits
        with status 0. Cancelled, its session is closed; the command may still
        run to its end.
        """
        return await self._host.run(list(argv), stdin=stdin)

    async def put(self, local: Path, remote: PurePosixPath) -> None:
        """Copy the file `local` here to `remote` on the host, over SFTP."""
        async with self._host.sftp() as sftp:
            await sftp.put(local, remote)

    async def get(self, remote: PurePosixPath, local: Path) -> None:
        """Copy the file `remote` on the host to `local` here, over SFTP."""
        async with self._host.sftp() as sftp:
            await sftp.get(remote, local)

    async def start(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: PurePosixPath | None = None,
    ) -> Process:
        """
        Start a command detached, attached to the host until stopped: it
        outlives the connection itself, but the host stops it when its
        connection closes. See `Process`.

        It runs in a private run directory of its own on the host,
        `/tmp/ezhpcy-<run ID>/`, with `env` added to its environment (names
        of shell variables), in `cwd` (by default the user's home directory),
        and not through a login shell: the host's profile, and so its
        modules, aren't read (start `["bash", "-lc", …]` for them). Its output
        goes to the run directory.
        Nothing about it is on a command line but the run directory's path.

        On a job's node, the job's end ends the process too. On another host,
        nothing stops it if this program dies before stopping it: stop it, or
        leave the host's block.
        """
        return await _process.start(self._host, argv, env=env, cwd=cwd)

    async def forward(
        self, destination: Destination, *, local_port: int = 0
    ) -> Forward:
        """
        Listen on a loopback port here, `local_port` or by default a free one,
        and forward each connection to it to `destination` from the host: a TCP
        port the host reaches, or a Unix socket on it. Attached to the host
        until closed. A connection the host can't make is closed, and the port
        keeps serving. Raises `OSError` if the local port is taken.
        """
        return await _forward.forward(self._host, destination, local_port=local_port)

    async def reverse_forward(
        self, remote_port: int, destination: Destination
    ) -> Forward:
        """
        Have the host listen on its loopback port `remote_port` (0: a free one,
        then `Forward.port`), and forward each connection to it to
        `destination` from here. Attached to the host until closed. Raises
        `asyncssh.ChannelListenError` if the host refuses to listen.
        """
        return await _forward.reverse_forward(self._host, remote_port, destination)

    async def run_app(
        self,
        app: App,
        *,
        local_port: int | None = None,
        provision: bool = True,
        apps_root: PurePosixPath | None = None,
    ) -> RunningApp:
        """
        Run an app from a manifest (`Config.app`, `Manifest`) on the host, and
        return once it is ready and forwarded to `local_port` here (by default
        its `access.local_port`, by default a free one). Attached to the host
        until stopped. See `RunningApp`.

        In order:

        1. with `provision`, its environment and assets, and EzHPCy's own Pixi,
           are installed on the host unless they are; without, they are only
           checked. They live under `apps_root`, by default `ezhpcy-apps` in
           the host's XDG cache directory; on a cluster, on the filesystem its
           nodes share.
        2. It starts as a process (`start`), through EzHPCy's Pixi, in its
           workdir: not through a login shell, so the host's profile and
           modules aren't read.
        3. Its readiness check runs through the host's connection, until it
           passes, the app exits, or `ready.timeout_seconds` pass.
        4. A loopback port here is forwarded to it (`forward`): to its socket
           in the process's run directory, or to `127.0.0.1` on the host.

        Several runs of one app may run at once; they share its workdir.
        Raises `ProvisioningError`, and `AppFailed` (with its stderr's last
        lines) if it exits before it is ready or isn't ready in time, once it
        has been stopped.
        """
        return await _app_run.run_app(
            self._host,
            app,
            local_port=local_port,
            provision=provision,
            apps_root=apps_root,
        )


class Job:
    """
    A worker job, from `Host.job`: entering `async with` submits it, and
    returns once the worker's SSH daemon is ready, its heartbeats run and the
    connection to it, `host`, is open. Leaving closes `host`, and what is
    attached there, withdraws the job's Host blocks, newest first, cancels the
    job unless it ended, and removes its files on the cluster, each step under a
    timeout.

    Entering raises `JobFailed` if the job fails to start (its cause a
    `MissingCredentials` if the worker's credentials are missing and the config
    doesn't provision; see `missing_credentials`), and `JobEnded` if it ends
    first, or `stop` is called. For how its end shows while it runs, see the
    module; losing `host`'s connection fails it, as losing the login node's
    does. A job is entered once.

    `status` says where it is, and what is attached to it and to `host`, at any
    time.
    """

    def __init__(
        self, host: RemoteHost, config: JobConfig, worker_ports: tuple[int, ...]
    ) -> None:
        # Use `Host.job`.
        self._lifecycle = JobLifecycle(
            host=host, config=config, worker_ports=worker_ports
        )
        self._node_host: Host | None = None

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
        return self._worker().sshd.host

    @property
    def host(self) -> Host:
        """
        The job's node, as a host, once entered: the connection to the worker's
        SSH daemon, through the login node's. Closed when the job is left.
        """
        if self._node_host is None:
            if (node_host := self._lifecycle.node_host) is None:
                raise RuntimeError("the job is not ready")
            self._node_host = Host(node_host)
        return self._node_host

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
