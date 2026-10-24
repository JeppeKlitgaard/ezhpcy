"""
`ezhpcy tunnel`: a worker job published under an alias, and the apps given
running on its node, until it ends or Ctrl+C, through `ezhpcy.api`.

Before connecting, so that a doomed tunnel fails before any password prompt or
queue wait: the manifests are read and the apps chosen, the configuration is
resolved, the worker's credentials checked, and the alias reserved
(`api.reserve_alias`, which checks that `ssh <alias>` would reach it). Then:
connect, enter a job, publish it under the alias, start the apps on its node in
order (printing a link's URL, for the alias, in its place), and wait. An app
that fails to start, or exits, leaves the tunnel serving.
"""

from contextlib import contextmanager
lazy import asyncio
lazy import os
lazy import sys
lazy from collections.abc import Callable, Generator, Sequence
lazy from dataclasses import replace
lazy from pathlib import Path, PurePosixPath
lazy from string.templatelib import Template
lazy from types import FrameType
lazy from typing import Annotated

lazy import asyncssh
lazy from cyclopts import Parameter, validators
lazy from pydantic import SecretStr

from ezhpcy.cli._options import (
    AliasOpt,
    AppOpt,
    ConnectionOptions,
    LoadBuiltinManifestsOpt,
    ManifestOpt,
    OptionalProfileArg,
    ResourceOptions,
    SchedulerOptions,
    TimingOptions,
)
from ezhpcy.constants import DEFAULT_WORKER_PORTS
from ezhpcy.logging import get_logger
lazy from ezhpcy import api
lazy from ezhpcy.app.manifest import (
    builtin_manifests,
    choose_app,
    collect_apps,
    combined_resources,
)
lazy from ezhpcy.app.run import log_stderr
lazy from ezhpcy.cli import _include as include
lazy from ezhpcy.cli._errors import CliUsageError, sentence
lazy from ezhpcy.cli._resolve import (
    job_from_cli,
    login_from_cli,
    login_usage_error,
    select_profile,
)
lazy from ezhpcy.cli._tunnel_status import StatusPanel
lazy from ezhpcy.cli.utils.interrupts import interrupts_handled_by
lazy from ezhpcy.cli.utils.ssh import cli_prompts
lazy from ezhpcy.config import config
lazy from ezhpcy.console import (
    console,
    flush_output,
    hide_live_display,
    live_display,
)
lazy from ezhpcy.logging import label_log_file
lazy from ezhpcy.messages import MessageError, plain
lazy from ezhpcy.tunnel.ssh_config import WorkerHost
lazy from ezhpcy.types import alias_digest
lazy from ezhpcy.worker.job import select_ports

_DEFAULT_WORKER_PORT_RETRIES = DEFAULT_WORKER_PORTS - 1
# Far fewer than the 16384 dynamic ports `select_ports` draws from.
_MAX_WORKER_PORT_RETRIES = 100
# How long the second Ctrl+C waits for the output to be written before quitting.
_EXIT_FLUSH_SECONDS = 0.5
logger = get_logger(__name__)


def _resolve_alias(
    alias: str | None,
    *,
    profile_name: str | None,
    login: api.Login,
    job: api.JobConfig,
) -> str:
    """Pick the Host alias this tunnel serves; profiles default to their name."""
    if alias is None:
        # Identical anonymous options yield the same alias across restarts, so
        # saved hosts (e.g. in VS Code) keep working.
        alias = profile_name or f"ezhpcy-{alias_digest(login, job)[:12]}"
    if alias != profile_name and alias in config.profile:
        raise CliUsageError(
            t"It is the name of profile {alias:name}; choose another alias.",
            param_hint="--alias",
            value=alias,
        )
    try:
        # Checks the alias, and that the user can be named in its Host block.
        WorkerHost.for_endpoint(alias, user=login.user, host=login.host)
    except ValueError as error:
        raise CliUsageError(
            t"{sentence(str(error))}", param_hint="--alias", value=alias
        ) from error
    return alias


