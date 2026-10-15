import logging
from contextlib import contextmanager
lazy import asyncio
lazy import os
lazy import shlex
lazy import signal
lazy import sys
lazy from collections.abc import Generator
lazy from types import FrameType
lazy from typing import Annotated

lazy import asyncssh
lazy from cyclopts import Parameter, validators

from ezhpcy.cli._options import (
    AliasOpt,
    ConnectionOptions,
    OptionalProfileArg,
    ResourceOptions,
    SchedulerOptions,
    TimingOptions,
    given_options,
)
lazy from ezhpcy.cli._errors import CliUsageError, sentence
lazy from ezhpcy.cli._resolve import (
    LIST_PROFILES_HINT,
    connection_from_cli,
    missing_setting_error,
    resolve_profile_config,
    with_cli_options,
)
lazy from ezhpcy.cli.utils import ssh as cli_ssh
lazy from ezhpcy.config import config
lazy from ezhpcy.ipc import IPCError
lazy from ezhpcy.permissions import FilePermissionError
lazy from ezhpcy.provision_local import WorkerCredentials
lazy from ezhpcy.scheduler.types import SchedulerType
lazy from ezhpcy.tunnel import TunnelError
lazy from ezhpcy.tunnel.session import TunnelSession
lazy from ezhpcy.tunnel.ssh_config import (
    WorkerHost,
    profile_hosts,
    write_profiles_config,
)
lazy from ezhpcy.tunnel.worker import select_worker_ports
lazy from ezhpcy.types import (
    ResolvedConfig,
    ResolvedProfileConfig,
    SchedulerConfig,
    SubmissionMode,
)
lazy from ezhpcy.utils import local_machine_id

_DEFAULT_WORKER_PORT_RETRIES = 5
logger = logging.getLogger(__name__)


def _ensure_local_worker_credentials(credentials: WorkerCredentials) -> None:
    required_files = (
        credentials.client_key,
        credentials.client_public_key,
        credentials.known_hosts,
    )
    missing = [str(path) for path in required_files if not path.is_file()]
    if missing:
        raise TunnelError(
            "worker SSH credentials are missing; run `ezhpcy provision` first "
            f"(missing: {', '.join(missing)})"
        )


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
        alias = profile_name or f"ezhpcy-{resolved.descriptor_digest()[:12]}"
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
    profile: str | None,
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
      direct settings is why the raw `config.profile[profile]` is consulted.
    """
    command = scheduler.interactive_submission_command
    if command is None:
        return
    # The command comes from the CLI or, failing that, from the profile.
    command_source = (
        "--interactive-submission-command"
        if scheduler_options.interactive_submission_command is not None
        else f"profile.{profile}.scheduler.interactive_submission_command"
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
            for field in getattr(config.profile[profile], section).model_fields_set
        }
        if profile is not None
        else set()
    )
    cli_submission_options = [
        t"{flag:name}={value}"
        for flag, value in given_options(resource_options).values()
    ]
    conflicts = [
        *(
            t"{f'profile.{profile}.{option}':name}"
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
            "Ignoring inherited scheduler submission options for "
            "scheduler.interactive_submission_command: %s",
            ", ".join(sorted(inherited_submission_options)),
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
            validator=validators.Number(gte=0),
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
    # work up to there (reading the keyring, writing files) may block it.
    session: TunnelSession | None = None
    profile_config = resolve_profile_config(profile)
    connection = connection_from_cli(
        connection_options, profile_config.connection, profile=profile
    )
    scheduler = with_cli_options(profile_config.scheduler, scheduler_options)
    resources = with_cli_options(profile_config.resources, resource_options)
    timings = with_cli_options(profile_config.timings, timing_options)
    try:
        resolved = ResolvedConfig(
            description=profile_config.description,
            connection=connection,
            scheduler=scheduler,
            resources=resources,
            timings=timings,
            lsf=profile_config.lsf,
            pbs=profile_config.pbs,
        )
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
        _check_interactive_submission_command(
            scheduler,
            scheduler_options=scheduler_options,
            resource_options=resource_options,
            profile=profile,
            profile_config=profile_config,
        )
        ssh_host = _resolve_ssh_host(alias, profile_name=profile, resolved=resolved)
        written_profile_hosts = profile_hosts()
        try:
            write_profiles_config(written_profile_hosts)
        except (OSError, FilePermissionError) as error:
            logger.warning("Could not write the profile SSH configuration: %s", error)
            written_profile_hosts = []
        if auto_provision is None:
            auto_provision = config.auto_provision
        if not auto_provision:
            _ensure_local_worker_credentials(
                WorkerCredentials.for_login(connection, machine_id=local_machine_id())
            )
        worker_ports = (
            (worker_port,)
            if worker_port is not None
            else select_worker_ports(worker_port_retries + 1)
        )
        session = TunnelSession(
            resolved=resolved,
            ssh_host=ssh_host,
            connect=cli_ssh.connect_interactive,
            profile_name=profile,
            # A profile tunnel without user/host overrides renders exactly the
            # block profiles.conf already holds, so it needs no active file.
            ssh_host_in_profiles_config=ssh_host in written_profile_hosts,
            worker_ports=worker_ports,
            auto_provision=auto_provision,
        )
        with _interrupts_stop(session):
            await session.run()
    except (IPCError, RuntimeError, asyncssh.Error, OSError) as error:
        served = session is not None and session.ready
        logger.error(
            "%s: %s",
            "Tunnel stopped" if served else "Could not start tunnel",
            error,
            exc_info=logger.isEnabledFor(logging.DEBUG),
        )
        raise SystemExit(1) from error


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
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(130)

    signals = [signal.SIGINT]
    if hasattr(signal, "SIGBREAK"):
        signals.append(signal.SIGBREAK)
    previous = {signum: signal.signal(signum, interrupt) for signum in signals}
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
