"""
One tunnel, from connecting to the login node until everything is cleaned up.

`TunnelSession.run` takes the alias lock, connects, provisions (or checks) the
worker infrastructure, submits the worker job, waits for its sshd, forwards a
loopback port to it and publishes the Host block that points OpenSSH there, then
serves until it is stopped. Each step registers its cleanup on an
`AsyncExitStack` as soon as it has acquired something, so cleanup covers exactly
what was started, in reverse order. Each cleanup step has a timeout, and one that
fails only logs a warning.

The tunnel stops when `stop` is called (the CLI calls it on Ctrl+C), when the
scheduler says the job has ended, or when something fails: a startup step, or a
background task (the connection watcher, the heartbeats, the job monitor). The
first of these wins: it is logged as `Stopping tunnel: <why>`, decides the error
`run` raises, if any, and cancels the main task, which then cleans up.
"""

lazy import asyncio
lazy import codecs
lazy import functools
lazy import secrets
lazy import shlex
lazy import time
lazy from collections.abc import Awaitable, Callable, Coroutine
lazy from contextlib import AbstractAsyncContextManager, AsyncExitStack, ExitStack
lazy from string.templatelib import Template

lazy import asyncssh
lazy from rich.text import Text

from ezhpcy.logging import REPLAY_ON_ERROR, get_logger
lazy from ezhpcy.provision_host import (
    provision_worker_infrastructure,
    validate_worker_infrastructure,
)
lazy from ezhpcy.scheduler import create_scheduler
lazy from ezhpcy.scheduler.base import (
    InteractiveJob,
    JobInfo,
    JobSpec,
    JobState,
    Scheduler,
    SchedulerError,
)
lazy from ezhpcy.ssh import RemoteHost, ensure_directory
lazy from ezhpcy.tunnel import TunnelError
lazy from ezhpcy.tunnel.lock import alias_lock
lazy from ezhpcy.tunnel.ssh_config import WorkerHost, publish_host, withdraw_host
lazy from ezhpcy.types import ConnectionInfo, ResolvedConfig, SubmissionMode
lazy from ezhpcy.worker.control import (
    WorkerControl,
    WorkerFailed,
    WorkerReady,
    WorkerStatus,
    WorkerStopped,
)
lazy from ezhpcy.worker.job import sshd_command
lazy from ezhpcy.worker.sshd import (
    client_alive,
    sshd_config_arguments,
)

logger = get_logger(__name__)
_job_output_logger = get_logger(f"{__name__}.job_output")

_SSH_BANNER_LIMIT = 255
# How long one attempt to read the worker's SSH banner may take.
_BANNER_READ_SECONDS = 5.0
# How often to look for the worker's status while it starts. More often buys
# little: the worker takes seconds to start sshd, and each look is a file open
# on the cluster's shared filesystem.
_STATUS_POLL_SECONDS = 1.0
# How long each cleanup step may take, so that a dead connection can't hang it.
_CLEANUP_SECONDS = 10.0
# Releasing the alias lock is a local unlock; the OS releases it at exit anyway.
_LOCK_RELEASE_SECONDS = 1.0
# How long a background task asked to stop may take to end by itself.
_STOP_GRACE_SECONDS = 1.0