def _chosen_apps(
    manifests: Sequence[Path], names: Sequence[str], *, load_builtins: bool
) -> list[api.App | api.Link]:
    """
    The apps `--app` names, `NAME` or `NAME@VERSION`, of the `--manifest`s, and
    of the built-in manifests if `load_builtins`.
    """
    defaults = builtin_manifests() if load_builtins else []
    try:
        apps = collect_apps(
            [api.Manifest.load(path) for path in manifests],
            [api.Manifest.load(path) for path in defaults],
        )
    except api.ManifestError as error:
        raise CliUsageError(
            t"{sentence(error.message)}", param_hint="--manifest"
        ) from error
    chosen = []
    for name in names:
        app_name, _, version = name.partition("@")
        try:
            chosen.append(choose_app(apps, app_name, version or None))
        except api.ManifestError as error:
            raise CliUsageError(
                t"{sentence(error.message)}", param_hint="--app", value=name
            ) from error
    return chosen


# ruff: ignore[B008]  # See the comment above the option dataclasses in _options.py
async def tunnel_cmd(
    profile: OptionalProfileArg = None,
    /,
    *,
    connection_options: ConnectionOptions = ConnectionOptions(),
    scheduler_options: SchedulerOptions = SchedulerOptions(),
    resource_options: ResourceOptions = ResourceOptions(),
    timing_options: TimingOptions = TimingOptions(),
    alias: AliasOpt = None,
    worker_port: Annotated[
        int | None,
        Parameter(
            name="--worker-port",
            validator=validators.Number(gte=1024, lte=65535),
            help="Worker SSH port; defaults to a random dynamic port.",
        ),
    ] = None,
    worker_port_retries: Annotated[
        int,
        Parameter(
            name="--worker-port-retries",
            validator=validators.Number(gte=0, lte=_MAX_WORKER_PORT_RETRIES),
            help=(
                "Alternate random worker SSH ports to try after a bind failure; "
                "ignored with --worker-port."
            ),
        ),
    ] = _DEFAULT_WORKER_PORT_RETRIES,
    auto_provision: Annotated[
        bool | None,
        Parameter(
            name="--auto-provision",
            negative="--no-auto-provision",
            help=(
                "Provision or repair worker infrastructure before submission. "
                "Defaults to the auto_provision setting."
            ),
        ),
    ] = None,
    manifest: ManifestOpt = None,
    load_builtin_manifests: LoadBuiltinManifestsOpt = None,
    app: AppOpt = None,
) -> None:
    """
    Allocate a compute node and expose its SSH service through a tunnel, with
    apps running on it.
    """
    # Nothing else runs on the event loop until connecting, so the local work up
    # to there (reading local files) may block it.
    if load_builtin_manifests is None:
        load_builtin_manifests = config.apps.load_builtin_manifests
    apps = _chosen_apps(manifest or [], app or [], load_builtins=load_builtin_manifests)
    select_profile(profile)
    login, password = login_from_cli(connection_options, profile_name=profile)
    job = job_from_cli(
        profile,
        scheduler_options=scheduler_options,
        resource_options=resource_options,
        timing_options=timing_options,
        auto_provision=auto_provision,
        app_resources=combined_resources(apps),
    )
    alias = _resolve_alias(alias, profile_name=profile, login=login, job=job)
    # The tunnel is known by its alias, which is usually the profile name.
    label_log_file(alias)
    if profile is None:
        logger.info(
            t"Using anonymous configuration with {job.scheduler.type:detail} scheduler."
        )
    else:
        logger.info(
            t"Using profile {profile:name} with {job.scheduler.type:detail} scheduler."
        )
    run = TunnelRun(
        alias,
        login,
        job,
        apps=apps,
        apps_root=config.resolve_profile(profile).apps.root,
    )
    try:
        # Before connecting, so the user isn't asked for a password first.
        if not job.auto_provision and (missing := api.missing_credentials(login)):
            raise api.MissingCredentials(missing)
        # Before connecting too: a tunnel that `ssh <alias>` won't reach is of no
        # use, and the job may queue for long before it would tell.
        async with api.reserve_alias(alias) as reservation:
            # A task of its own, so that stopping while connecting cancels it,
            # not the command.
            serving = run.serving = asyncio.ensure_future(
                serve(
                    run,
                    reservation,
                    password=password,
                    worker_ports=(
                        (worker_port,)
                        if worker_port is not None
                        else select_ports(worker_port_retries + 1)
                    ),
                )
            )
            try:
                await asyncio.wait([serving])
            finally:
                if not serving.done():
                    serving.cancel()
                    await asyncio.wait([serving])
            # Cancelled only by stopping while connecting: as a stopped job.
            if not serving.cancelled():
                serving.result()
    except api.AliasUnreachable as error:
        logger.error(
            t"Could not start tunnel: {f'ssh {alias}':suggestion} would not reach "
            t"it: {include.explanation(error.problem)}"
        )
        include.print_include_lines(error.problem)
        raise SystemExit(1) from error
    except api.ConfigError as error:
        # A password source that couldn't be read when connecting.
        raise login_usage_error(
            error, connection_options, profile_name=profile
        ) from error
    except (MessageError, RuntimeError, asyncssh.Error, OSError) as error:
        # ruff: ignore[TRY401]  # The terminal shows the traceback only at DEBUG
        logger.exception(
            t"{'Tunnel stopped' if run.served else 'Could not start tunnel'}: {error}"
        )
        raise SystemExit(1) from error


