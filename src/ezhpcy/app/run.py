"""
One run of an app on a host (`run_app`):
provisioned there, started as a detached process (`ezhpcy.process`) through
EzHPCy's own Pixi, checked for readiness through the host's connection, and
forwarded to a loopback port here. It needs only the host's connection: no
job, control directory or scheduler.

The process runs, in the app's workdir,

    env PIXI_HOME=… PIXI_CACHE_DIR=… <EzHPCy's pixi> run --as-is -x \
        --manifest-path <environment>/pixi.toml -e <name> -- <run.command>

`-x` runs the command as an executable, so that a task of the user's
`pixi.toml` named like it can't run instead, and passes its arguments on as they
are, unquoted; `--as-is` never installs or writes.
Not through a login shell: the host's profile, and so its modules, aren't read,
as for any process. A manifest may run `bash -lc …` itself.

The app's values are rendered in its launch script, written over SFTP: the
token, in its environment only; the run directory, which only the host knows
before the start; and for `tcp`, the port the launch script picks.

An app with actions alone has no process and no port: it is ready once its
environment is, and its actions run until it is stopped.

An app is one attachment of its host, whose parts are its process and, once it
is ready, its forwarded port. Stopping it, or closing the host's connection,
stops its actions (`ezhpcy.app.action`) and closes the port, then stops the
process and removes its run directory.
"""

from dataclasses import dataclass
from enum import Enum, auto
lazy import asyncio
lazy import secrets
lazy from collections.abc import Mapping
lazy from functools import partial
lazy from pathlib import PurePosixPath

lazy import asyncssh

from ezhpcy.logging import REPLAY_ON_ERROR, get_logger
from ezhpcy.ssh_errors import translating_ssh_errors
lazy from ezhpcy import forward as _forward, process as _process
lazy from ezhpcy.app import AppFailed
lazy from ezhpcy.app.action import ActionRun, AppActions, path_problem
lazy from ezhpcy.app.manifest import (
    ENVIRONMENT_VARIABLES,
    Action,
    App,
    Command,
    Link,
    Ready,
    render,
    render_command,
)
lazy from ezhpcy.app.provision import (
    AppsLayout,
    ProvisionedApp,
    mark_used,
    provision_app,
    validate_app,
)
lazy from ezhpcy.attachments import Attachment
lazy from ezhpcy.forward import Forward
lazy from ezhpcy.process import Process
lazy from ezhpcy.ssh import (
    CommandFailed,
    Destination,
    RemoteHost,
    SocketDestination,
    TCPDestination,
    ensure_directory,
)
lazy from ezhpcy.ssh_errors import SSHError
lazy from ezhpcy.types import RemoteState

logger = get_logger(__name__)
_app_output_logger = get_logger(f"{__name__}.app_output")

# In the run directory, for `socket`: short, as a socket's path may have at
# most 107 bytes.
SOCKET_NAME = "app.sock"
# How long one readiness check may take.
_ATTEMPT_SECONDS = 10.0
# How often to check the app until it is ready.
_READY_POLL_SECONDS = 1.0
# How many lines of the app's stderr to show when it fails to start.
_STDERR_LINES = 20
_STATUS_LINE_LIMIT = 1024


class AppState(Enum):
    """Where an app run is."""

    PROVISIONING = auto()
    # Started, until its readiness check passes.
    STARTING = auto()
    READY = auto()
    EXITED = auto()


@dataclass(frozen=True, slots=True)
class AppStatus:
    """An app run on a host (`RunningApp`), served on this machine at `url`."""

    name: str
    version: str
    state: AppState = AppState.PROVISIONING
    # Its process's, once started; one of its own, without a server.
    run_id: str | None = None
    url: str | None = None
    # Shown under its URL (`access.status_hints`), once it has one.
    status_hints: tuple[str, ...] = ()
    # Connections open through its forwarded port.
    connections: int = 0
    # Once it has exited: `128+n` for a death by signal n.
    exit_status: int | None = None
    # Each action's latest run, of those that have run.
    actions: tuple[ActionRun, ...] = ()


@dataclass(frozen=True, slots=True)
class LinkStatus:
    """
    A link (`Link`), at `url` for the Host alias it was given. It runs nothing,
    so nothing goes through it to count.
    """

    name: str
    version: str
    url: str
    # Shown under its URL (`access.status_hints`).
    status_hints: tuple[str, ...] = ()


