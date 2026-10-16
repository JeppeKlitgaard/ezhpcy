"""
One tunnel, from connecting to the login node until everything is cleaned up.

`TunnelSession.run` connects, provisions (or checks) the worker infrastructure,
submits the worker job, waits for its sshd, and starts the tunnel server, then
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

import logging
lazy import asyncio
lazy import codecs
lazy import functools
lazy import secrets
lazy import shlex
lazy import sys
lazy import time
lazy from collections.abc import Awaitable, Callable, Coroutine
lazy from contextlib import AbstractAsyncContextManager, AsyncExitStack

lazy import asyncssh

lazy from ezhpcy.ipc.descriptor import TunnelDescriptor
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
lazy from ezhpcy.scheduler.types import SchedulerType
lazy from ezhpcy.ssh import RemoteHost, ensure_directory
lazy from ezhpcy.tunnel import TunnelError
lazy from ezhpcy.tunnel.server import TunnelServer
lazy from ezhpcy.tunnel.ssh_config import WorkerHost, publish_host, withdraw_host
lazy from ezhpcy.types import ConnectionInfo, ResolvedConfig, SubmissionMode
lazy from ezhpcy.worker.control import WorkerControl
lazy from ezhpcy.worker.job import (
    WORKER_SCRIPT,
    batch_script,
    sshd_command,
)
lazy from ezhpcy.worker.sshd import (
    client_alive,
    sshd_config_arguments,
)

logger = logging.getLogger(__name__)

_SSH_BANNER_LIMIT = 255
# How long one attempt to read the worker's SSH banner may take.
_BANNER_READ_SECONDS = 5.0
_READY_POLL_SECONDS = 0.1
# How long each cleanup step may take, so that a dead connection can't hang it.
_CLEANUP_SECONDS = 10.0
# How long a background task asked to stop may take to end by itself.
_STOP_GRACE_SECONDS = 1.0


class TunnelSession:
    """
    Runs one tunnel to a worker job. `connect` opens the login connection; the
    CLI's asks for passwords and host keys as needed.
    """

    def __init__(
        self,
        *,
        resolved: ResolvedConfig,
        ssh_host: WorkerHost,
        connect: Callable[[ConnectionInfo], Awaitable[RemoteHost]],
        worker_ports: tuple[int, ...],
        auto_provision: bool,
        profile_name: str | None = None,
        ssh_host_in_profiles_config: bool = False,
    ) -> None:
        self._resolved = resolved
        self._ssh_host = ssh_host
        self._connect = connect
        self._worker_ports = worker_ports
        self._auto_provision = auto_provision
        self._profile_name = profile_name
        self._ssh_host_in_profiles_config = ssh_host_in_profiles_config
        # Whether the tunnel got as far as serving.
        self.ready = False
        self._host: RemoteHost | None = None
        # Set once the scheduler has said the job ended, so it isn't cancelled.
        self._job_finished = False
        self._main_task: asyncio.Task[object] | None = None
        # Why the tunnel stops, once it does, and the error that stops it, if any.
        self._why: str | None = None
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
                "Cleaning up the tunnel: why=%s job_finished=%s",
                self._why,
                self._job_finished,
            )
        if self._error is not None:
            raise self._error

    def stop(self, why: str) -> None:
        """
        Stop the tunnel and clean up, saying `why`; `run` then returns. Call it
        while `run` runs.
        """
        self._stop(None, why)

    def _stop(self, error: Exception | None, why: str | None = None) -> None:
        """
        Stop the tunnel, failing with `error` (whose message is `why` by default),
        unless it is stopping already. Every way the tunnel stops logs this one line.
        """
        why = why or str(error)
        if self._why is not None:
            logger.debug("Tunnel already stopping; also: %s", why)
            return
        self._why, self._error = why, error
        logger.info("Stopping tunnel: %s", why)
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

        host = self._host = await self._connect(resolved.connection)
        stack.push_async_callback(
            self._cleanup, "close the login connection", host.close
        )
        self._start_task(stack, self._watch_connection(host), name="connection watcher")

        remote_state = await host.remote_state()
        credentials = self._ssh_host.credentials
        if self._auto_provision:
            await provision_worker_infrastructure(host, remote_state, credentials)
        else:
            await validate_worker_infrastructure(host, remote_state, credentials)
        authorized_key = await asyncio.to_thread(credentials.authorized_key)
        scheduler = create_scheduler(resolved, host)
        if self._profile_name is None:
            logger.info(
                "Using anonymous configuration with %s scheduler.",
                resolved.scheduler.type,
            )
        else:
            logger.info(
                "Using profile %r with %s scheduler.",
                self._profile_name,
                resolved.scheduler.type,
            )
        logger.debug(
            "Worker request: queue=%r cores=%d gpus=%d exclusive=%s "
            "time_limit=%s memory_bytes=%s ports=%s heartbeat_interval=%g "
            "heartbeat_timeout=%g",
            resources.queue,
            resources.cores,
            resources.gpus,
            resources.exclusive,
            resources.time_limit_delta,
            resources.memory_bytes,
            self._worker_ports,
            timings.worker_heartbeat_interval_seconds,
            timings.worker_heartbeat_timeout_seconds,
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

        # An LSF batch job reads the worker's script from `bsub`'s stdin.
        stream_script = (
            resolved.scheduler.type is SchedulerType.LSF
            and resolved.scheduler.submission_mode is SubmissionMode.BATCH
        )
        logs_dir = remote_state.worker_logs_dir()
        spec = JobSpec(
            command=sshd_command(
                remote_state,
                ports=self._worker_ports,
                heartbeat_token=token,
                heartbeat_timeout_seconds=timings.worker_heartbeat_timeout_seconds,
                heartbeat_debug=logger.isEnabledFor(logging.DEBUG),
                control_dir=control.directory,
                read_script_from_stdin=stream_script,
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
            time_limit=resources.time_limit_delta,
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
                _forward_output(interactive_job),
                name="interactive job output",
                stop=interactive_job.close,
            )
            cancel_on_cleanup(job_id)
        else:
            submission = (
                scheduler.submit_script(spec, batch_script(spec.command, WORKER_SCRIPT))
                if stream_script
                else scheduler.submit(spec)
            )
            job_id = await self._finish(
                "submit the worker job", submission, then=cancel_on_cleanup
            )
        logger.info("Submitted worker job %s.", job_id)
        logger.info(
            "Worker heartbeat enabled for job %s: interval=%g seconds, "
            "timeout=%g seconds.",
            job_id,
            timings.worker_heartbeat_interval_seconds,
            timings.worker_heartbeat_timeout_seconds,
        )
        logger.info("Worker heartbeat log: %s.", logs_dir / f"heartbeat-{job_id}.log")
        logger.info("Worker logs: %s and %s.", spec.stdout_path, spec.stderr_path)
        if interactive_job is not None:
            logger.debug(
                "Submission command: %s", shlex.join(interactive_job.submission_command)
            )
            await interactive_job.start_command()

        worker_host = await self._wait_for_running_job(scheduler, job_id)
        # From allocation, one budget for the worker's sshd to start and answer.
        deadline = (
            asyncio.get_running_loop().time() + timings.worker_startup_timeout_seconds
        )
        worker_port = await self._wait_for_ready_port(control, deadline)
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
        logger.info("Waiting for worker SSH endpoint %s:%d.", *destination)
        await self._wait_for_worker_endpoint(destination, deadline)

        # Serve, and tell OpenSSH how to reach the tunnel.
        server = await self._enter(
            stack, TunnelServer(host, destination), "close the tunnel server"
        )
        descriptor = TunnelDescriptor(
            self._ssh_host.alias,
            port=server.port,
            authkey=server.authkey,
            debug=logger.isEnabledFor(logging.DEBUG),
        )
        # Each removal is registered before its file is written: it removes only
        # this tunnel's file, if there is one.
        stack.push_async_callback(
            self._cleanup,
            "remove the tunnel descriptor",
            lambda: asyncio.to_thread(descriptor.remove),
        )
        await self._finish(
            "publish the tunnel descriptor", asyncio.to_thread(descriptor.publish)
        )
        # Withdrawn before the descriptor is removed, so that a Host block never
        # outlives the tunnel it points at.
        stack.push_async_callback(
            self._cleanup,
            "remove the SSH configuration",
            lambda: asyncio.to_thread(
                withdraw_host, self._ssh_host, instance_id=descriptor.instance_id
            ),
        )
        await self._finish(
            "publish the SSH configuration",
            asyncio.to_thread(
                publish_host,
                self._ssh_host,
                instance_id=descriptor.instance_id,
                in_profiles_config=self._ssh_host_in_profiles_config,
            ),
        )
        self._start_task(
            stack, self._monitor_job(scheduler, job_id), name="job monitor"
        )
        self.ready = True
        logger.info(
            "Tunnel ready for %s via worker %s:%d (press Ctrl+C to stop).",
            self._ssh_host.alias,
            *destination,
        )
        logger.info(
            "Connect with `ssh %s` or select `%s` in your editor's remote development interface.",
            self._ssh_host.alias,
            self._ssh_host.alias,
        )

    async def _wait_for_running_job(self, scheduler: Scheduler, job_id: str) -> str:
        """Poll the scheduler until the job runs, and return its host."""
        timings = self._resolved.timings
        previous_state: tuple[JobState, str] | None = None
        try:
            async with asyncio.timeout(timings.queue_timeout_seconds):
                while True:
                    info = await scheduler.inspect(job_id)
                    if (info.state, info.raw_state) != previous_state:
                        previous_state = (info.state, info.raw_state)
                        logger.info(
                            "Worker job %s: %s (%s).",
                            job_id,
                            info.state.value,
                            info.raw_state,
                        )
                    if info.state is JobState.RUNNING and info.host is not None:
                        return info.host
                    if info.state.is_terminal:
                        self._job_finished = True
                        raise TunnelError(f"worker job {job_id} ended{_ending(info)}")
                    if info.state is JobState.UNKNOWN:
                        raise TunnelError(
                            f"worker job {job_id} entered unknown scheduler state "
                            f"{info.raw_state}"
                        )
                    await asyncio.sleep(timings.job_poll_interval_seconds)
        except TimeoutError:
            raise TunnelError(
                f"worker job {job_id} did not start within "
                f"{timings.queue_timeout_seconds:g} seconds"
            ) from None

    async def _wait_for_ready_port(
        self, control: WorkerControl, deadline: float
    ) -> int:
        """
        Wait until `deadline` (loop time) for the worker to say which port its sshd
        listens on.
        """
        try:
            async with asyncio.timeout_at(deadline):
                while True:
                    if (port := await control.read_ready_port()) is not None:
                        return port
                    if failure := await control.read_failure():
                        raise TunnelError(
                            f"worker SSH daemon failed to start: {failure}"
                        )
                    await asyncio.sleep(_READY_POLL_SECONDS)
        except TimeoutError:
            timeout = self._resolved.timings.worker_startup_timeout_seconds
            raise TunnelError(
                "worker SSH daemon did not bind a candidate port within "
                f"{timeout:g} seconds"
            ) from None

    async def _wait_for_worker_endpoint(
        self, destination: tuple[str, int], deadline: float
    ) -> None:
        """
        Wait until `deadline` (loop time) for the worker's sshd to send its banner
        through the login connection.
        """
        timeout = self._resolved.timings.worker_startup_timeout_seconds
        poll_interval = self._resolved.timings.job_poll_interval_seconds
        last_error: Exception | None = None
        try:
            async with asyncio.timeout_at(deadline):
                while True:
                    try:
                        async with asyncio.timeout(_BANNER_READ_SECONDS):
                            banner = await self._read_banner(destination)
                    except TimeoutError:
                        last_error = TunnelError(
                            f"no SSH banner within {_BANNER_READ_SECONDS:g} seconds"
                        )
                    except (asyncssh.Error, OSError) as error:
                        last_error = error
                        logger.debug(
                            "Worker readiness attempt failed: destination=%s:%d "
                            "error=%r",
                            *destination,
                            error,
                        )
                    else:
                        if banner.startswith(b"SSH-"):
                            return
                        last_error = TunnelError(f"unexpected worker banner {banner!r}")
                        logger.debug(
                            "Worker readiness returned an unexpected banner: "
                            "destination=%s:%d banner=%r",
                            *destination,
                            banner,
                        )
                    await asyncio.sleep(poll_interval)
        except TimeoutError:
            detail = f": {last_error}" if last_error is not None else ""
            raise TunnelError(
                f"worker SSH endpoint {destination[0]}:{destination[1]} did not "
                f"become ready within {timeout:g} seconds{detail}"
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
            "Worker heartbeat sender started: job=%s interval_seconds=%g",
            job_id,
            interval_seconds,
        )
        try:
            while True:
                started = time.monotonic()
                try:
                    sequence = await control.publish_lease()
                except (asyncssh.Error, OSError) as error:
                    raise TunnelError(
                        f"worker heartbeat write failed for job {job_id} at "
                        f"sequence {sequence}: {error}"
                    ) from error
                logger.debug(
                    "Worker heartbeat published: job=%s sequence=%d "
                    "elapsed_seconds=%.3f",
                    job_id,
                    sequence,
                    time.monotonic() - started,
                )
                try:
                    async with asyncio.timeout(interval_seconds):
                        await stopping.wait()
                except TimeoutError:
                    continue
                return
        finally:
            logger.debug(
                "Worker heartbeat sender stopped: job=%s last_sequence=%d",
                job_id,
                sequence,
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
                    f"could not monitor worker job {job_id}: {error}"
                ) from error
            logger.debug(
                "Job monitor poll: job=%s poll=%d state=%s raw_state=%s host=%s "
                "exit_code=%s",
                job_id,
                polls,
                info.state.name,
                info.raw_state,
                info.host,
                info.exit_code,
            )
            if info.state.is_terminal:
                self._job_finished = True
                self._stop(None, f"worker job {job_id} ended{_ending(info)}")
                return
            if info.state is JobState.UNKNOWN:
                raise TunnelError(
                    f"worker job {job_id} entered unknown scheduler state "
                    f"{info.raw_state}"
                )

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
            logger.debug("Waiting to %s before stopping.", description)
            done, _ = await asyncio.wait([task], timeout=_CLEANUP_SECONDS)
            if not done:
                task.cancel()
                logger.warning(
                    "Gave up waiting to %s after %g seconds; it may have left "
                    "something behind.",
                    description,
                    _CLEANUP_SECONDS,
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
        logger.info("Cancelled worker job %s.", job_id)

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
        self, description: str, step: Callable[[], Awaitable[object]]
    ) -> None:
        """Run one cleanup step, with a timeout; if it fails, warn and carry on."""
        try:
            async with asyncio.timeout(_CLEANUP_SECONDS):
                await step()
        except TimeoutError:
            logger.warning(
                "Could not %s: no answer within %g seconds.",
                description,
                _CLEANUP_SECONDS,
            )
        except Exception as error:
            logger.warning(
                "Could not %s: %s",
                description,
                error,
                exc_info=logger.isEnabledFor(logging.DEBUG),
            )


async def _forward_output(job: InteractiveJob) -> None:
    """Show the interactive job's output on stderr."""
    # Incremental, so that a character split across chunks isn't garbled.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def write(output: bytes, *, final: bool = False) -> None:
        if text := decoder.decode(output, final):
            sys.stderr.write(text)
            sys.stderr.flush()

    try:
        await job.drain(write)
    except (asyncssh.Error, OSError) as error:
        logger.debug("Interactive job output ended: error=%r", error)
    write(b"", final=True)


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
        logger.warning("The %s did not stop.", task.get_name())


def _connection_lost(error: Exception) -> TunnelError:
    return TunnelError(f"login-node SSH connection lost: {error}")


def _ending(info: JobInfo) -> str:
    """How a job ended, as far as the scheduler still says (PBS may not)."""
    state = f" in scheduler state {info.raw_state}" if info.raw_state else ""
    exit_code = (
        f" with exit code {info.exit_code}" if info.exit_code is not None else ""
    )
    return state + exit_code
