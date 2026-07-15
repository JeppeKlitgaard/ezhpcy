import logging
import re
from pathlib import PurePosixPath
from typing import Annotated

import typer
from rich.prompt import Confirm

from ezhpcy import console
from ezhpcy.cli.tunnel.common import (
    ProfileContext,
    local_machine_or_fail,
    with_profile_options,
)
from ezhpcy.cli.utils.ssh import InteractiveSSHClient
from ezhpcy.ssh import SSHClient
from ezhpcy.types import RemoteState
from ezhpcy.worker_payload import PAYLOAD_DIRECTORY_NAME, worker_payload_path

_PAYLOAD_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")

logger = logging.getLogger(__name__)


def prune_stale_payloads(
    ssh: SSHClient, remote_state: RemoteState
) -> tuple[PurePosixPath, ...]:
    """Remove content-addressed payload directories other than the current one."""
    current_directory = worker_payload_path(remote_state).parent
    payload_root = remote_state.package_cache_dir() / PAYLOAD_DIRECTORY_NAME
    try:
        with ssh.sftp_client() as sftp:
            entries = sftp.listdir_attr(str(payload_root))
    except FileNotFoundError:
        logger.info("No remote payload cache exists at %s.", payload_root)
        return ()

    stale: list[PurePosixPath] = []
    for entry in entries:
        name = entry.filename
        if name == current_directory.name:
            continue
        if _PAYLOAD_HASH_PATTERN.fullmatch(name) is None:
            logger.warning(
                "Retaining unrecognized payload entry %s/%s.", payload_root, name
            )
            continue
        stale.append(payload_root / name)

    if stale:
        ssh.run(["rm", "-rf", "--", *(str(path) for path in stale)])
        for path in stale:
            logger.info("Pruned stale worker payload %s.", path)
    else:
        logger.info("Remote worker payload cache is current.")
    return tuple(stale)


def prune_all_remote_data(ssh: SSHClient, remote_state: RemoteState) -> None:
    """Remove the complete ezhpcy-managed remote footprint."""
    remote_root = remote_state.package_cache_dir()
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
    """Prune stale payloads, or remove all managed remote data."""
    local_machine_or_fail()
    ssh = InteractiveSSHClient(profile_context.connection)
    ssh.interactive_connect()
    remote_state = ssh.get_remote_state()

    if not all_data:
        prune_stale_payloads(ssh, remote_state)
        return

    remote_root = remote_state.package_cache_dir()
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
