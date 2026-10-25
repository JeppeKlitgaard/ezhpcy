"""
One worker job, on a login connection, from submitting it until everything is
cleaned up, as an async context manager.

Entering `JobLifecycle` provisions (or checks) the worker infrastructure,
submits the worker job, waits for it to say it is ready, starts the heartbeats,
and connects to the worker's sshd through the login connection: `node_host`, the
connection to the job's node. Each step registers its cleanup on an
`AsyncExitStack` as soon as it has acquired something, so cleanup covers exactly
what was started, in reverse order. Each cleanup step has a timeout, and one
that fails only logs a warning. Once it is entered, what runs on the node is
attached to `node_host`, and the job's Host blocks to the job itself (`attach`);
leaving closes them in that order, first.

The job stops when `stop` is called (the CLI calls it on Ctrl+C), when the
scheduler says the job has ended, or when something fails: a startup step, or a
background task (the connection watchers, the heartbeats, the job monitor). The
first of these wins: it is logged as `Stopping job: <why>`, and decides what
leaving the job raises, if anything. Losing the login connection, or the
connection to the node, fails the job; closing the login connection is its
owner's, after the job is left.

How the end shows to the task that entered the job, its host task, as
`asyncio.timeout` does: when the job stops while that task runs the block's
code, the job cancels it, and leaving turns that cancellation back into
`JobEnded` (the scheduler ended the job) or `JobFailed`, or into nothing after
`stop`. A cancellation from anywhere else passes through. While the host task
waits in `wait`, it isn't cancelled: `wait` returns a `JobEnd`, or raises
`JobFailed`, instead. Entering raises the same when the job stops before it is
ready, `JobEnded` after `stop` too, as there is no block to skip.

All along, `status` says where the job is, and what is attached to it, for
the CLI's display.
"""

from dataclasses import dataclass
lazy import asyncio
lazy import codecs
lazy import functools
lazy import json
lazy import secrets
lazy import shlex
lazy import time
lazy from collections.abc import Awaitable, Callable, Coroutine
lazy from contextlib import AbstractAsyncContextManager, AsyncExitStack
lazy from dataclasses import replace
lazy from pathlib import PurePosixPath
lazy from string.templatelib import Template
lazy from types import TracebackType
lazy from typing import Any, Self

lazy import asyncssh
lazy from rich.text import Text

from ezhpcy.logging import REPLAY_ON_ERROR, get_logger
lazy from ezhpcy.attachments import Attachment, Attachments
lazy from ezhpcy.cleanup import cleanup, finish
lazy from ezhpcy.constants import DEFAULT_WORKER_PORTS
lazy from ezhpcy.job import JobEnd, JobEnded, JobFailed
lazy from ezhpcy.job.status import JobStatus, Phase
lazy from ezhpcy.messages import plain
lazy from ezhpcy.provision_host import (
    provision_worker_infrastructure,
    validate_worker_infrastructure,
)
lazy from ezhpcy.provision_local import MissingCredentials, WorkerCredentials
lazy from ezhpcy.scheduler import create_scheduler
lazy from ezhpcy.scheduler.base import (
    InteractiveJob,
    JobInfo,
    JobSpec,
    JobState,
    Scheduler,
    SchedulerError,
)
lazy from ezhpcy.ssh import RemoteHost, TCPDestination, ensure_directory
lazy from ezhpcy.tunnel import TunnelError
lazy from ezhpcy.types import JobConfig, RemoteState, SubmissionMode
lazy from ezhpcy.utils import local_machine_id
lazy from ezhpcy.worker.control import (
    JOB_RECORD_NAME,
    WorkerControl,
    WorkerFailed,
    WorkerReady,
    WorkerStatus,
    WorkerStopped,
)
lazy from ezhpcy.worker.job import select_ports, sshd_command
lazy from ezhpcy.worker.sshd import (
    client_alive,
    sshd_config_arguments,
)

logger = get_logger(__name__)
_job_output_logger = get_logger(f"{__name__}.job_output")

# How long one attempt to connect to the worker's sshd may take.
_CONNECT_ATTEMPT_SECONDS = 10.0
# How often to look for the worker's status while it starts. More often buys
# little: the worker takes seconds to start sshd, and each look is a file open
# on the cluster's shared filesystem.
_STATUS_POLL_SECONDS = 1.0
# How long reading the worker's status may take, once its sshd is lost.
_STATUS_READ_SECONDS = 10.0
# How long a background task asked to stop may take to end by itself, and then
# once cancelled.
_STOP_GRACE_SECONDS = 1.0
_TASK_STOP_SECONDS = 10.0