class RunningApp:
    """
    An app from its host (`Host.run_app`): its `process` there, the `forward` of a
    loopback port here to it, at `url`, and its `actions`. Attached to its host
    until stopped; its exit ends only this handle, never its host or job. An app
    with actions alone has no `process`, `forward` or `url`.
    """

    def __init__(
        self,
        app: App,
        url: str | None,
        status_hints: tuple[str, ...],
        process: Process | None,
        forward: Forward | None,
        attachment: Attachment,
        actions: AppActions,
        stopped: asyncio.Future[None] | None = None,
    ) -> None:
        # Use `Host.run_app`.
        self.app = app
        self.url = url
        self.status_hints = status_hints
        self.process = process
        self.forward = forward
        self._attachment = attachment
        self._actions = actions
        # Resolved when it is stopped, if it has no process to wait for.
        self._stopped = stopped

    @property
    def actions(self) -> Mapping[str, Action]:
        """
        Its actions, by name, with everything a frontend needs to offer them:
        what each does, what runs it, and its inputs, whose `path` defaults are
        rendered for this run.
        """
        return self._actions.actions

    @translating_ssh_errors
    async def run_action(self, name: str, /, **inputs: object) -> ActionRun:
        """
        Run the action `name` on the app's host, with `inputs` as its inputs'
        values and their defaults for the rest, and return how it ended once it
        has: an exit status other than 0 is a result, `ActionState.FAILED`. See
        `ActionRun`; `status` has each action's latest.

        A value is as its type says: `Parameter.parse` makes one of what a user
        typed. A `path` is on the host, and its `kind` and `must_exist` are
        checked there, over SFTP.

        Raises `ActionError` if the action can't run as asked: there is no such
        action or it runs only by its timer, it is running already, the app
        isn't, or a value is wrong. Raises `SSHError` if the connection fails.
        Cancelled, it stops the run's process and removes its run directory.
        """
        return await self._actions.run(name, inputs)

    @property
    def local_port(self) -> int | None:
        """The loopback port here that `url` is on, if it has one."""
        return self.forward.port if self.forward is not None else None

    @property
    def status(self) -> AppStatus:
        """A snapshot of the run, replaced whole at each change."""
        status = self._attachment.status
        assert isinstance(status, AppStatus)
        return status

    async def wait(self) -> int:
        """
        Wait until the app exits, and return its exit status. Raises `SSHError`
        or `RuntimeError` if the host's connection is lost first. Cancelled, only
        the waiting stops. An app with actions alone exits only by being stopped,
        with status 0.
        """
        if self.process is not None:
            return await self.process.wait()
        assert self._stopped is not None
        await asyncio.shield(self._stopped)
        return 0

    async def stop(self) -> None:
        """
        Close its forwarded port, then stop its process (`Process.stop`) and
        remove its run directory. Once only; cancelled, it still stops it.
        """
        await self._attachment.close()

    async def stderr(self, lines: int = 20) -> str:
        """The last `lines` lines of its stderr, until it is stopped, if it has one."""
        return await self.process.stderr(lines) if self.process is not None else ""

    async def log_stderr(self) -> str:
        """
        The last lines of its stderr, logged at DEBUG for a handler to show with
        the next error; empty if they can't be read, as it raises nothing for that.
        """
        return await log_stderr(self.process) if self.process is not None else ""


async def run_app(
    host: RemoteHost,
    app: App | Link,
    *,
    local_port: int | None = None,
    provision: bool = True,
    apps_root: PurePosixPath | None = None,
    arguments: Mapping[str, object] | None = None,
) -> RunningApp:
    """
    Run `app` on `host` (see the module), and return once it is ready and
    forwarded to `local_port` here (by default its `access.local_port`), if it
    has a server. `arguments` are the values of its arguments.

    With `provision`, its environment and assets (and EzHPCy's Pixi) are
    installed on the host first if they aren't; without, they are only
    checked. They live under `apps_root`, by default `ezhpcy-apps` in the
    host's XDG cache directory.

    Raises `ManifestError` for an argument it can't take, before anything else;
    `ProvisioningError`; and `AppFailed` if the app exits before it is ready, or
    isn't within `ready.timeout`, or a path it was given isn't there, or a template
    can't be rendered, after stopping it. Raises `TypeError` for a link, which has
    nothing to run.
    """
    if isinstance(app, Link):
        raise TypeError(f"{app} is a link, which has nothing to run")
    checked = app.checked_arguments(arguments or {})
    # Before the app, so that it is closed after it.
    _process.watcher(host)
    attachment = host.attachments.attach()
    attachment.report(AppStatus(name=app.name, version=app.version))
    try:
        return await _run(
            host, app, attachment, local_port, provision, apps_root, checked
        )
    except BaseException as error:
        await attachment.close()
        if isinstance(error, ValueError):
            raise AppFailed(t"app {app!s:name}: {error}") from error
        raise


