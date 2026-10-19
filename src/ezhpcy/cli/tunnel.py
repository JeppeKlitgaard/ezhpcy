import logging
from contextlib import contextmanager
lazy import asyncio
lazy import functools
lazy import os
lazy import shlex
lazy import sys
lazy from collections.abc import Generator
lazy from types import FrameType
lazy from typing import Annotated

lazy import asyncssh
lazy from cyclopts import Parameter, validators
lazy from rich.panel import Panel

from ezhpcy.cli._options import (
    AliasOpt,
    ConnectionOptions,
    OptionalProfileArg,
    ResourceOptions,
    SchedulerOptions,
    TimingOptions,
    given_options,
)
from ezhpcy.logging import get_logger
lazy from ezhpcy.cli._errors import CliUsageError, sentence
lazy from ezhpcy.cli._resolve import (
    LIST_PROFILES_HINT,
    connection_from_cli,
    missing_setting_error,
    resolve_profile_config,
    with_cli_options,
)
lazy from ezhpcy.cli.utils import ssh as cli_ssh
lazy from ezhpcy.cli.utils.interrupts import interrupts_handled_by
lazy from ezhpcy.config import config
lazy from ezhpcy.console import console
lazy from ezhpcy.logging import flush_logs
lazy from ezhpcy.messages import rich_text
lazy from ezhpcy.scheduler.types import SchedulerType
lazy from ezhpcy.tunnel import TunnelError
lazy from ezhpcy.tunnel.session import TunnelSession
lazy from ezhpcy.tunnel.ssh_config import WorkerHost
lazy from ezhpcy.types import (
    ResolvedConfig,
    ResolvedProfileConfig,
    ResolvedSchedulerConfig,
    SchedulerConfig,
    SubmissionMode,
)
lazy from ezhpcy.worker.job import select_ports

_DEFAULT_WORKER_PORT_RETRIES = 5
# Far fewer than the 16384 dynamic ports `select_ports` draws from.
_MAX_WORKER_PORT_RETRIES = 100
# How long the second Ctrl+C waits for the log to be written before quitting.
_EXIT_FLUSH_SECONDS = 0.5
logger = get_logger(__name__)


def _resolve_ssh_host(
    alias: str | None,
    *,
    profile_name: str | None,
    resolved: ResolvedConfig,
) -> WorkerHost:
    """Pick the Host alias this tunnel serves; profiles default to their name."""
    if alias is None:
        # Identical anonymous options yield the same alias across restarts, so
        # saved hosts (e.g. in VS Code) keep working.
        alias = profile_name or f"ezhpcy-{resolved.alias_digest()[:12]}"
    if alias != profile_name and alias in config.profile:
        raise CliUsageError(
            t"It is the name of profile {alias:name}; choose another alias.",
            param_hint="--alias",
            value=alias,
        )
    try:
        return WorkerHost.for_endpoint(
            alias, user=resolved.connection.user, host=str(resolved.connection.host)
        )
    except ValueError as error:
        raise CliUsageError(
            t"{sentence(str(error))}", param_hint="--alias", value=alias
        ) from error