class TunnelRun:
    """
    One `ezhpcy tunnel`: the apps it runs, under `apps_root` on the node (by
    default the node's own), what its status panel shows, how Ctrl+C stops
    it, and whether it served.
    """

    def __init__(
        self,
        alias: str,
        login: api.Login,
        config: api.JobConfig,
        *,
        apps: Sequence[api.App | api.Link] = (),
        apps_root: PurePosixPath | None = None,
    ) -> None:
        self.alias = alias
        self.login = login
        self.config = config
        self.apps = tuple(apps)
        self.apps_root = apps_root
        self.job: api.Job | None = None
        # Whether its Host block was published.
        self.served = False
        # What connects and serves, once started; stopping cancels it until
        # there is a job.
        self.serving: asyncio.Future[None] | None = None
        self._stopped_while_connecting = False
        # Until the job exists.
        self._status = api.JobStatus(
            phase=api.Phase.CONNECTING,
            resources=config.resources,
            submission_command=config.scheduler.interactive_submission_command,
        )
        # What has no attachment of its own: the apps that failed to start, and
        # the links, once published.
        self._unattached: list[api.AppStatus | api.LinkStatus] = []

    @property
    def status(self) -> api.JobStatus:
        status = self.job.status if self.job is not None else self._status
        if not self._unattached:
            return status
        return replace(status, attachments=(*status.attachments, *self._unattached))

    async def start_app(self, host: api.Host, app: api.App) -> api.RunningApp | None:
        """
        Run `app` on `host`, and print its URL once it is ready; or say why it
        couldn't start, and return None.
        """
        try:
            running = await host.run_app(app, apps_root=self.apps_root)
        except (MessageError, RuntimeError, asyncssh.Error, OSError) as error:
            logger.error(
                t"Could not start app {app!s:name}: {error}. The tunnel keeps serving."
            )
            self._unattached.append(
                api.AppStatus(
                    name=app.name, version=app.version, state=api.AppState.EXITED
                )
            )
            return None
        _print_url(running.url)
        return running

    def show_link(self, link: api.Link) -> None:
        """Print `link`'s URL, for the alias, and show it with the apps."""
        url = link.url(self.alias)
        self._unattached.append(
            api.LinkStatus(
                name=link.name,
                version=link.version,
                url=url,
                status_hints=link.status_hints(self.alias),
            )
        )
        _print_url(url)

    def stop(self, why: Template) -> None:
        """Stop the tunnel: its job, or connecting, if it hasn't one yet."""
        if self.job is not None:
            self.job.stop(plain(why))
            return
        if self._stopped_while_connecting or self.serving is None:
            return
        self._stopped_while_connecting = True
        logger.info(t"Stopping job: {why}")
        self._status = api.JobStatus(phase=api.Phase.STOPPING)
        self.serving.cancel()