class TunnelSession:
    """
    Runs one tunnel to a worker job. `connect` opens the login connection; the
    CLI's asks for passwords and host keys as needed. `on_ready` is awaited once
    the tunnel serves; the CLI's prints a banner, from a thread (see
    `ezhpcy.console`).
    """

    def __init__(
        self,
        *,
        resolved: ResolvedConfig,
        ssh_host: WorkerHost,
        connect: Callable[[ConnectionInfo], Awaitable[RemoteHost]],
        on_ready: Callable[[], Awaitable[object]],
        worker_ports: tuple[int, ...],
        auto_provision: bool,
        profile_name: str | None = None,
    ) -> None:
        self._resolved = resolved
        self._ssh_host = ssh_host
        self._connect = connect
        self._on_ready = on_ready
        self._worker_ports = worker_ports
        self._auto_provision = auto_provision
        self._profile_name = profile_name
        # Whether the tunnel got as far as serving.
        self.ready = False
        self._host: RemoteHost | None = None
        # Set once the scheduler has said the job ended, so it isn't cancelled.
        self._job_finished = False
        self._main_task: asyncio.Task[object] | None = None
        # Why the tunnel stops, once it does, and the error that stops it, if any.
        self._why: Template | None = None
        self._error: Exception | None = None
        self._cancelled_main_task = False

    async def run(self) -> None:
        """
        Start the tunnel and serve until it stops, then clean up.

        Returns when it stopped without failing: `stop` was called, or the worker
        job ended. Otherwise raises the error that stopped it.
        """
        main_task = self._main_task = asyncio.current_task()
        assert main_task is not None
        async with AsyncExitStack() as stack:
            try:
                await self._start(stack)
                # Serve until `_stop` cancels this task.
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if not self._cancelled_main_task:
                    # Cancelled by the caller: clean up, then pass it on.
                    raise
            # ruff: ignore[BLE001]  # Raised again below
            except Exception as error:
                self._stop(self._blame(error))
            # A background task may have stopped the tunnel as this task failed.
            if self._cancelled_main_task:
                main_task.uncancel()
            logger.debug(
                t"Cleaning up the tunnel: why={self._why} "
                t"job_finished={self._job_finished:detail}"
            )
        if self._error is not None:
            raise self._error

    def stop(self, why: Template) -> None:
        """
        Stop the tunnel and clean up, saying `why`; `run` then returns. Call it
        while `run` runs.
        """
        self._stop(None, why)

    def _stop(self, error: Exception | None, why: Template | None = None) -> None:
        """
        Stop the tunnel, failing with `error` (whose message is `why` by default),
        unless it is stopping already. Every way the tunnel stops logs this one line.
        """
        if why is None:
            why = t"{error}"
        if self._why is not None:
            logger.debug(t"Tunnel already stopping; also: {why}")
            return
        self._why, self._error = why, error
        logger.info(t"Stopping tunnel: {why}")
        assert self._main_task is not None
        if asyncio.current_task() is not self._main_task:
            self._main_task.cancel()
            self._cancelled_main_task = True

    def _blame(self, error: Exception) -> Exception:
        """
        The error to stop with after `error`. Once the login connection is lost,
        every remote operation fails, so report the loss rather than the operation
        that happened to notice it first.
        """
        if self._host is None or not self._host.closed.done():
            return error
        lost = self._host.closed.result()
        if lost is None:
            return error
        connection_error = _connection_lost(lost)
        connection_error.__cause__ = error
        return connection_error

    async def _start(self, stack: AsyncExitStack) -> None:
        resolved = self._resolved
        resources, timings = resolved.resources, resolved.timings

        # Before connecting, so a second tunnel for the alias fails before any
        # password prompt; released last.
        lock = ExitStack()
        lock.enter_context(alias_lock(self._ssh_host.alias))
        stack.push_async_callback(
            self._cleanup,
            "release the alias lock",
            lambda: asyncio.to_thread(lock.close),
            seconds=_LOCK_RELEASE_SECONDS,
        )
        host = self._host = await self._connect(resolved.connection)
        stack.push_async_callback(
            self._cleanup, "close the login connection", host.close
        )
        self._start_task(stack, self._watch_connection(host), name="connection watcher")

        remote_state = await host.remote_state()
        credentials = self._ssh_host.credentials
        if self._auto_provision:
            authorized_key = await provision_worker_infrastructure(
                host, remote_state, credentials
            )
        else:
            await validate_worker_infrastructure(host, remote_state, credentials)
            authorized_key = await asyncio.to_thread(credentials.authorized_key)
        scheduler = create_scheduler(resolved, host)
        if self._profile_name is None:
            logger.info(
                t"Using anonymous configuration with "
                t"{resolved.scheduler.type:detail} scheduler."
            )
        else:
            logger.info(
                t"Using profile {self._profile_name:name} with "
                t"{resolved.scheduler.type:detail} scheduler."
            )
        logger.debug(
            t"Worker request: queue={resources.queue!r:detail} "
            t"cores={resources.cores:detail} gpus={resources.gpus:detail} "
            t"exclusive={resources.exclusive:detail} "
            t"time_limit={resources.time_limit:detail} "
            t"memory_bytes={resources.memory_bytes:detail} "
            t"ports={self._worker_ports!s:detail} "
            t"heartbeat_interval={timings.worker_heartbeat_interval_seconds:detail:g} "
            t"heartbeat_timeout={timings.worker_heartbeat_timeout_seconds:detail:g}"
        )

        # The worker's control directory, with its first lease.
        token = secrets.token_hex(16)
        sftp = await self._enter(stack, host.sftp(), "close the SFTP session")
        control = WorkerControl(
            sftp,
            remote_state.worker_control_root() / token,
            token,
            ports=self._worker_ports,
        )
        # Registered first: `close` also copes with a directory never created.
        stack.push_async_callback(
            self._cleanup, "remove the worker control directory", control.close
        )
        # Concurrently: each takes a round trip or more.
        await asyncio.gather(
            ensure_directory(sftp, remote_state.worker_cwd_dir()),
            ensure_directory(sftp, remote_state.worker_logs_dir()),
            self._finish("create the worker control directory", control.create()),
        )
        await control.publish_lease()

        logs_dir = remote_state.worker_logs_dir()
        spec = JobSpec(
            command=sshd_command(
                remote_state,
                ports=self._worker_ports,
                heartbeat_token=token,
                heartbeat_timeout_seconds=timings.worker_heartbeat_timeout_seconds,
                control_dir=control.directory,
                sshd_arguments=sshd_config_arguments(
                    host_key=credentials.remote_host_key(remote_state),
                    remote_username=credentials.login_user,
                    authorized_key=authorized_key,
                    client_alive=client_alive(
                        timings.worker_heartbeat_interval_seconds,
                        timings.worker_heartbeat_timeout_seconds,
                    ),
                ),
            ),
            name="ezhpcy-worker",
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
                self._cleanup,
                f"cancel worker job {job_id}",
                functools.partial(self._cancel_job, scheduler, job_id),
            )

        interactive_job: InteractiveJob | None = None
        if resolved.scheduler.submission_mode is SubmissionMode.INTERACTIVE:
            # Safe to cancel: the scheduler then closes the job.
            interactive_job = await scheduler.submit_interactive(
                spec, startup_timeout=timings.worker_startup_timeout_seconds
            )
            job_id = interactive_job.job_id
            # Stopped by closing the job, after its last output.
            self._start_task(
                stack,
                _log_job_output(interactive_job),
                name="interactive job output",
                stop=interactive_job.close,
            )
            cancel_on_cleanup(job_id)
        else:
            job_id = await self._finish(
                "submit the worker job", scheduler.submit(spec), then=cancel_on_cleanup
            )
        logger.info(t"Submitted worker job {job_id:detail}.")
        logger.info(
            t"Worker heartbeat enabled for job {job_id:detail}: "
            t"interval={timings.worker_heartbeat_interval_seconds:detail:g} seconds, "
            t"timeout={timings.worker_heartbeat_timeout_seconds:detail:g} seconds."
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

        worker_host = await self._wait_for_running_job(scheduler, job_id)
        # From allocation, one budget for the worker's sshd to start and answer.
        deadline = (
            asyncio.get_running_loop().time() + timings.worker_startup_timeout_seconds
        )
        worker_port = await self._wait_for_ready_port(
            control, scheduler, job_id, deadline
        )
        # Stopped between leases: cancelling one half written would leave its
        # temporary file in the control directory.
        stopping = asyncio.Event()
        self._start_task(
            stack,
            self._send_heartbeats(control, job_id, stopping),
            name="worker heartbeats",
            stop=stopping.set,
        )
        destination = (worker_host, worker_port)
        endpoint = f"{worker_host}:{worker_port}"
        logger.info(t"Waiting for worker SSH endpoint {endpoint:endpoint}.")
        await self._wait_for_worker_endpoint(
            destination, control, scheduler, job_id, deadline
        )

        # Serve on a loopback port, and tell OpenSSH how to reach it.
        def accept(client_host: str, client_port: int) -> bool:
            logger.debug(
                t"Tunnel client accepted: "
                t"client={f'{client_host}:{client_port}':endpoint} "
                t"destination={endpoint:endpoint}"
            )
            return True

        listener = await host.forward_local_port(*destination, accept_handler=accept)

        async def close_listener() -> None:
            # New connections are refused at once. Open ones end when the login
            # connection closes; `wait_closed` would wait for them (FINDINGS.md,
            # "Phase 1").
            listener.close()

        stack.push_async_callback(
            self._cleanup, "close the forwarded port", close_listener
        )
        port = listener.get_port()
        logger.debug(
            t"Forwarding port: port={port:detail} destination={endpoint:endpoint}"
        )
        # Registered before the block is written, and withdrawn before the port
        # closes, so that a Host block never outlives the port it points at.
        stack.push_async_callback(
            self._cleanup,
            "remove the SSH configuration",
            lambda: asyncio.to_thread(withdraw_host, self._ssh_host),
        )
        await self._finish(
            "publish the SSH configuration",
            asyncio.to_thread(publish_host, self._ssh_host, port),
        )
        self._start_task(
            stack, self._monitor_job(scheduler, job_id), name="job monitor"
        )
        self.ready = True
        alias = self._ssh_host.alias
        logger.info(
            t"Tunnel ready for {alias:name} via worker {endpoint:endpoint} "
            t"{'(press Ctrl+C to stop)':aside}."
        )
        logger.info(
            t"Connect with {f'ssh {alias}':suggestion} or select "
            t"{alias:suggestion} in your editor's remote development interface."
        )
        await self._on_ready()

    async def _wait_for_running_job(self, scheduler: Scheduler, job_id: str) -> str:
        """
        Poll the scheduler until the job runs, and return its host. Says so when
        the job's state changes, and otherwise every queue progress interval how
        long it has waited, so that a long wait in the queue doesn't look like a
        hang. Both say why the job waits, if the scheduler does.
        """
        timings = self._resolved.timings
        progress_interval = timings.queue_progress_interval_seconds
        loop = asyncio.get_running_loop()
        started = loop.time()
        next_progress = started + progress_interval
        previous_state: tuple[JobState, str] | None = None
        try:
            async with asyncio.timeout(timings.queue_timeout_seconds):
                while True:
                    info = await scheduler.inspect(job_id)
                    reason = f": {info.pending_reason}" if info.pending_reason else ""
                    if (info.state, info.raw_state) != previous_state:
                        previous_state = (info.state, info.raw_state)
                        next_progress = loop.time() + progress_interval
                        logger.info(
                            t"Worker job {job_id:detail}: "
                            t"{info.state.value:detail} "
                            t"{f'({info.raw_state})':aside}{reason}."
                        )
                    if info.state is JobState.RUNNING and info.host is not None:
                        return info.host
                    if ended := self._job_ended(job_id, info):
                        raise TunnelError(ended)
                    if loop.time() >= next_progress:
                        next_progress = loop.time() + progress_interval
                        waited = loop.time() - started
                        timeout_in = _duration(timings.queue_timeout_seconds - waited)
                        logger.info(
                            t"Worker job {job_id:detail} still "
                            t"{info.state.value:detail} after "
                            t"{_duration(waited):detail} "
                            t"{t'(queue timeout in {timeout_in:detail})':aside}"
                            t"{reason}."
                        )
                    await asyncio.sleep(timings.job_poll_interval_seconds)
        except TimeoutError:
            raise TunnelError(
                t"worker job {job_id:detail} did not start within "
                t"{timings.queue_timeout_seconds:detail:g} seconds"
            ) from None

    def _job_ended(self, job_id: str, info: JobInfo) -> Template | None:
        """
        If the scheduler says the job has ended, note that it needn't be cancelled,
        and return how it ended, to stop the tunnel with. Raises for a state the
        tunnel can't act on.
        """
        if info.state is JobState.UNKNOWN:
            raise TunnelError(
                t"worker job {job_id:detail} entered unknown scheduler state "
                t"{info.raw_state:detail}"
            )
        if not info.state.is_terminal:
            return None
        self._job_finished = True
        return t"worker job {job_id:detail} ended{_ending(info)}"

    async def _check_job_lives(self, scheduler: Scheduler, job_id: str) -> None:
        """
        Raise if the job has ended while the tunnel starts: the worker died, or
        exited before it could say why.
        """
        if ended := self._job_ended(job_id, await scheduler.inspect(job_id)):
            raise TunnelError(ended)

    async def _wait_for_ready_port(
        self, control: WorkerControl, scheduler: Scheduler, job_id: str, deadline: float
    ) -> int:
        """
        Wait until `deadline` (loop time) for the worker to say which port its sshd
        listens on, checking every job poll interval that the job still runs.
        """
        loop = asyncio.get_running_loop()
        poll_interval = self._resolved.timings.job_poll_interval_seconds
        next_job_check = loop.time() + poll_interval
        try:
            async with asyncio.timeout_at(deadline):
                while True:
                    # The worker's status first: it says more than its job's end.
                    status = await control.read_status()
                    if isinstance(status, WorkerReady):
                        return status.port
                    if error := _worker_gone(status):
                        raise error
                    if loop.time() >= next_job_check:
                        await self._check_job_lives(scheduler, job_id)
                        next_job_check = loop.time() + poll_interval
                    await asyncio.sleep(_STATUS_POLL_SECONDS)
        except TimeoutError:
            timeout = self._resolved.timings.worker_startup_timeout_seconds
            raise TunnelError(
                t"worker SSH daemon did not bind a candidate port within "
                t"{timeout:detail:g} seconds"
            ) from None

    async def _wait_for_worker_endpoint(
        self,
        destination: tuple[str, int],
        control: WorkerControl,
        scheduler: Scheduler,
        job_id: str,
        deadline: float,
    ) -> None:
        """
        Wait until `deadline` (loop time) for the worker's sshd to send its banner
        through the login connection, checking after each failed attempt that sshd
        and its job still run.
        """
        timeout = self._resolved.timings.worker_startup_timeout_seconds
        poll_interval = self._resolved.timings.job_poll_interval_seconds
        endpoint = f"{destination[0]}:{destination[1]}"
        last_error: Exception | None = None
        try:
            async with asyncio.timeout_at(deadline):
                while True:
                    try:
                        async with asyncio.timeout(_BANNER_READ_SECONDS):
                            banner = await self._read_banner(destination)
                    except TimeoutError:
                        last_error = TunnelError(
                            t"no SSH banner within "
                            t"{_BANNER_READ_SECONDS:detail:g} seconds"
                        )
                    except (asyncssh.Error, OSError) as error:
                        last_error = error
                        logger.debug(
                            t"Worker readiness attempt failed: "
                            t"destination={endpoint:endpoint} error={error!r}"
                        )
                    else:
                        if banner.startswith(b"SSH-"):
                            return
                        last_error = TunnelError(
                            t"unexpected worker banner {banner!r:detail}"
                        )
                        logger.debug(
                            t"Worker readiness returned an unexpected banner: "
                            t"destination={endpoint:endpoint} banner={banner!r:detail}"
                        )
                    if error := _worker_gone(await control.read_status()):
                        raise error
                    await self._check_job_lives(scheduler, job_id)
                    await asyncio.sleep(poll_interval)
        except TimeoutError:
            detail = t": {last_error}" if last_error is not None else t""
            raise TunnelError(
                t"worker SSH endpoint {endpoint:endpoint} did not "
                t"become ready within {timeout:detail:g} seconds{detail}"
            ) from None

    async def _read_banner(self, destination: tuple[str, int]) -> bytes:
        assert self._host is not None
        reader, writer = await self._host.open_tcp(*destination)
        try:
            banner = bytearray()
            while len(banner) < _SSH_BANNER_LIMIT and b"\n" not in banner:
                chunk = await reader.read(_SSH_BANNER_LIMIT - len(banner))
                if not chunk:
                    break
                banner.extend(chunk)
            return bytes(banner)
        finally:
            writer.close()

    # Background tasks

    def _start_task(
        self,
        stack: AsyncExitStack,
        coroutine: Coroutine[object, object, None],
        *,
        name: str,
        stop: Callable[[], object] | None = None,
    ) -> None:
        """
        Run `coroutine` in the background until cleanup gets back to this point. If
        it fails, the tunnel stops with its error.

        Cleanup then cancels it, or with `stop`, calls `stop` to ask it to end and
        gives it a moment to do so first.
        """
        task = asyncio.create_task(self._stop_on_failure(coroutine), name=name)
        stack.push_async_callback(_stop_task, task, stop)

    async def _stop_on_failure(
        self, coroutine: Coroutine[object, object, None]
    ) -> None:
        try:
            await coroutine
        # ruff: ignore[BLE001]  # Raised again by `run`
        except Exception as error:
            self._stop(self._blame(error))

    async def _watch_connection(self, host: RemoteHost) -> None:
        """Stop the tunnel when the login connection is lost."""
        # Shielded: cancelling this task must not cancel `closed`, which others read.
        if (error := await asyncio.shield(host.closed)) is not None:
            self._stop(_connection_lost(error))

    async def _send_heartbeats(
        self, control: WorkerControl, job_id: str, stopping: asyncio.Event
    ) -> None:
        """Renew the worker's lease every heartbeat interval, until `stopping`."""
        interval_seconds = self._resolved.timings.worker_heartbeat_interval_seconds
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
        """Stop the tunnel when the scheduler says the job has ended."""
        interval_seconds = self._resolved.timings.job_monitor_interval_seconds
        polls = 0
        while True:
            await asyncio.sleep(interval_seconds)
            polls += 1
            # Single strike: a failed query, or an unknown state, stops the tunnel.
            try:
                info = await scheduler.inspect(job_id)
            except SchedulerError as error:
                raise TunnelError(
                    t"could not monitor worker job {job_id:detail}: {error}"
                ) from error
            logger.debug(
                t"Job monitor poll: job={job_id:detail} poll={polls:detail} "
                t"state={info.state.name:detail} raw_state={info.raw_state:detail} "
                t"host={info.host:detail} exit_code={info.exit_code:detail}"
            )
            if ended := self._job_ended(job_id, info):
                self._stop(None, ended)
                return

    # Cleanup

    async def _finish[T](
        self,
        description: str,
        step: Awaitable[T],
        *,
        then: Callable[[T], object] | None = None,
    ) -> T:
        """
        Await `step`, then call `then` with its result, e.g. to register a cleanup
        step for what `step` acquired.

        Cancelled midway, a step may still take effect (a job queued by a `bsub`
        whose output never arrived, a file written by a thread), with nothing to
        undo it. So if the tunnel stops meanwhile, `step` still finishes first,
        within the cleanup timeout, and `then` is called; only then does the stop
        proceed.
        """
        task = asyncio.ensure_future(step)
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            logger.debug(t"Waiting to {description} before stopping.")
            done, _ = await asyncio.wait([task], timeout=_CLEANUP_SECONDS)
            if not done:
                task.cancel()
                logger.warning(
                    t"Gave up waiting to {description} after "
                    t"{_CLEANUP_SECONDS:detail:g} seconds; it may have left "
                    t"something behind."
                )
            # A step that failed acquired nothing, and the tunnel stops anyway.
            elif task.exception() is None and then is not None:
                then(task.result())
            raise
        if then is not None:
            then(result)
        return result

    async def _cancel_job(self, scheduler: Scheduler, job_id: str) -> None:
        """Cancel the job, unless the scheduler said it had ended."""
        if self._job_finished:
            return
        await scheduler.cancel(job_id)
        logger.info(t"Cancelled worker job {job_id:detail}.")

    async def _enter[T](
        self,
        stack: AsyncExitStack,
        context: AbstractAsyncContextManager[T],
        description: str,
    ) -> T:
        """Enter `context`, and leave it as a cleanup step."""
        context_stack = AsyncExitStack()
        value = await context_stack.enter_async_context(context)
        stack.push_async_callback(self._cleanup, description, context_stack.aclose)
        return value

    async def _cleanup(
        self,
        description: str,
        step: Callable[[], Awaitable[object]],
        *,
        seconds: float | None = None,
    ) -> None:
        """
        Run one cleanup step, for at most `seconds` (by default `_CLEANUP_SECONDS`);
        if it fails, warn and carry on.
        """
        seconds = _CLEANUP_SECONDS if seconds is None else seconds
        try:
            async with asyncio.timeout(seconds):
                await step()
        except TimeoutError:
            logger.warning(
                t"Could not {description}: no answer within {seconds:detail:g} seconds."
            )
        except Exception as error:
            logger.warning(
                t"Could not {description}: {error}",
                exc_info=True,
            )


async def _log_job_output(job: InteractiveJob) -> None:
    """
    Log an interactive job's output, which echoes the long worker script, a line at
    a time at DEBUG. The terminal shows it with debug logging, or before the error
    when the tunnel fails (`REPLAY_ON_ERROR`). Reads all of it: unread output
    would stall the job once SSH's window fills.
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
    await asyncio.wait([task], timeout=_CLEANUP_SECONDS)
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


def _duration(seconds: float) -> str:
    """`seconds` as `2 min 30 s`, or `45 s` under a minute."""
    minutes, seconds = divmod(round(seconds), 60)
    return f"{minutes} min {seconds} s" if minutes else f"{seconds} s"


def _ending(info: JobInfo) -> Template:
    """How a job ended, as far as the scheduler still says (PBS may not)."""
    state = t" in scheduler state {info.raw_state:detail}" if info.raw_state else t""
    exit_code = (
        t" with exit code {info.exit_code:detail}"
        if info.exit_code is not None
        else t""
    )
    return state + exit_code