async def _run(
    host: RemoteHost,
    app: App,
    attachment: Attachment,
    local_port: int | None,
    provision: bool,
    apps_root: PurePosixPath | None,
    arguments: Mapping[str, object],
) -> RunningApp:
    remote_state = await host.remote_state()
    layout = AppsLayout(apps_root or remote_state.apps_root())
    prepare = provision_app if provision else validate_app
    provisioned = await prepare(host, remote_state, layout, app)
    workdir = layout.workdir(app)
    async with host.sftp() as sftp:
        await ensure_directory(sftp, workdir)
        await mark_used(sftp, provisioned)
    arguments = await _arguments_on_host(host, app, arguments, provisioned, workdir)
    if app.endpoint is None:
        return _without_server(
            host, app, attachment, remote_state, layout, provisioned, workdir, arguments
        )
    logger.debug(
        t"App run: app={app!s:name} environment={provisioned.environment:path} "
        t"assets={provisioned.assets!s:path} workdir={workdir:path} "
        t"transport={app.endpoint.transport:detail}"
    )
    await _note_other_runs(host, app)
    _warn_if_reachable(app)

    state, url, status_hints = AppState.STARTING, None, ()
    process: Process | None = None
    forward: Forward | None = None
    actions: AppActions | None = None

    def changed() -> None:
        """Report the app's status, from its parts' and its own state."""
        if process is None:
            return
        exit_status = process.exit_status
        attachment.report(
            AppStatus(
                name=app.name,
                version=app.version,
                state=AppState.EXITED if exit_status is not None else state,
                run_id=process.run_id,
                url=url,
                status_hints=status_hints,
                connections=forward.connections if forward is not None else 0,
                exit_status=exit_status,
                actions=actions.latest if actions is not None else (),
            )
        )

    token = secrets.token_hex(16)
    argv, env = _command(
        app, remote_state, layout, provisioned, workdir, token, arguments
    )
    process = await _process.start(
        host,
        argv,
        env=env,
        cwd=workdir,
        attachment=attachment.attach(on_change=changed),
        pick_port=app.endpoint.transport == "tcp",
        record={
            "app": app.normalised_name,
            "version": app.version,
            "transport": app.endpoint.transport,
        },
    )
    changed()
    if process.port is not None:
        destination: Destination = TCPDestination("127.0.0.1", process.port)
    else:
        destination = SocketDestination(process.run_dir / SOCKET_NAME)
    try:
        await _wait_until_ready(host, destination, app, process)
    except AppFailed as failed:
        failed.stderr = await log_stderr(process)
        raise
    forward = await _forward.forward(
        host,
        destination,
        local_port=app.access.local_port if local_port is None else local_port,
        attachment=attachment.attach(on_change=changed),
    )
    state = AppState.READY
    values = {"token": token, "local_port": forward.port, **arguments}
    url = render(app.access.url, values)
    status_hints = tuple(render(hint, values) for hint in app.access.status_hints)
    actions = AppActions(
        host,
        app,
        process,
        attachment,
        workdir=workdir,
        values=_action_values(process, provisioned, workdir, token, arguments),
        command=partial(_through_pixi, app, remote_state, layout, provisioned),
        on_change=changed,
    )
    actions.start()
    changed()
    logger.info(t"App {app!s:name} is ready at {url:target}.")
    return RunningApp(app, url, status_hints, process, forward, attachment, actions)


