"""
An app's actions, while it runs (`RunningApp.run_action`). Each run is a process
on the app's host (`ezhpcy.process`), with a run directory of its own, through the
app's environment and in its workdir, with the app's values and its inputs'. The
result (`ActionRun`) is read from its output before that directory is removed.

Every value is checked here, at one choke point for all frontends, before anything
runs. One run of an action runs at a time, so that a slow one can't pile up.

Timers run here, in the client, and stop with the app: the first run is as the
app becomes ready, the next after one interval, a tick is skipped while the last
run continues, what a run wrote is logged, and a failure is logged and the timer
continues.

Stopping the app first stops its timers and its actions' processes. A process
isn't a part of the app's attachment: those stay until it is closed, and a timer
would add one for each tick.
"""

import time
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum, auto
lazy import asyncio
lazy from collections.abc import Awaitable, Callable, Mapping
lazy from dataclasses import replace
lazy from pathlib import PurePosixPath

lazy import asyncssh

from ezhpcy.logging import get_logger
lazy from ezhpcy import process as _process
lazy from ezhpcy.app import ActionError
lazy from ezhpcy.app.manifest import Action, App, Command, Parameter, PathParameter
lazy from ezhpcy.attachments import Attachment
lazy from ezhpcy.messages import MessageError
lazy from ezhpcy.process import Process
lazy from ezhpcy.ssh import CommandFailed, RemoteHost
lazy from ezhpcy.ssh_errors import SSHError

logger = get_logger(__name__)

# How much of an action's output its result holds: the last lines.
_OUTPUT_LINES = 200
# How long reading one of its outputs may take.
_READ_SECONDS = 10.0


class ActionState(Enum):
    """Where a run of an action is."""

    RUNNING = auto()
    SUCCEEDED = auto()
    # It exited with another status than 0, or couldn't run to its end.
    FAILED = auto()


@dataclass(frozen=True, slots=True)
class ActionRun:
    """
    One run of an app's action (`RunningApp.run_action`). Times are
    `time.monotonic()` readings. `stdout` and `stderr` are the last lines, as
    plain text, once it has finished; `stderr` only if it failed, where it may say
    why it couldn't run instead.
    """

    action: str
    state: ActionState = ActionState.RUNNING
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    # Once it has exited: `128+n` for a death by signal n.
    exit_status: int | None = None
    stdout: str = ""
    stderr: str = ""


