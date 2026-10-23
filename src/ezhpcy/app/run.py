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

An app is one attachment of its host, whose parts are its process and, once it
is ready, its forwarded port. Stopping it, or closing the host's connection,
closes the port, then stops the process and removes its run directory.
"""

from dataclasses import dataclass
from enum import Enum, auto
lazy import asyncio
lazy import secrets
lazy from pathlib import PurePosixPath

lazy import asyncssh

from ezhpcy.logging import REPLAY_ON_ERROR, get_logger
lazy from ezhpcy import forward as _forward, process as _process
lazy from ezhpcy.app import AppFailed
lazy from ezhpcy.app.manifest import ENVIRONMENT_VARIABLES, App, Ready, render
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
    # Its process's, once started.
    run_id: str | None = None
    url: str | None = None
    # Connections open through its forwarded port.
    connections: int = 0
    # Once it has exited: `128+n` for a death by signal n.
    exit_status: int | None = None


class RunningApp:
    """
    An app served from its host (`Host.run_app`): its `process` there, and the
    `forward` of a loopback port here to it, at `url`. Attached to its host
    until stopped; its exit ends only this handle, never its host or job.
    """

    def __init__(
        self,
        app: App,
        url: str,
        process: Process,
        forward: Forward,
        attachment: Attachment,
    ) -> None:
        # Use `Host.run_app`.
        self.app = app
        self.url = url
        self.process = process
        self.forward = forward
        self._attachment = attachment

    @property
    def local_port(self) -> int:
        """The loopback port here that `url` is on."""
        return self.forward.port

    @property
    def status(self) -> AppStatus:
        """A snapshot of the run, replaced whole at each change."""
        status = self._attachment.status
        assert isinstance(status, AppStatus)
        return status

    async def wait(self) -> int:
        """Wait until the app exits, and return its exit status (`Process.wait`)."""
        return await self.process.wait()

    async def stop(self) -> None:
        """
        Close its forwarded port, then stop its process (`Process.stop`) and
        remove its run directory. Once only; cancelled, it still stops it.
        """
        await self._attachment.close()

    async def stderr(self, lines: int = 20) -> str:
        """The last `lines` lines of its stderr, until it is stopped."""
        return await self.process.stderr(lines)


async def run_app(
    host: RemoteHost,
    app: App,
    *,
    local_port: int | None = None,
    provision: bool = True,
    apps_root: PurePosixPath | None = None,
) -> RunningApp:
    """
    Run `app` on `host` (see the module), and return once it is ready and
    forwarded to `local_port` here (by default its `access.local_port`).

    With `provision`, its environment and assets (and EzHPCy's Pixi) are
    installed on the host first if they aren't; without, they are only
    checked. They live under `apps_root`, by default `ezhpcy-apps` in the
    host's XDG cache directory.

    Raises `ProvisioningError`, and `AppFailed` if the app exits before it is
    ready, or isn't within `ready.timeout_seconds`, after stopping it.
    """
    # Before the app, so that it is closed after it.
    _process.watcher(host)
    attachment = host.attachments.attach()
    attachment.report(AppStatus(name=app.name, version=app.version))
    try:
        return await _run(host, app, attachment, local_port, provision, apps_root)
    except BaseException:
        await attachment.close()
        raise


async def _run(
    host: RemoteHost,
    app: App,
    attachment: Attachment,
    local_port: int | None,
    provision: bool,
    apps_root: PurePosixPath | None,
) -> RunningApp:
    remote_state = await host.remote_state()
    layout = AppsLayout(apps_root or remote_state.apps_root())
    prepare = provision_app if provision else validate_app
    provisioned = await prepare(host, remote_state, layout, app)
    workdir = layout.workdir(app)
    async with host.sftp() as sftp:
        await ensure_directory(sftp, workdir)
        await mark_used(sftp, provisioned)
    logger.debug(
        t"App run: app={app!s:name} environment={provisioned.environment:path} "
        t"assets={provisioned.assets!s:path} workdir={workdir:path} "
        t"transport={app.endpoint.transport:detail}"
    )
    await _note_other_runs(host, app)
    _warn_if_reachable(app)

    state, url = AppState.STARTING, None
    process: Process | None = None
    forward: Forward | None = None

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
                connections=forward.connections if forward is not None else 0,
                exit_status=exit_status,
            )
        )

    token = secrets.token_hex(16)
    argv, env = _command(app, remote_state, layout, provisioned, workdir, token)
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
        failed.stderr = await _log_stderr(process)
        raise
    forward = await _forward.forward(
        host,
        destination,
        local_port=app.access.local_port if local_port is None else local_port,
        attachment=attachment.attach(on_change=changed),
    )
    state = AppState.READY
    url = render(app.access.url, {"token": token, "local_port": str(forward.port)})
    changed()
    logger.info(t"App {app!s:name} is ready at {url:target}.")
    return RunningApp(app, url, process, forward, attachment)


def _command(
    app: App,
    remote_state: RemoteState,
    layout: AppsLayout,
    provisioned: ProvisionedApp,
    workdir: PurePosixPath,
    token: str,
) -> tuple[list[str], dict[str, str]]:
    """The app's command, through EzHPCy's Pixi, and its environment."""
    values = {"run_dir": _process.RUN_DIR, "workdir": str(workdir), "token": token}
    if app.endpoint.transport == "socket":
        values["socket"] = f"{_process.RUN_DIR}/{SOCKET_NAME}"
    else:
        values["port"] = _process.PORT
    if provisioned.assets is not None:
        values["assets"] = str(provisioned.assets)
    argv = remote_state.pixi_command(
        "run",
        "--as-is",
        "--executable",
        "--manifest-path",
        str(provisioned.pixi_manifest),
        "--environment",
        app.environment.environment,
        "--",
        *(render(argument, values) for argument in app.run.command),
        cache_dir=layout.pixi_cache,
    )
    # The manifest's own variables after EzHPCy's, which they can't override
    # (`EZHPCY_*` are reserved).
    env = {ENVIRONMENT_VARIABLES[name]: value for name, value in values.items()}
    env |= {name: render(value, values) for name, value in app.run.env.items()}
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
    `ready.timeout_seconds`, giving up early if its process exits.
    """
    ready = app.ready
    logger.info(
        t"Waiting for app {app!s:name} to be ready {f'(check: {ready.check})':aside}."
    )
    exited = asyncio.ensure_future(process.wait())
    why = "no attempt finished"
    try:
        async with asyncio.timeout(ready.timeout_seconds):
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
            t"{ready.timeout_seconds:detail:g} seconds: {why}"
        ) from None
    finally:
        exited.cancel()


async def _log_stderr(process: Process) -> str:
    """
    The last lines of the app's stderr, logged to be shown with the error; empty
    if they can't be read.
    """
    try:
        async with asyncio.timeout(_ATTEMPT_SECONDS):
            output = await process.stderr(_STDERR_LINES)
    except (TimeoutError, CommandFailed, asyncssh.Error, OSError) as error:
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