def _without_server(
    host: RemoteHost,
    app: App,
    attachment: Attachment,
    remote_state: RemoteState,
    layout: AppsLayout,
    provisioned: ProvisionedApp,
    workdir: PurePosixPath,
    arguments: Mapping[str, object],
) -> RunningApp:
    """An app with actions alone, ready now that its environment is."""
    run_id = secrets.token_hex(8)
    stopped = asyncio.get_running_loop().create_future()
    actions: AppActions | None = None

    def changed() -> None:
        attachment.report(
            AppStatus(
                name=app.name,
                version=app.version,
                state=AppState.READY,
                run_id=run_id,
                actions=actions.latest if actions is not None else (),
            )
        )

    async def stop() -> None:
        stopped.set_result(None)

    attachment.push("say the app has stopped", stop)
    actions = AppActions(
        host,
        app,
        None,
        attachment,
        workdir=workdir,
        values={**_places(provisioned, workdir), **arguments},
        command=partial(_through_pixi, app, remote_state, layout, provisioned),
        on_change=changed,
    )
    actions.start()
    changed()
    logger.info(t"App {app!s:name} is ready, with its actions alone.")
    return RunningApp(app, None, (), None, None, attachment, actions, stopped)


def _places(provisioned: ProvisionedApp, workdir: PurePosixPath) -> dict[str, str]:
    """Where the app's work is on its host, for any field to use."""
    values = {"workdir": str(workdir)}
    if provisioned.assets is not None:
        values["assets"] = str(provisioned.assets)
    return values


async def _arguments_on_host(
    host: RemoteHost,
    app: App,
    arguments: Mapping[str, object],
    provisioned: ProvisionedApp,
    workdir: PurePosixPath,
) -> dict[str, object]:
    """
    `arguments`, with the defaults of `path`s, which only now can be rendered, and
    what they ask of what is on the host checked.
    """
    values = dict(arguments)
    for name, parameter in app.argument.items():
        if name not in values and parameter.default is not None:
            values[name] = render(str(parameter.default), _places(provisioned, workdir))
    problem = await path_problem(host, app.argument, values)
    if problem is not None:
        name, path, why = problem
        raise AppFailed(
            t"argument {name:name} of app {app!s:name}: {path:path} {why} on the host"
        )
    return values