class AppActions:
    """
    The actions of one run of an app. `actions` holds them as `values`, the app's
    values, render them; `command` gives the argv and environment for a command
    with some values.
    """

    def __init__(
        self,
        host: RemoteHost,
        app: App,
        process: Process | None,
        attachment: Attachment,
        *,
        workdir: PurePosixPath,
        values: Mapping[str, object],
        command: Callable[
            [Command, Mapping[str, object]], tuple[list[str], dict[str, str]]
        ],
        on_change: Callable[[], object],
    ) -> None:
        self.actions = {
            name: action.rendered(values) for name, action in app.action.items()
        }
        self._host = host
        self._app = app
        self._process = process
        self._attachment = attachment
        self._workdir = workdir
        self._values = values
        self._command = command
        self._on_change = on_change
        self._latest: dict[str, ActionRun] = {}
        # What is running, by action, resolved when it has finished.
        self._runs: dict[str, asyncio.Future[None]] = {}
        # The attachments of the processes that have started.
        self._parts: set[Attachment] = set()
        self._timers: list[asyncio.Task[None]] = []
        self._timed_runs: set[asyncio.Task[None]] = set()

    @property
    def latest(self) -> tuple[ActionRun, ...]:
        """Each action's latest run, in the manifest's order, if it has had one."""
        return tuple(
            self._latest[name] for name in self.actions if name in self._latest
        )

    def start(self) -> None:
        """Start the timers; closing the attachment stops them, and all runs."""
        self._attachment.push("stop the app's actions", self._stop)
        for name, action in self.actions.items():
            if isinstance(every := action.trigger.every, timedelta):
                self._timers.append(
                    asyncio.create_task(self._timer(name, action, every))
                )

    @property
    def _app_exited(self) -> bool:
        """Whether the app's server exited, if it has one."""
        return self._process is not None and self._process.exit_status is not None

    async def run(self, name: str, inputs: Mapping[str, object]) -> ActionRun:
        """As `RunningApp.run_action`."""
        action = self.actions.get(name)
        if action is None:
            raise ActionError(
                t"app {self._app!s:name} has no action {name:value}; choose from: "
                t"{sorted(self.actions):choice}"
            )
        if not action.trigger.manual:
            raise ActionError(
                t"action {name:name} of app {self._app!s:name} runs only by its timer"
            )
        return await self._run(name, action, inputs)

    async def _run(
        self, name: str, action: Action, inputs: Mapping[str, object]
    ) -> ActionRun:
        values = self._checked(name, action, inputs)
        try:
            command = self._command(action.run, {**self._values, **values})
        except ValueError as error:
            raise ActionError(t"action {name:name}: {error}") from error
        if self._attachment.closed or self._app_exited:
            raise ActionError(
                t"action {name:name} needs app {self._app!s:name} running, and it isn't"
            )
        if name in self._runs:
            raise ActionError(t"action {name:name} is running already")
        finished = self._runs[name] = asyncio.get_running_loop().create_future()
        try:
            await self._check_on_host(name, action, values)
            return await self._execute(name, command)
        finally:
            del self._runs[name]
            finished.set_result(None)

    def _checked(
        self, name: str, action: Action, inputs: Mapping[str, object]
    ) -> dict[str, object]:
        """Every input's value for its placeholder, with the defaults."""
        for unknown in inputs.keys() - action.input.keys():
            raise ActionError(
                t"action {name:name} has no input {unknown:value}; choose from: "
                t"{sorted(action.input):choice}",
                input_name=unknown,
            )
        values: dict[str, object] = {}
        for input_name, parameter in action.input.items():
            value = inputs.get(input_name, parameter.default)
            if value is None:
                if not parameter.required:
                    continue
                raise ActionError(
                    t"action {name:name} needs a value for input {input_name:name}",
                    input_name=input_name,
                )
            try:
                values[input_name] = parameter.check(value)
            except ValueError as error:
                raise ActionError(
                    t"input {input_name:name} of action {name:name}: {error}",
                    input_name=input_name,
                ) from error
        return values

    async def _check_on_host(
        self, name: str, action: Action, values: Mapping[str, object]
    ) -> None:
        problem = await path_problem(self._host, action.input, values)
        if problem is not None:
            input_name, path, why = problem
            raise ActionError(
                t"input {input_name:name} of action {name:name}: "
                t"{path:path} {why} on the host",
                input_name=input_name,
            )

    async def _execute(
        self, name: str, command: tuple[list[str], dict[str, str]]
    ) -> ActionRun:
        run = ActionRun(action=name)
        self._report(run)
        part = Attachment(on_change=lambda: None)
        try:
            argv, env = command
            process = await _process.start(
                self._host,
                argv,
                env=env,
                cwd=self._workdir,
                attachment=part,
                # Not `app`: what is listed as a run of the app is the app.
                record={
                    "action": name,
                    "action_of": self._app.normalised_name,
                    "version": self._app.version,
                },
            )
            # Only once started: closing it while starting would lose the process.
            self._parts.add(part)
            if self._attachment.closed:
                await part.close()
            exit_status = await process.wait()
            stdout = await _read(process.stdout)
            stderr = await _read(process.stderr) if exit_status != 0 else ""
        except BaseException as error:
            self._report(
                replace(
                    run,
                    state=ActionState.FAILED,
                    finished=time.monotonic(),
                    stderr=str(error) or type(error).__name__,
                )
            )
            raise
        finally:
            try:
                await part.close()
            finally:
                self._parts.discard(part)
        now = time.monotonic()
        finished = replace(
            run,
            state=ActionState.SUCCEEDED if exit_status == 0 else ActionState.FAILED,
            finished=now,
            exit_status=exit_status,
            stdout=stdout,
            stderr=stderr,
        )
        self._report(finished)
        logger.debug(
            t"Action finished: action={name:detail} exit_status={exit_status:detail} "
            t"seconds={now - run.started:detail:.1f}"
        )
        return finished

    def _report(self, run: ActionRun) -> None:
        self._latest[run.action] = run
        self._on_change()

    async def _timer(self, name: str, action: Action, every: timedelta) -> None:
        interval = every.total_seconds()
        tick = time.monotonic()
        while True:
            if self._app_exited:
                logger.debug(t"Timer of action {name:detail} stops, as its app exited.")
                return
            if name in self._runs:
                logger.debug(t"Timer tick skipped: action={name:detail} is running")
            else:
                task = asyncio.create_task(self._timed_run(name, action))
                self._timed_runs.add(task)
                task.add_done_callback(self._timed_runs.discard)
            tick += interval
            await asyncio.sleep(max(0.0, tick - time.monotonic()))

    async def _timed_run(self, name: str, action: Action) -> None:
        """Run an action for its timer, which continues whatever becomes of it."""
        where = t"Action {name:name} of app {self._app!s:name}"
        try:
            run = await self._run(name, action, {})
        except (MessageError, RuntimeError, OSError, asyncssh.Error) as error:
            if not self._attachment.closed:
                logger.warning(where + t" could not run: {error}. Its timer continues.")
            return
        if run.state is ActionState.SUCCEEDED:
            # Nobody asked for this run, so nobody else would show what it says.
            logger.info(where + t" ran by its timer.")
            for line in run.stdout.splitlines():
                logger.info(t"  {line}")
        # Stopping the app stops its actions, which isn't their failure.
        elif not self._attachment.closed:
            lines = run.stderr.strip().splitlines()
            why = t": {lines[-1]}" if lines else t""
            logger.warning(
                where
                + t" exited with status {run.exit_status!s:detail}"
                + why
                + t". Its timer continues."
            )

    async def _stop(self) -> None:
        """
        Stop the timers, then every run, by hand or not, the same way: not by
        cancelling them, as one still starting stops once it has started.
        """
        for timer in self._timers:
            timer.cancel()
        if self._timers:
            await asyncio.wait(self._timers)
        if parts := list(self._parts):
            await asyncio.gather(*(part.close() for part in parts))
        if runs := list(self._runs.values()):
            await asyncio.wait(runs)
        if timed_runs := list(self._timed_runs):
            await asyncio.wait(timed_runs)