def _check_interactive_submission_command(
    scheduler: SchedulerConfig,
    *,
    scheduler_options: SchedulerOptions,
    resource_options: ResourceOptions,
    profile_name: str | None,
    profile_config: ResolvedProfileConfig,
) -> None:
    """Reject an interactive submission command that would drop requested settings.

    The command is a site wrapper (e.g. DTU's `a100sh`) that EzHPCy runs instead of
    building its own scheduler request, so:

    - It only makes sense in interactive submission mode.
    - It decides the resources itself, so every `resources.*` (and, for LSF,
      `lsf.*`) setting is ignored. Asking for one explicitly, on the CLI or in
      the selected profile itself, is an error rather than silently getting
      something else.
    - Settings the profile only inherits are logged and ignored instead, because
      wrapper profiles typically inherit from a base profile that sets resources
      (e.g. `dtu-a100sh` inherits from `dtu-base-lsf`). Telling inherited from
      direct settings is why the raw `config.profile[profile_name]` is consulted.
    """
    command = scheduler.interactive_submission_command
    if command is None:
        return
    # The command comes from the CLI or, failing that, from the profile.
    command_source = (
        "--interactive-submission-command"
        if scheduler_options.interactive_submission_command is not None
        else f"profile.{profile_name}.scheduler.interactive_submission_command"
    )
    command_value = shlex.join(command)
    if scheduler.submission_mode is SubmissionMode.BATCH:
        raise CliUsageError(
            t"It requires {'scheduler.submission_mode':name} to be "
            t"{'interactive':choice}.",
            param_hint=command_source,
            value=command_value,
        )

    submission_sections = ["resources"]
    if scheduler.type is SchedulerType.LSF:
        submission_sections.append("lsf")
    configured_submission_options = {
        f"{section}.{field}"
        for section in submission_sections
        for field in getattr(profile_config, section).model_fields_set
    }
    directly_configured_submission_options = (
        {
            f"{section}.{field}"
            for section in submission_sections
            for field in getattr(config.profile[profile_name], section).model_fields_set
        }
        if profile_name is not None
        else set()
    )
    cli_submission_options = [
        t"{flag:name}={value}"
        for flag, value in given_options(resource_options).values()
    ]
    conflicts = [
        *(
            t"{f'profile.{profile_name}.{option}':name}"
            for option in sorted(directly_configured_submission_options)
        ),
        *cli_submission_options,
    ]
    if conflicts:
        raise CliUsageError(
            t"It replaces the scheduler-generated request and cannot be "
            t"combined with submission options: {conflicts}.",
            param_hint=command_source,
            value=command_value,
        )
    inherited_submission_options = (
        configured_submission_options - directly_configured_submission_options
    )
    if inherited_submission_options:
        logger.info(
            t"Ignoring inherited scheduler submission options for "
            t"{'scheduler.interactive_submission_command':name}: "
            t"{sorted(inherited_submission_options):name}"
        )


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
) -> None:
    """Allocate a compute node and expose its SSH service through a tunnel."""
    # Nothing else runs on the event loop until the session starts, so the local
    # work up to there (reading the keyring and local files) may block it.
    session: TunnelSession | None = None
    profile_config = resolve_profile_config(profile)
    conn_info = connection_from_cli(
        connection_options, profile_config.connection, profile_name=profile
    )
    scheduler = with_cli_options(profile_config.scheduler, scheduler_options)
    resources = with_cli_options(profile_config.resources, resource_options)
    timings = with_cli_options(profile_config.timings, timing_options)
    try:
        for name, cli_param in (
            ("type", "--scheduler"),
            ("submission_mode", "--submission-mode"),
        ):
            if getattr(scheduler, name) is None:
                raise missing_setting_error(
                    f"scheduler.{name}",
                    cli_param=cli_param,
                    config_param=(
                        f"profile.{profile}.scheduler.{name}" if profile else None
                    ),
                    hint=LIST_PROFILES_HINT if profile is None else t"",
                )
        resolved = ResolvedConfig(
            description=profile_config.description,
            connection=conn_info,
            scheduler=ResolvedSchedulerConfig.model_validate(scheduler.model_dump()),
            resources=resources,
            timings=timings,
            lsf=profile_config.lsf,
            pbs=profile_config.pbs,
        )
        _check_interactive_submission_command(
            scheduler,
            scheduler_options=scheduler_options,
            resource_options=resource_options,
            profile_name=profile,
            profile_config=profile_config,
        )
        ssh_host = _resolve_ssh_host(alias, profile_name=profile, resolved=resolved)
        if auto_provision is None:
            auto_provision = config.auto_provision
        # Before connecting, so the user isn't asked for a password first.
        if not auto_provision and (
            missing := ssh_host.credentials.missing_tunnel_files()
        ):
            raise TunnelError(
                "worker SSH credentials are missing; run `ezhpcy provision` first "
                f"(missing: {', '.join(map(str, missing))})"
            )
        worker_ports = (
            (worker_port,)
            if worker_port is not None
            else select_ports(worker_port_retries + 1)
        )
        session = TunnelSession(
            resolved=resolved,
            ssh_host=ssh_host,
            connect=cli_ssh.connect_interactive,
            on_ready=functools.partial(
                asyncio.to_thread, _print_ready_banner, ssh_host.alias
            ),
            profile_name=profile,
            worker_ports=worker_ports,
            auto_provision=auto_provision,
        )
        with _interrupts_stop(session):
            await session.run()
    except (RuntimeError, asyncssh.Error, OSError) as error:
        served = session is not None and session.ready
        logger.error(
            t"{'Tunnel stopped' if served else 'Could not start tunnel'}: {error}",
            exc_info=logger.isEnabledFor(logging.DEBUG),
        )

        raise SystemExit(1) from error


def _print_ready_banner(alias: str) -> None:
    """Say how to connect, on stdout, after the log so far."""
    console.print(
        Panel(
            rich_text(
                t"Connect with {f'ssh {alias}':suggestion} or select "
                t"{alias:suggestion} in your editor's remote development "
                t"interface.\n{'Press Ctrl+C to stop.':aside}"
            ),
            title="Tunnel ready",
            title_align="left",
            border_style="green",
            expand=False,
            padding=(1, 2),
        )
    )


@contextmanager
def _interrupts_stop(session: TunnelSession) -> Generator[None]:
    """
    Make the first Ctrl+C (or Ctrl+Break, on Windows) stop the tunnel and clean
    up, and the second quit at once, without cleaning up.

    It replaces asyncio's handler, whose second Ctrl+C would interrupt whichever
    cleanup step is running, and possibly leave the job queued or running.
    """
    loop = asyncio.get_running_loop()
    presses = 0

    def stop() -> None:
        session.stop("Ctrl+C")
        logger.info("Press Ctrl+C again to quit without cleaning up.")

    def interrupt(_signum: int, _frame: FrameType | None) -> None:
        nonlocal presses
        presses += 1
        if presses == 1:
            loop.call_soon_threadsafe(stop)
            return
        logger.warning(
            "Quitting without cleaning up. The worker job ends by itself once its "
            "heartbeats stop."
        )
        # Briefly: a terminal that doesn't take output mustn't keep it running.
        flush_logs(_EXIT_FLUSH_SECONDS)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(130)

    with interrupts_handled_by(interrupt):
        yield