# Prepares more for the job once its control directory exists, before its first
# lease and its submission: the login connection, its remote state, the control
# directory's SFTP session, and the control directory.
type Prepare = Callable[
    [RemoteHost, RemoteState, asyncssh.SFTPClient, WorkerControl], Awaitable[None]
]


class JobLifecycle:
    """
    Runs one worker job on the login connection `host`, from submitting it to
    cleaning up; see the module. `worker_ports` are the ports its sshd tries, by
    default a few random ones. `prepare`, if given, prepares more before the job
    is submitted. `job_name` is the job's name, as the scheduler shows it, and
    `what` names what the job runs in its log ("Stopping job: …").

    Once entered, `credentials` are the worker's SSH credentials for `host`'s
    login node, `worker` is the running worker, and `node_host` the connection
    to its sshd. Entered once.
    """

    def __init__(
        self,
        *,
        host: RemoteHost,
        config: JobConfig,
        worker_ports: tuple[int, ...] = (),
        prepare: Prepare | None = None,
        job_name: str = "ezhpcy-worker",
        what: str = "job",
    ) -> None:
        self.host = host
        self.config = config
        self.credentials: WorkerCredentials
        self._worker_ports = worker_ports or select_ports(DEFAULT_WORKER_PORTS)
        self._prepare = prepare
        self._job_name = job_name
        self._what = what
        self.status = JobStatus(
            resources=config.resources,
            submission_command=config.scheduler.interactive_submission_command,
        )
        self.worker: RunningWorker | None = None
        self.node_host: RemoteHost | None = None
        # Stops the job when the node's connection is lost (`_watch_node`).
        self._node_watcher: asyncio.Task[None] | None = None
        # The job's own: its Host blocks.
        self.attachments = Attachments()
        self.attachments.on_change(self._report_attachments)
        self._stack = AsyncExitStack()
        # Set once the scheduler has said the job ended, so it isn't cancelled,
        # with what it said.
        self._job_finished = False
        self._final_info: JobInfo | None = None
        # The task that entered the job, and how many cancellations it had then.
        self._host_task: asyncio.Task[object] | None = None
        self._host_cancelling = 0
        # Whether the host task waits in `wait`, has been cancelled by the job, or
        # has had the job's end from `wait`.
        self._host_waiting = False
        self._cancelled_host_task = False
        self._end_delivered = False
        # Set once the job is ready, which its end no longer fails.
        self._entered = False
        # Set once leaving has begun: from then on, nothing cancels the host task.
        self._leaving = False
        # Why the job stops, once it does, and the error that stops it, if any.
        self._why: Template | None = None
        self._error: Exception | None = None
        self._stopped = asyncio.Event()

    async def __aenter__(self) -> Self:
        if self._host_task is not None:
            raise RuntimeError("a job is entered once")
        task = asyncio.current_task()
        assert task is not None
        self._host_task = task
        self._host_cancelling = task.cancelling()
        try:
            # Unless `stop` came first, with nothing to clean up.
            if self._why is None:
                await self._start()
        except BaseException as error:
            await self._leave(error, entering=True)
            raise
        if self._why is not None:
            await self._leave(None, entering=True)
        self._entered = True
        self.report(Phase.READY)
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> bool:
        return await self._leave(error, entering=False)

    async def _leave(self, error: BaseException | None, *, entering: bool) -> bool:
        """
        Clean up after `error` (None if the block's code returned), and raise
        what the job's end calls for; or return whether to suppress `error`.
        """
        self._leaving = True
        task = self._host_task
        assert task is not None
        # Taken back, so that the job's cancellation doesn't reach cleanup, and
        # only an outer one shows when it is passed on.
        ours = (
            self._cancelled_host_task
            and task.uncancel() <= self._host_cancelling
            and isinstance(error, asyncio.CancelledError)
        )
        passed_on = error is not None and not ours and not isinstance(error, Exception)
        if isinstance(error, Exception) and not passed_on:
            await self._await_node_loss()
        if self._why is None and not passed_on:
            if isinstance(error, Exception):
                self._stop(self._blame(error))
            else:
                self._stop(None, t"its block was left")
        logger.debug(
            t"Cleaning up the {self._what}: why={self._why} "
            t"job_finished={self._job_finished:detail}"
        )
        await self._stack.aclose()
        if passed_on or self._end_delivered:
            return False
        if (failure := self._error) is not None:
            if failure is error and not entering:
                # The block's own error.
                return False
            raise JobFailed(failure) from failure
        if self._job_finished or entering:
            assert self._why is not None
            # Not chained to the job's own cancellation.
            raise JobEnded(self._why, self._end()) from None
        return ours

    async def wait(self) -> JobEnd:
        """
        Wait until the job stops: return how it ended, or raise `JobFailed` if it
        failed. The host task isn't cancelled while it waits here.
        """
        hosting = asyncio.current_task() is self._host_task
        if self._why is None and hosting:
            self._host_waiting = True
            try:
                await self._stopped.wait()
            finally:
                self._host_waiting = False
        elif self._why is None:
            await self._stopped.wait()
        self._end_delivered = self._end_delivered or hosting
        if self._error is not None:
            raise JobFailed(self._error) from self._error
        return self._end()

    def stop(self, why: Template) -> None:
        """
        Stop the job and clean up, saying `why`; leaving it then raises nothing.
        Call it from the event loop's thread.
        """
        self._stop(None, why)

    def fail(self, error: Exception) -> None:
        """Stop the job, failing with `error`, e.g. when an attachment can't go on."""
        self._stop(error)

    def _stop(self, error: Exception | None, why: Template | None = None) -> None:
        """
        Stop the job, failing with `error` (whose message is `why` by default),
        unless it is stopping already. Every way the job stops logs this one line.
        """
        if why is None:
            why = t"{error}"
        if self._why is not None:
            logger.debug(t"Already stopping; also: {why}")
            return
        self._why, self._error = why, error
        logger.info(t"Stopping {self._what}: {why}")
        self.report(Phase.STOPPING)
        self._stopped.set()
        task = self._host_task
        if (
            task is not None
            and not self._leaving
            and not self._host_waiting
            and asyncio.current_task() is not task
        ):
            task.cancel()
            self._cancelled_host_task = True

    def _end(self) -> JobEnd:
        assert self._why is not None
        info = self._final_info
        return JobEnd(
            reason=plain(self._why),
            by_scheduler=self._job_finished,
            exit_code=info.exit_code if info is not None else None,
        )

    def report(self, phase: Phase | None = None, **changes: Any) -> None:
        """
        Replace `status` with one with `changes`, and in `phase`, if given, since
        now. Once stopping, the job stays stopping.
        """
        if phase is not None and self.status.phase is not Phase.STOPPING:
            changes |= {"phase": phase, "since": time.monotonic()}
        self.status = replace(self.status, **changes)

    async def _await_node_loss(self) -> None:
        """
        Before blaming the block's error, let the node's watcher stop the job if
        the node's connection was lost, as the error may come of that: asyncssh
        fails the connection's channels, and refuses new ones, one step before
        `closed` says it is lost, and the watcher then reads the worker's status
        to say why.
        """
        node_host, watcher = self.node_host, self._node_watcher
        if node_host is None or watcher is None:
            return
        # The connection's cleanup, which resolves `closed`, is already queued
        # when its channels fail: one step lets it run.
        await asyncio.sleep(0)
        if node_host.closed.done() and node_host.closed.result() is not None:
            # It stops the job with its verdict, from its done callback.
            await asyncio.wait([watcher])

    def _blame(self, error: Exception) -> Exception:
        """
        The error to stop with after `error`. Once the login connection is lost,
        every remote operation fails, so report the loss rather than the operation
        that happened to notice it first.
        """
        if not self.host.closed.done():
            return error
        lost = self.host.closed.result()
        if lost is None:
            return error
        connection_error = _connection_lost(lost)
        connection_error.__cause__ = error
        return connection_error

    def attach(self) -> Attachment:
        """
        Register a new attachment of the entered job, such as a Host block,
        closed when the job is left unless it was closed before, after what
        runs on its node; then push its cleanup steps onto it as it acquires
        things.
        """
        if self._leaving:
            raise RuntimeError("the job is being left")
        return self.attachments.attach()

    def _report_attachments(self) -> None:
        node_host = self.node_host
        node = node_host.attachments.statuses if node_host is not None else ()
        self.report(attachments=self.attachments.statuses + node)

    async def _start(self) -> None:
        stack = self._stack
        config, host = self.config, self.host
        resources, timings = config.resources, config.timings
        heartbeat_interval = timings.worker_heartbeat_interval.total_seconds()
        heartbeat_timeout = timings.worker_heartbeat_timeout.total_seconds()
        startup_timeout = timings.worker_startup_timeout.total_seconds()

        self.start_task(self._watch_connection(host), name="connection watcher")
        self.report(Phase.PREPARING)
        credentials = self.credentials = WorkerCredentials.for_login(
            host.login, machine_id=await asyncio.to_thread(local_machine_id)
        )
        if not config.auto_provision and (
            missing := await asyncio.to_thread(credentials.missing_tunnel_files)
        ):
            # Before anything remote.
            raise MissingCredentials(missing)
        remote_state = await host.remote_state()
        if config.auto_provision:
            authorized_key = await provision_worker_infrastructure(
                host, remote_state, credentials
            )
        else:
            await validate_worker_infrastructure(host, remote_state, credentials)
            authorized_key = await asyncio.to_thread(credentials.authorized_key)
        scheduler = create_scheduler(config, host)
        logger.debug(
            t"Worker request: queue={resources.queue!r:detail} "
            t"cores={resources.cores:detail} gpus={resources.gpus:detail} "
            t"exclusive={resources.exclusive:detail} "
            t"time_limit={resources.time_limit:detail} "
            t"memory_bytes={resources.memory_bytes:detail} "
            t"ports={self._worker_ports!s:detail} "
            t"heartbeat_interval={heartbeat_interval:detail:g} "
            t"heartbeat_timeout={heartbeat_timeout:detail:g}"
        )

        # The worker's control directory, with its first lease.
        token = secrets.token_hex(16)
        sftp = await self._enter(host.sftp(), "close the SFTP session")
        control = WorkerControl(
            sftp,
            remote_state.worker_control_root() / token,
            token,
            ports=self._worker_ports,
        )
        # Registered first: `close` also copes with a directory never created.
        stack.push_async_callback(
            cleanup, "remove the worker control directory", control.close
        )
        # Concurrently: each takes a round trip or more.
        await asyncio.gather(
            ensure_directory(sftp, remote_state.worker_cwd_dir()),
            ensure_directory(sftp, remote_state.worker_logs_dir()),
            finish("create the worker control directory", control.create()),
        )
        if self._prepare is not None:
            await self._prepare(host, remote_state, sftp, control)
        await control.publish_lease()

        logs_dir = remote_state.worker_logs_dir()
        spec = JobSpec(
            command=sshd_command(
                remote_state,
                ports=self._worker_ports,
                heartbeat_token=token,
                heartbeat_timeout_seconds=int(heartbeat_timeout),
                control_dir=control.directory,
                cancel_executable=scheduler.cancel_executable,
                sshd_arguments=sshd_config_arguments(
                    host_key=credentials.remote_host_key(remote_state),
                    remote_username=credentials.login_user,
                    authorized_key=authorized_key,
                    client_alive=client_alive(
                        heartbeat_interval, int(heartbeat_timeout)
                    ),
                ),
            ),
            name=self._job_name,
            time_limit=resources.time_limit,
            memory_bytes=resources.memory_bytes,
            cores=resources.cores,
            gpus=resources.gpus,
            exclusive=resources.exclusive,
            queue=resources.queue,
            working_directory=remote_state.worker_cwd_dir(),
            stdout_path=logs_dir / f"worker-{token}.out",
            stderr_path=logs_dir / f"worker-{token}.err",
        )

        # Submit the job, and wait for its sshd.
        def cancel_on_cleanup(job_id: str) -> None:
            stack.push_async_callback(
                cleanup,
                f"cancel worker job {job_id}",
                functools.partial(self._cancel_job, scheduler, job_id),
            )

        self.report(Phase.SUBMITTING)
        interactive_job: InteractiveJob | None = None
        if config.scheduler.submission_mode is SubmissionMode.INTERACTIVE:
            # Safe to cancel: the scheduler then closes the job.
            interactive_job = job = await scheduler.submit_interactive(
                spec, startup_timeout=startup_timeout
            )
            job_id = job.job_id

            def stop_job_output() -> None:
                # Once its job has ended, the submission ends by itself after
                # its last output, which says why: closing it would lose what
                # is still on its way.
                if not self._job_finished:
                    job.close()

            # Closed after its output task stops, in case it didn't end by itself.
            stack.callback(job.close)
            self.start_task(
                _log_job_output(job),
                name="interactive job output",
                stop=stop_job_output,
            )
            cancel_on_cleanup(job_id)
        else:
            job_id = await finish(
                "submit the worker job", scheduler.submit(spec), then=cancel_on_cleanup
            )
        self.report(Phase.QUEUED, job_id=job_id)
        logger.info(t"Submitted worker job {job_id:detail}.")
        logger.info(
            t"Worker heartbeat enabled for job {job_id:detail}: "
            t"interval={heartbeat_interval:detail:g} seconds, "
            t"timeout={heartbeat_timeout:detail:g} seconds."
        )
        logger.info(
            t"Worker heartbeat log: {logs_dir / f'heartbeat-{job_id}.log':path}."
        )
        logger.info(
            t"Worker logs: {spec.stdout_path:path} and {spec.stderr_path:path}."
        )
        if interactive_job is not None:
            logger.debug(
                t"Submission command: "
                t"{shlex.join(interactive_job.submission_command):detail}"
            )
            await interactive_job.start_command()
        # For a later client to find the job by.
        record: dict[str, object] = {
            "job_id": job_id,
            "scheduler": config.scheduler.type,
            "submission_mode": config.scheduler.submission_mode,
        }
        await control.write_record(JOB_RECORD_NAME, json.dumps(record))

        worker_host = await self._wait_for_running_job(scheduler, job_id)
        self.report(Phase.STARTING)
        # From allocation, one budget for the worker's sshd to start and answer.
        deadline = asyncio.get_running_loop().time() + startup_timeout
        ready = await self._wait_for_ready(control, scheduler, job_id, deadline)
        record["sshd_port"] = ready.port
        await control.write_record(JOB_RECORD_NAME, json.dumps(record))
        # Stopped between leases: cancelling one half written would leave its
        # temporary file in the control directory.
        stopping = asyncio.Event()
        self.start_task(
            self._send_heartbeats(control, job_id, stopping),
            name="worker heartbeats",
            stop=stopping.set,
        )
        sshd = TCPDestination(worker_host, ready.port)
        self.report(worker=str(sshd))
        worker = self.worker = RunningWorker(
            sshd=sshd,
            ready=ready,
            control=control,
            scheduler=scheduler,
            job_id=job_id,
            deadline=deadline,
            logs_dir=logs_dir,
        )
        self.start_task(self._monitor_job(scheduler, job_id), name="job monitor")
        # The Host blocks, once attached, are closed here: after what runs on
        # the node, and before the job's end. Each step has its own timeout.
        stack.push_async_callback(self.attachments.close)
        node_host = self.node_host = await self._connect_node(worker)
        stack.push_async_callback(
            cleanup, "close the worker SSH connection", node_host.close
        )
        # What runs on the node, first.
        stack.push_async_callback(node_host.attachments.close)
        node_host.attachments.on_change(self._report_attachments)
        self._node_watcher = self.start_task(
            self._watch_node(node_host, worker), name="worker connection watcher"
        )

    async def check_worker(self) -> None:
        """Raise if the worker has said it stopped, or its job has ended."""
        worker = self.worker
        assert worker is not None
        if error := _worker_gone(await worker.control.read_status()):
            raise error
        await self._check_job_lives(worker.scheduler, worker.job_id)

    async def _wait_for_ready(
        self, control: WorkerControl, scheduler: Scheduler, job_id: str, deadline: float
    ) -> WorkerReady:
        """
        Wait until `deadline` (loop time) for the worker to say it is ready, and
        which port its sshd listens on, checking every job poll interval that the
        job still runs.
        """
        loop = asyncio.get_running_loop()
        poll_interval = self.config.timings.job_poll_interval.total_seconds()
        next_job_check = loop.time() + poll_interval
        try:
            async with asyncio.timeout_at(deadline):
                while True:
                    # The worker's status first: it says more than its job's end.
                    status = await control.read_status()
                    if isinstance(status, WorkerReady):
                        return status
                    if error := _worker_gone(status):
                        raise error
                    if loop.time() >= next_job_check:
                        await self._check_job_lives(scheduler, job_id)
                        next_job_check = loop.time() + poll_interval
                    await asyncio.sleep(_STATUS_POLL_SECONDS)
        except TimeoutError:
            timeout = self.config.timings.worker_startup_timeout.total_seconds()
            raise TunnelError(
                t"worker SSH daemon did not bind a candidate port within "
                t"{timeout:detail:g} seconds"
            ) from None

    async def _connect_node(self, worker: RunningWorker) -> RemoteHost:
        """
        Connect to the worker's sshd through the login connection, as `ssh
        <alias>` does through a tunnel, trying again until the worker's deadline,
        and checking after each failed attempt that sshd and its job still run.
        A host key or client key the worker refuses fails at once: that won't
        come right by waiting.
        """
        timings = self.config.timings
        timeout = timings.worker_startup_timeout.total_seconds()
        poll_interval = timings.job_poll_interval.total_seconds()
        destination = worker.sshd
        endpoint = str(destination)
        logger.info(t"Waiting for worker SSH endpoint {endpoint:endpoint}.")
        last_error: Exception | None = None
        try:
            async with asyncio.timeout_at(worker.deadline):
                while True:
                    try:
                        async with asyncio.timeout(_CONNECT_ATTEMPT_SECONDS):
                            return await self.host.connect_worker(
                                destination.host, destination.port, self.credentials
                            )
                    except asyncssh.HostKeyNotVerifiable, asyncssh.PermissionDenied:
                        raise
                    except TimeoutError:
                        last_error = TunnelError(
                            t"no answer within "
                            t"{_CONNECT_ATTEMPT_SECONDS:detail:g} seconds"
                        )
                    except (asyncssh.Error, OSError) as error:
                        last_error = error
                        logger.debug(
                            t"Worker connection attempt failed: "
                            t"destination={endpoint:endpoint} error={error!r}"
                        )
                    await self.check_worker()
                    await asyncio.sleep(poll_interval)
        except TimeoutError:
            detail = t": {last_error}" if last_error is not None else t""
            raise TunnelError(
                t"worker SSH endpoint {endpoint:endpoint} did not "
                t"become ready within {timeout:detail:g} seconds{detail}"
            ) from None

    async def _watch_node(self, node_host: RemoteHost, worker: RunningWorker) -> None:
        """
        Stop the job when the connection to its node is lost: with the worker's
        word if its sshd stopped, which is what drops the connection then.
        """
        # Shielded: cancelling this task must not cancel `closed`, which others read.
        if (error := await asyncio.shield(node_host.closed)) is None:
            return
        try:
            async with asyncio.timeout(_STATUS_READ_SECONDS):
                status = await worker.control.read_status()
        except (TimeoutError, asyncssh.Error, OSError) as status_error:
            logger.debug(t"Could not read the worker's status: {status_error!r}")
            status = None
        raise _worker_gone(status) or TunnelError(
            t"worker SSH connection lost: {error}"
        )

    async def _wait_for_running_job(self, scheduler: Scheduler, job_id: str) -> str:
        """
        Poll the scheduler until the job runs, and return its host. Says so when
        the job's state changes, and why the job waits, if the scheduler does.
        """
        timings = self.config.timings
        queue_timeout = timings.queue_timeout.total_seconds()
        previous_state: tuple[JobState, str] | None = None
        try:
            async with asyncio.timeout(queue_timeout):
                while True:
                    info = await scheduler.inspect(job_id)
                    self._report_job(info)
                    if (info.state, info.raw_state) != previous_state:
                        previous_state = (info.state, info.raw_state)
                        reason = (
                            f": {info.pending_reason}" if info.pending_reason else ""
                        )
                        logger.info(
                            t"Worker job {job_id:detail}: "
                            t"{info.state.value:detail} "
                            t"{f'({info.raw_state})':aside}{reason}."
                        )
                    if info.state is JobState.RUNNING and info.host is not None:
                        return info.host
                    if ended := self._job_ended(job_id, info):
                        raise TunnelError(ended)
                    await asyncio.sleep(timings.job_poll_interval.total_seconds())
        except TimeoutError:
            raise TunnelError(
                t"worker job {job_id:detail} did not start within "
                t"{queue_timeout:detail:g} seconds"
            ) from None

    def _report_job(self, info: JobInfo) -> None:
        """Report what the scheduler said about the job, and when it began running."""
        running_since = self.status.running_since
        if running_since is None and info.state is JobState.RUNNING:
            running_since = time.monotonic()
        self.report(job=info, running_since=running_since)

    def _job_ended(self, job_id: str, info: JobInfo) -> Template | None:
        """
        If the scheduler says the job has ended, note that it needn't be cancelled,
        and return how it ended, to stop the job with. Raises for a state the job
        can't act on.
        """
        if info.state is JobState.UNKNOWN:
            raise TunnelError(
                t"worker job {job_id:detail} entered unknown scheduler state "
                t"{info.raw_state:detail}"
            )
        if not info.state.is_terminal:
            return None
        self._job_finished = True
        self._final_info = info
        return t"worker job {job_id:detail} ended{_ending(info)}"

    async def _check_job_lives(self, scheduler: Scheduler, job_id: str) -> None:
        """
        Raise if the job has ended while it starts: the worker died, or exited
        before it could say why.
        """
        info = await scheduler.inspect(job_id)
        self._report_job(info)
        if ended := self._job_ended(job_id, info):
            raise TunnelError(ended)

    def start_task(
        self,
        coroutine: Coroutine[object, object, None],
        *,
        name: str,
        stop: Callable[[], object] | None = None,
    ) -> asyncio.Task[None]:
        """
        Run `coroutine` in the background until cleanup gets back to this point. If
        it fails, the job stops with its error.

        Cleanup then cancels it, or with `stop`, calls `stop` to ask it to end and
        gives it a moment to do so first.
        """
        # The coroutine is the task itself, so that one cancelled before it began
        # is closed, rather than never awaited.
        task = asyncio.create_task(coroutine, name=name)
        task.add_done_callback(self._stop_on_failure)
        self._stack.push_async_callback(_stop_task, task, stop)
        return task

    def _stop_on_failure(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        if isinstance(error := task.exception(), Exception):
            self._stop(self._blame(error))

    async def _watch_connection(self, host: RemoteHost) -> None:
        """Stop the job when the login connection is lost."""
        # Shielded: cancelling this task must not cancel `closed`, which others read.
        if (error := await asyncio.shield(host.closed)) is not None:
            self._stop(_connection_lost(error))

    async def _send_heartbeats(
        self, control: WorkerControl, job_id: str, stopping: asyncio.Event
    ) -> None:
        """Renew the worker's lease every heartbeat interval, until `stopping`."""
        timings = self.config.timings
        interval_seconds = timings.worker_heartbeat_interval.total_seconds()
        sequence = 0
        logger.debug(
            t"Worker heartbeat sender started: job={job_id:detail} "
            t"interval_seconds={interval_seconds:detail:g}"
        )
        try:
            while True:
                started = time.monotonic()
                try:
                    sequence = await control.publish_lease()
                except (asyncssh.Error, OSError) as error:
                    raise TunnelError(
                        t"worker heartbeat write failed for job {job_id:detail} at "
                        t"sequence {sequence:detail}: {error}"
                    ) from error
                self.report(last_heartbeat=time.monotonic())
                elapsed = time.monotonic() - started
                logger.debug(
                    t"Worker heartbeat published: job={job_id:detail} "
                    t"sequence={sequence:detail} elapsed_seconds={elapsed:detail:.3f}"
                )
                try:
                    async with asyncio.timeout(interval_seconds):
                        await stopping.wait()
                except TimeoutError:
                    continue
                return
        finally:
            logger.debug(
                t"Worker heartbeat sender stopped: job={job_id:detail} "
                t"last_sequence={sequence:detail}"
            )

    async def _monitor_job(self, scheduler: Scheduler, job_id: str) -> None:
        """
        Stop the job when the scheduler says it has ended; fail it if the job
        ends while the connection to its node is being made.
        """
        interval_seconds = self.config.timings.job_monitor_interval.total_seconds()
        polls = 0
        while True:
            await asyncio.sleep(interval_seconds)
            polls += 1
            # Single strike: a failed query, or an unknown state, stops the job.
            try:
                info = await scheduler.inspect(job_id)
            except SchedulerError as error:
                raise TunnelError(
                    t"could not monitor worker job {job_id:detail}: {error}"
                ) from error
            self._report_job(info)
            logger.debug(
                t"Job monitor poll: job={job_id:detail} poll={polls:detail} "
                t"state={info.state.name:detail} raw_state={info.raw_state:detail} "
                t"host={info.host:detail} exit_code={info.exit_code:detail}"
            )
            if ended := self._job_ended(job_id, info):
                if not self._entered:
                    # Before the job was ready, as `_check_job_lives` does.
                    raise TunnelError(ended)
                self._stop(None, ended)
                return

    async def _cancel_job(self, scheduler: Scheduler, job_id: str) -> None:
        """Cancel the job, unless the scheduler said it had ended."""
        if self._job_finished:
            return
        await scheduler.cancel(job_id)
        logger.info(t"Cancelled worker job {job_id:detail}.")

    async def _enter[T](
        self, context: AbstractAsyncContextManager[T], description: str
    ) -> T:
        """Enter `context`, and leave it as a cleanup step."""
        context_stack = AsyncExitStack()
        value = await context_stack.enter_async_context(context)
        self._stack.push_async_callback(cleanup, description, context_stack.aclose)
        return value


@dataclass(frozen=True, slots=True)
class RunningWorker:
    """The worker job, once it has said it is ready."""

    # Its sshd, as the login node reaches it.
    sshd: TCPDestination
    ready: WorkerReady
    control: WorkerControl
    scheduler: Scheduler
    job_id: str
    # When sshd must answer by (loop time).
    deadline: float
    logs_dir: PurePosixPath


async def _log_job_output(job: InteractiveJob) -> None:
    """
    Log an interactive job's output, which echoes the long worker script, a line at
    a time at DEBUG. The terminal shows it with debug logging, or before the error
    when the job fails (`REPLAY_ON_ERROR`). Reads all of it: unread output would
    stall the job once SSH's window fills.
    """
    # Incremental, so that a character split across chunks isn't garbled.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    # The line read so far, up to its newline.
    partial = ""

    async def log_lines(output: bytes) -> None:
        nonlocal partial
        *lines, partial = (partial + decoder.decode(output)).split("\n")
        for line in lines:
            _job_output_logger.debug(_as_displayed(line), extra=REPLAY_ON_ERROR)

    try:
        await job.drain(log_lines)
    except (asyncssh.Error, OSError) as error:
        logger.debug(t"Interactive job output ended: error={error!r}")
    if last := partial + decoder.decode(b"", final=True):
        _job_output_logger.debug(_as_displayed(last), extra=REPLAY_ON_ERROR)


def _as_displayed(line: str) -> str:
    """
    A line of terminal output as the terminal shows it: without escape sequences
    or control characters, and from its last carriage return, which writes over
    what came before. Such as bash's echo of the worker script, around bracketed
    paste.
    """
    # Without a line ending's carriage return, which Rich would take for one that
    # writes over the whole line.
    return Text.from_ansi(line.removesuffix("\r")).plain


async def _stop_task(
    task: asyncio.Task[None], stop: Callable[[], object] | None
) -> None:
    """Cancel a background task, unless `stop` makes it end soon enough by itself."""
    if stop is not None:
        stop()
        await asyncio.wait([task], timeout=_STOP_GRACE_SECONDS)
    task.cancel()
    await asyncio.wait([task], timeout=_TASK_STOP_SECONDS)
    if not task.done():
        logger.warning(t"The {task.get_name()} did not stop.")


def _worker_gone(status: WorkerStatus | None) -> TunnelError | None:
    """The error to stop with if `status` says sshd won't serve, else None."""
    match status:
        case WorkerFailed(reason):
            return TunnelError(t"worker SSH daemon failed to start: {reason}")
        case WorkerStopped(exit_status):
            return TunnelError(
                t"worker SSH daemon exited with status {exit_status:detail}"
            )
        case _:
            return None


def _connection_lost(error: Exception) -> TunnelError:
    return TunnelError(t"login-node SSH connection lost: {error}")


def _ending(info: JobInfo) -> Template:
    """How a job ended, as far as the scheduler still says (PBS may not)."""
    state = t" in scheduler state {info.raw_state:detail}" if info.raw_state else t""
    exit_code = (
        t" with exit code {info.exit_code:detail}"
        if info.exit_code is not None
        else t""
    )
    return state + exit_code