def _command(
    app: App,
    remote_state: RemoteState,
    layout: AppsLayout,
    provisioned: ProvisionedApp,
    workdir: PurePosixPath,
    token: str,
    arguments: Mapping[str, object] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """The app's command, through EzHPCy's Pixi, and its environment."""
    assert app.run is not None and app.endpoint is not None
    values: dict[str, object] = {
        **_places(provisioned, workdir),
        **(arguments or {}),
        "run_dir": _process.RUN_DIR,
        "token": token,
    }
    if app.endpoint.transport == "socket":
        values["socket"] = f"{_process.RUN_DIR}/{SOCKET_NAME}"
    else:
        values["port"] = _process.PORT
    return _through_pixi(app, remote_state, layout, provisioned, app.run, values)


def _action_values(
    process: Process,
    provisioned: ProvisionedApp,
    workdir: PurePosixPath,
    token: str,
    arguments: Mapping[str, object],
) -> dict[str, object]:
    """What an action's placeholders are, of the app it reaches, as it runs."""
    values: dict[str, object] = {
        **_places(provisioned, workdir),
        **arguments,
        "token": token,
    }
    if process.port is not None:
        values["port"] = str(process.port)
    else:
        values["socket"] = str(process.run_dir / SOCKET_NAME)
    return values


def _through_pixi(
    app: App,
    remote_state: RemoteState,
    layout: AppsLayout,
    provisioned: ProvisionedApp,
    command: Command,
    values: Mapping[str, object],
) -> tuple[list[str], dict[str, str]]:
    """`command`, through EzHPCy's Pixi in the app's environment, and its environment."""
    argv = remote_state.pixi_command(
        "run",
        "--as-is",
        "--executable",
        "--manifest-path",
        str(provisioned.pixi_manifest),
        "--environment",
        app.environment.environment,
        "--",
        *render_command(command.command, values),
        cache_dir=layout.pixi_cache,
    )
    # The manifest's own variables after EzHPCy's, which they can't override
    # (`EZHPCY_*` are reserved).
    env = {
        variable: str(values[name])
        for name, variable in ENVIRONMENT_VARIABLES.items()
        if name in values
    }
    env |= {name: render(value, values) for name, value in command.env.items()}
    return list(argv), env


async def _note_other_runs(host: RemoteHost, app: App) -> None:
    """Say so when another run of the app is live on the host."""
    for run_dir, record in (await _process.live_runs(host)).items():
        if record.get("app") == app.normalised_name:
            logger.info(
                t"Another run of app {app.name:name} is live on this host: "
                t"{record.get('version')!s:name} in {run_dir:path}. They share "
                t"its workdir."
            )


def _warn_if_reachable(app: App) -> None:
    """
    Warn about a `tcp` app that doesn't authenticate its clients, on any
    host. Not even an exclusive job's node is counted on to be the user's
    alone.
    """
    assert app.endpoint is not None
    if app.endpoint.authenticates:
        return
    if app.endpoint.transport == "tcp":
        logger.warning(
            t"App {app!s:name} listens on a TCP port and doesn't authenticate its "
            t"clients: other users on its host can reach it. Set "
            t"{'endpoint.authenticates':name} if the app checks its clients."
        )
    logger.debug(
        "The app doesn't authenticate its clients: every local user can reach it "
        "through the forwarded loopback port."
    )


async def _wait_until_ready(
    host: RemoteHost, destination: Destination, app: App, process: Process
) -> None:
    """
    Check the app through the host's connection until it is ready, for at most
    `ready.timeout`, giving up early if its process exits.
    """
    ready = app.ready
    logger.info(
        t"Waiting for app {app!s:name} to be ready {f'(check: {ready.check})':aside}."
    )
    exited = asyncio.ensure_future(process.wait())
    why = "no attempt finished"
    try:
        async with asyncio.timeout(ready.timeout.total_seconds()):
            while True:
                try:
                    async with asyncio.timeout(_ATTEMPT_SECONDS):
                        not_ready = await check_ready(host, destination, ready)
                except (TimeoutError, asyncssh.Error, OSError) as error:
                    not_ready = repr(error)
                if not_ready is None:
                    return
                why = not_ready
                logger.debug(t"App not ready yet: {why}")
                await asyncio.wait([exited], timeout=_READY_POLL_SECONDS)
                if exited.done():
                    raise AppFailed(
                        t"app {app!s:name} exited with status "
                        t"{exited.result():detail} before it was ready"
                    )
    except TimeoutError:
        raise AppFailed(
            t"app {app!s:name} did not become ready within "
            t"{ready.timeout.total_seconds():detail:g} seconds: {why}"
        ) from None
    finally:
        exited.cancel()


async def log_stderr(process: Process) -> str:
    """
    The last lines of the app's stderr, logged to be shown with the next error
    (`REPLAY_ON_ERROR`); empty if they can't be read.
    """
    try:
        async with asyncio.timeout(_ATTEMPT_SECONDS):
            output = await process.stderr(_STDERR_LINES)
    except (TimeoutError, CommandFailed, SSHError, OSError) as error:
        logger.debug(t"Could not read the app's stderr: {error!r}")
        return ""
    if lines := output.splitlines():
        _app_output_logger.debug(
            t"Last lines of the app's stderr ({process.run_dir:path}/err):",
            extra=REPLAY_ON_ERROR,
        )
        for line in lines:
            _app_output_logger.debug(f"  {line}", extra=REPLAY_ON_ERROR)
    return output


async def check_ready(
    host: RemoteHost, destination: Destination, ready: Ready
) -> str | None:
    """
    Check the app once, through the host's connection, as `ready` says: `None`
    if it is ready, otherwise why not.
    """
    reader, writer = await host.open(destination)
    try:
        match ready.check:
            case "tcp":
                return None
            case "banner":
                prefix = (ready.prefix or "").encode()
                received = b""
                while len(received) < len(prefix):
                    if not (chunk := await reader.read(len(prefix) - len(received))):
                        break
                    received += chunk
                if received.startswith(prefix):
                    return None
                return f"the app sent {received!r}, not {prefix!r}"
            case "http":
                writer.write(
                    f"GET {ready.path} HTTP/1.0\r\nHost: 127.0.0.1\r\n"
                    "Connection: close\r\n\r\n".encode()
                )
                line = await reader.readline()
                fields = line[:_STATUS_LINE_LIMIT].decode(errors="replace").split()
                if len(fields) < 2 or not fields[0].startswith("HTTP/"):
                    return f"not an HTTP response: {line[:80]!r}"
                if not fields[1].isdigit():
                    return f"not an HTTP status: {fields[1]!r}"
                status = int(fields[1])
                if ready.accepts(status):
                    return None
                return f"GET {ready.path} answered {status}"
    finally:
        writer.close()
