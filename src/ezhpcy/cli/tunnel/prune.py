import logging
import re
from pathlib import PurePosixPath
from typing import Annotated

import typer
from rich.prompt import Confirm

from ezhpcy import console
from ezhpcy.cli.tunnel.common import (
    ProfileContext,
    with_profile_options,
)
from ezhpcy.cli.utils.ssh import InteractiveSSHClient
from ezhpcy.ssh import SSHClient
from ezhpcy.types import RemoteState

_INSTALLATION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!-]*$")

logger = logging.getLogger(__name__)


def prune_stale_installations(
    ssh: SSHClient, remote_state: RemoteState
) -> tuple[PurePosixPath, ...]:
    """Remove remote ezhpcy installations other than the current version."""
    current_directory = remote_state.package_cache_dir()
    remote_root = remote_state.package_cache_root()
    try:
        with ssh.sftp_client() as sftp:
            entries = sftp.listdir_attr(str(remote_root))
    except FileNotFoundError:
        logger.info("No remote ezhpcy cache exists at %s.", remote_root)
        return ()

    stale: list[PurePosixPath] = []
    for entry in entries:
        name = entry.filename
        if name == current_directory.name:
            continue
        if _INSTALLATION_NAME_PATTERN.fullmatch(name) is None:
            logger.warning(
                "Retaining unrecognized ezhpcy cache entry %s/%s.", remote_root, name
            )
            continue
        stale.append(remote_root / name)

    if stale:
        ssh.run(["rm", "-rf", "--", *(str(path) for path in stale)])
        for path in stale:
            logger.info("Pruned stale ezhpcy installation %s.", path)
    else:
        logger.info("Remote ezhpcy installation cache is current.")
    return tuple(stale)


def prune_stale_pixi_data(
    ssh: SSHClient, remote_state: RemoteState
) -> tuple[PurePosixPath, ...]:
    """Remove Pixi homes and caches other than the pinned Pixi version."""
    current_directories = (
        remote_state.pixi_home(),
        remote_state.pixi_cache_dir(),
    )
    stale: list[PurePosixPath] = []
    with ssh.sftp_client() as sftp:
        for current_directory in current_directories:
            root = current_directory.parent
            try:
                entries = sftp.listdir_attr(str(root))
            except FileNotFoundError:
                logger.info("No remote Pixi cache exists at %s.", root)
                continue

            for entry in entries:
                name = entry.filename
                if name == current_directory.name:
                    continue
                if _INSTALLATION_NAME_PATTERN.fullmatch(name) is None:
                    logger.warning(
                        "Retaining unrecognized Pixi cache entry %s/%s.", root, name
                    )
                    continue
                stale.append(root / name)

    if stale:
        ssh.run(["rm", "-rf", "--", *(str(path) for path in stale)])
        for path in stale:
            logger.info("Pruned stale Pixi data %s.", path)
    else:
        logger.info("Remote Pixi homes and caches are current.")
    return tuple(stale)


def prune_all_remote_data(ssh: SSHClient, remote_state: RemoteState) -> None:
    """Remove the complete ezhpcy-managed remote footprint."""
    remote_root = remote_state.package_cache_root()
    ssh.run(["rm", "-rf", "--", str(remote_root)])


@with_profile_options
def prune_cmd(
    profile_context: ProfileContext,
    all_data: Annotated[
        bool,
        typer.Option(
            "--all",
            "-a",
            help="Remove all ezhpcy-managed remote infrastructure and data.",
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Remove all remote data without asking for confirmation.",
        ),
    ] = False,
) -> None:
    """Prune stale installations and Pixi data."""
    ssh = InteractiveSSHClient(profile_context.connection)
    ssh.interactive_connect()
    remote_state = ssh.get_remote_state()

    if not all_data:
        prune_stale_installations(ssh, remote_state)
        prune_stale_pixi_data(ssh, remote_state)
        return

    remote_root = remote_state.package_cache_root()
    user_accepts = yes or Confirm.ask(
        "This will remove [bold red]all[/bold red] ezhpcy-managed remote "
        f"infrastructure and data under [bold blue]{remote_root}[/bold blue]. Proceed?",
        console=console,
        default=False,
    )
    if not user_accepts:
        console.print("[bold yellow]Aborted[/bold yellow]: prune cancelled.")
        raise typer.Exit(code=1)

    prune_all_remote_data(ssh, remote_state)
    console.print("[bold green]Success[/bold green]: all managed remote data removed.")