async def path_problem(
    host: RemoteHost, parameters: Mapping[str, Parameter], values: Mapping[str, object]
) -> tuple[str, str, str] | None:
    """
    The first `path` that isn't what its parameter asks of what is on the host, over
    SFTP, if any: its name, its value, and why.
    """
    paths = {
        name: parameter
        for name, parameter in parameters.items()
        if isinstance(parameter, PathParameter)
        and name in values
        and (parameter.kind is not None or parameter.must_exist)
    }
    if not paths:
        return None
    async with host.sftp() as sftp:
        for name, parameter in paths.items():
            path = str(values[name])
            if not await sftp.exists(path):
                if parameter.must_exist:
                    return name, path, "doesn't exist"
            elif parameter.kind == "file" and not await sftp.isfile(path):
                return name, path, "isn't a file"
            elif parameter.kind == "directory" and not await sftp.isdir(path):
                return name, path, "isn't a directory"
    return None


async def _read(reader: Callable[[int], Awaitable[str]]) -> str:
    """The last lines of an output; empty if they can't be read."""
    try:
        async with asyncio.timeout(_READ_SECONDS):
            return await reader(_OUTPUT_LINES)
    except (TimeoutError, CommandFailed, SSHError, OSError) as error:
        logger.debug(t"Could not read an action's output: {error!r}")
        return ""
