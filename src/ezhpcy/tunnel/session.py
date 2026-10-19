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

import logging
lazy import asyncio
lazy import codecs
lazy import functools
lazy import secrets
lazy import shlex
lazy import sys
lazy import time
lazy from collections.abc import Awaitable, Callable, Coroutine
lazy from contextlib import AbstractAsyncContextManager, AsyncExitStack, ExitStack

lazy import asyncssh

lazy from ezhpcy.logging import flush_logs
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

logger = logging.getLogger(__name__)

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
# How much of an interactive job's output to keep, without debug logging, to show
# if the tunnel fails: all of it for any startup, but bounded for a long tunnel.
_KEPT_OUTPUT_BYTES = 2 * 1024 * 1024


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
    ) -> None:
        self._resolved = resolved
        self._ssh_host = ssh_host
        self._connect = connect
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
        self._why: str | None = None
        self._error: Exception | None = None
        self._cancelled_main_task = False
        # An interactive job's output, once it is submitted.
        self._job_output: _JobOutput | None = None

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
            # After cleanup, which closes the job, so that its last output is in.
            if self._job_output is not None:
                await self._job_output.show_kept()
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
            resources.time_limit,
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

        logs_dir = remote_state.worker_logs_dir()
        spec = JobSpec(
            command=sshd_command(
                remote_state,
                ports=self._worker_ports,
                heartbeat_token=token,
                heartbeat_timeout_seconds=timings.worker_heartbeat_timeout_seconds,
                heartbeat_debug=logger.isEnabledFor(logging.DEBUG),
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
            self._job_output = _JobOutput(interactive_job)
            # Stopped by closing the job, after its last output.
            self._start_task(
                stack,
                self._job_output.forward(),
                name="interactive job output",
                stop=interactive_job.close,
            )
            cancel_on_cleanup(job_id)
        else:
            job_id = await self._finish(
                "submit the worker job", scheduler.submit(spec), then=cancel_on_cleanup
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
        logger.info("Waiting for worker SSH endpoint %s:%d.", *destination)
        await self._wait_for_worker_endpoint(
            destination, control, scheduler, job_id, deadline
        )

        # Serve on a loopback port, and tell OpenSSH how to reach it.
        def accept(client_host: str, client_port: int) -> bool:
            logger.debug(
                "Tunnel client accepted: client=%s:%d destination=%s:%d",
                client_host,
                client_port,
                *destination,
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
        logger.debug("Forwarding port: port=%d destination=%s:%d", port, *destination)
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
                            "Worker job %s: %s (%s)%s.",
                            job_id,
                            info.state.value,
                            info.raw_state,
                            reason,
                        )
                    if info.state is JobState.RUNNING and info.host is not None:
                        return info.host
                    if ended := self._job_ended(job_id, info):
                        raise TunnelError(ended)
                    if loop.time() >= next_progress:
                        next_progress = loop.time() + progress_interval
                        waited = loop.time() - started
                        logger.info(
                            "Worker job %s still %s after %s (queue timeout in %s)%s.",
                            job_id,
                            info.state.value,
                            _duration(waited),
                            _duration(timings.queue_timeout_seconds - waited),
                            reason,
                        )
                    await asyncio.sleep(timings.job_poll_interval_seconds)
        except TimeoutError:
            raise TunnelError(
                f"worker job {job_id} did not start within "
                f"{timings.queue_timeout_seconds:g} seconds"
            ) from None

    def _job_ended(self, job_id: str, info: JobInfo) -> str | None:
        """
        If the scheduler says the job has ended, note that it needn't be cancelled,
        and return how it ended, to stop the tunnel with. Raises for a state the
        tunnel can't act on.
        """
        if info.state is JobState.UNKNOWN:
            raise TunnelError(
                f"worker job {job_id} entered unknown scheduler state {info.raw_state}"
            )
        if not info.state.is_terminal:
            return None
        self._job_finished = True
        return f"worker job {job_id} ended{_ending(info)}"

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
                "worker SSH daemon did not bind a candidate port within "
                f"{timeout:g} seconds"
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
                    if error := _worker_gone(await control.read_status()):
                        raise error
                    await self._check_job_lives(scheduler, job_id)
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
                "Could not %s: no answer within %g seconds.", description, seconds
            )
        except Exception as error:
            logger.warning(
                "Could not %s: %s",
                description,
                error,
                exc_info=logger.isEnabledFor(logging.DEBUG),
            )


class _JobOutput:
    """
    An interactive job's output, which echoes the long worker script.

    With debug logging, `forward` shows it on stderr as it arrives, after the
    records logged before it. Otherwise it keeps only the last
    `_KEPT_OUTPUT_BYTES`, for `show_kept` to show if the tunnel fails. Either way
    it reads all of it: unread output would stall the job once SSH's window fills.

    Written from a thread, as logging is (`ezhpcy.logging`), so that a terminal
    that doesn't take output holds up only the job's output.
    """

    def __init__(self, job: InteractiveJob) -> None:
        self._job = job
        self._live = logger.isEnabledFor(logging.DEBUG)
        self._kept = bytearray()
        # Whether the start of the output was dropped to keep within the limit.
        self._cut = False

    async def forward(self) -> None:
        # Incremental, so that a character split across chunks isn't garbled.
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        async def write(output: bytes, *, final: bool = False) -> None:
            if text := decoder.decode(output, final):
                await asyncio.to_thread(_write_stderr, text)

        async def keep(output: bytes) -> None:
            self._kept += output
            if len(self._kept) > _KEPT_OUTPUT_BYTES:
                del self._kept[:-_KEPT_OUTPUT_BYTES]
                self._cut = True

        try:
            await self._job.drain(write if self._live else keep)
        except (asyncssh.Error, OSError) as error:
            logger.debug("Interactive job output ended: error=%r", error)
        if self._live:
            await write(b"", final=True)

    async def show_kept(self) -> None:
        """Show the end of the output that `forward` kept, if any."""
        if not self._kept:
            return
        logger.info(
            "%s of worker job %s:",
            "End of the output" if self._cut else "Output",
            self._job.job_id,
        )
        # Cut at a byte count, so its first character may be a replacement.
        text = self._kept.decode(errors="replace")
        await asyncio.to_thread(
            _write_stderr, text if text.endswith("\n") else f"{text}\n"
        )


def _write_stderr(text: str) -> None:
    flush_logs()
    sys.stderr.write(text)
    sys.stderr.flush()


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


def _worker_gone(status: WorkerStatus | None) -> TunnelError | None:
    """The error to stop with if `status` says sshd won't serve, else None."""
    match status:
        case WorkerFailed(reason):
            return TunnelError(f"worker SSH daemon failed to start: {reason}")
        case WorkerStopped(exit_status):
            return TunnelError(f"worker SSH daemon exited with status {exit_status}")
        case _:
            return None


def _connection_lost(error: Exception) -> TunnelError:
    return TunnelError(f"login-node SSH connection lost: {error}")


def _duration(seconds: float) -> str:
    """`seconds` as `2 min 30 s`, or `45 s` under a minute."""
    minutes, seconds = divmod(round(seconds), 60)
    return f"{minutes} min {seconds} s" if minutes else f"{seconds} s"


def _ending(info: JobInfo) -> str:
    """How a job ended, as far as the scheduler still says (PBS may not)."""
    state = f" in scheduler state {info.raw_state}" if info.raw_state else ""
    exit_code = (
        f" with exit code {info.exit_code}" if info.exit_code is not None else ""
    )
    return state + exit_code
