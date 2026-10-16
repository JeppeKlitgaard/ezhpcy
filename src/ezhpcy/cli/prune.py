import logging
import re
lazy from pathlib import PurePosixPath
lazy from typing import Annotated

lazy import asyncssh
lazy from cyclopts import Parameter
lazy from rich.prompt import Confirm

from ezhpcy.cli._options import ConnectionOptions, OptionalProfileArg
lazy from ezhpcy.cli._output import print_aborted, print_success, rich_text
lazy from ezhpcy.cli._resolve import connection_from_cli, resolve_profile_config
lazy from ezhpcy.cli.utils.prompt import ask
lazy from ezhpcy.cli.utils.ssh import connect_or_exit
lazy from ezhpcy.console import error_console
lazy from ezhpcy.ssh import RemoteHost
lazy from ezhpcy.types import RemoteState

_INSTALLATION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!-]*$")

logger = logging.getLogger(__name__)


async def prune_stale_installations(
    login: RemoteHost, remote_state: RemoteState
) -> tuple[PurePosixPath, ...]:
    """Remove remote EzHPCy cache and state other than the current version's."""
    current_directories = (
        remote_state.package_cache_dir(),
        remote_state.package_state_dir(),
    )
    stale: list[PurePosixPath] = []
    async with login.sftp() as sftp:
        for current_directory in current_directories:
            root = current_directory.parent
            try:
                names = await sftp.listdir(root)
            except asyncssh.SFTPNoSuchFile:
                logger.info("No remote EzHPCy data exists at %s.", root)
                continue

            for name in names:
                if name in {".", "..", current_directory.name}:
                    continue
                if _INSTALLATION_NAME_PATTERN.fullmatch(name) is None:
                    logger.warning(
                        "Retaining unrecognized EzHPCy entry %s/%s.", root, name
                    )
                    continue
                stale.append(root / name)

    if stale:
        await login.run(["rm", "-rf", "--", *(str(path) for path in stale)])
        for path in stale:
            logger.info("Pruned stale EzHPCy data %s.", path)
    else:
        logger.info("Remote EzHPCy cache and state are current.")
    return tuple(stale)


async def prune_stale_pixi_data(
    login: RemoteHost, remote_state: RemoteState
) -> tuple[PurePosixPath, ...]:
    """Remove Pixi homes and caches other than the pinned Pixi version."""
    current_directories = (
        remote_state.pixi_home(),
        remote_state.pixi_cache_dir(),
    )
    stale: list[PurePosixPath] = []
    async with login.sftp() as sftp:
        for current_directory in current_directories:
            root = current_directory.parent
            try:
                names = await sftp.listdir(root)
            except asyncssh.SFTPNoSuchFile:
                logger.info("No remote Pixi cache exists at %s.", root)
                continue

            for name in names:
                if name in {".", "..", current_directory.name}:
                    continue
                if _INSTALLATION_NAME_PATTERN.fullmatch(name) is None:
                    logger.warning(
                        "Retaining unrecognized Pixi cache entry %s/%s.", root, name
                    )
                    continue
                stale.append(root / name)

    if stale:
        await login.run(["rm", "-rf", "--", *(str(path) for path in stale)])
        for path in stale:
            logger.info("Pruned stale Pixi data %s.", path)
    else:
        logger.info("Remote Pixi homes and caches are current.")
    return tuple(stale)


async def prune_all_remote_data(login: RemoteHost, remote_state: RemoteState) -> None:
    """Remove the complete EzHPCy-managed remote footprint."""
    await login.run(
        [
            "rm",
            "-rf",
            "--",
            str(remote_state.package_cache_root()),
            str(remote_state.package_state_root()),
        ]
    )


# ruff: ignore[B008]  # See the comment above the option dataclasses in _options.py
async def prune_cmd(
    profile: OptionalProfileArg = None,
    /,
    *,
    connection_options: ConnectionOptions = ConnectionOptions(),
    all_data: Annotated[
        bool,
        Parameter(
            name=["--all", "-a"],
            help="Remove all EzHPCy-managed remote infrastructure and data.",
        ),
    ] = False,
    yes: Annotated[
        bool,
        Parameter(
            name=["--yes", "-y"],
            help="Remove all remote data without asking for confirmation.",
        ),
    ] = False,
) -> None:
    """Prune stale installations and Pixi data."""
    conn_info = connection_from_cli(
        connection_options, resolve_profile_config(profile).connection, profile=profile
    )
    async with await connect_or_exit(conn_info) as login:
        remote_state = await login.remote_state()

        if not all_data:
            await prune_stale_installations(login, remote_state)
            await prune_stale_pixi_data(login, remote_state)
            return

        user_accepts = yes or ask(
            Confirm.ask,
            rich_text(
                t"This will remove {'all':emphasis} EzHPCy-managed remote "
                t"infrastructure and data under "
                t"{remote_state.package_cache_root():path} and "
                t"{remote_state.package_state_root():path}. Proceed?"
            ),
            console=error_console,
            default=False,
        )
        if not user_accepts:
            print_aborted(t"Prune cancelled.")
            raise SystemExit(1)

        await prune_all_remote_data(login, remote_state)
    print_success(t"All managed remote data removed.")