async def serve(
    run: TunnelRun,
    reservation: api.AliasReservation,
    *,
    password: SecretStr | None,
    worker_ports: tuple[int, ...],
) -> None:
    """
    Connect, enter a job, publish it under the reserved alias, start the apps on
    its node, and wait until it ends, or `run` is stopped; then clean up, the
    apps first.
    """
    login = run.login
    panel = StatusPanel(
        lambda: run.status,
        title=t"Tunnel {run.alias:name}",
        login=f"{login.user}@{login.host}",
    )
    with live_display(panel), _interrupts_stop(run.stop):
        async with api.connect(
            login, prompts=cli_prompts(), password=password
        ) as login_node:
            job = run.job = login_node.job(run.config, worker_ports=worker_ports)
            try:
                async with job:
                    published = await job.publish(reservation)
                    if published.problem is not None:
                        include.print_include_lines(published.problem)
                    run.served = True
                    logger.info(
                        t"Tunnel ready for {run.alias:name} via worker "
                        t"{job.node:endpoint}."
                    )
                    watchers: list[asyncio.Task[None]] = []
                    try:
                        for app in run.apps:
                            if isinstance(app, api.Link):
                                run.show_link(app)
                                continue
                            running = await run.start_app(job.host, app)
                            if running is not None:
                                watchers.append(
                                    asyncio.create_task(_watch_app(running))
                                )
                        await job.wait()
                    finally:
                        # Before the apps stop with the job.
                        for watcher in watchers:
                            watcher.cancel()
            except api.JobEnded:
                pass


def _print_url(url: str) -> None:
    # On stdout, for scripts; the log and the status are on stderr.
    console.print(url, soft_wrap=True, markup=False, highlight=False)


async def _watch_app(running: api.RunningApp) -> None:
    """Say so when an app exits while serving, after the end of its stderr."""
    try:
        exit_status = await running.wait()
    except (RuntimeError, asyncssh.Error, OSError) as error:
        # Its host's connection is lost, which ends the job.
        logger.debug(t"Stopped watching app {running.app!s:name}: {error!r}")
        return
    await log_stderr(running.process)
    logger.error(
        t"App {running.app!s:name} exited with status {exit_status:detail}. The "
        t"tunnel keeps serving."
    )


@contextmanager
def _interrupts_stop(stop: Callable[[Template], object]) -> Generator[None]:
    """
    Make the first Ctrl+C (or Ctrl+Break, on Windows) call `stop`, which stops
    the job and cleans up, and the second quit at once, without cleaning up.

    It replaces asyncio's handler, whose second Ctrl+C would interrupt whichever
    cleanup step is running, and possibly leave the job queued or running.
    """
    loop = asyncio.get_running_loop()
    presses = 0

    def interrupt(_signum: int, _frame: FrameType | None) -> None:
        nonlocal presses
        presses += 1
        if presses == 1:
            loop.call_soon_threadsafe(stop, t"Ctrl+C")
            return
        logger.warning(
            "Quitting without cleaning up. The worker job ends by itself once its "
            "heartbeats stop."
        )
        # Hidden, the live display gives the terminal its cursor back. Briefly: a
        # terminal that doesn't take output mustn't keep it running.
        hide_live_display()
        flush_output(_EXIT_FLUSH_SECONDS)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(130)

    with interrupts_handled_by(interrupt):
        yield
