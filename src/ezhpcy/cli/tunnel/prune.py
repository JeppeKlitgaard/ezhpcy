import logging
import re
from importlib import resources
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
from ezhpcy.cli.tunnel.provision import (
    REMOTE_ROOT_NAME,
    _render_shell_script,
)
from ezhpcy.cli.utils.ssh import InteractiveSSHClient
from ezhpcy.config import RemoteFileConfig
from ezhpcy.ssh import SSHClient
from ezhpcy.worker_payload import PAYLOAD_DIRECTORY_NAME, worker_payload_path

PRUNE_ALL_SCRIPT_RESOURCE = "static/data/prune-all.sh.j2"
_PAYLOAD_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")

logger = logging.getLogger(__name__)


def prune_stale_payloads(
    ssh: SSHClient, remote_file_config: RemoteFileConfig
) -> tuple[PurePosixPath, ...]:
    """Remove content-addressed payload directories other than the current one."""
    current_directory = worker_payload_path(remote_file_config).parent
    payload_root = (
        remote_file_config.data_dir / REMOTE_ROOT_NAME / PAYLOAD_DIRECTORY_NAME
    )
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


def prune_all_remote_data(ssh: SSHClient, remote_file_config: RemoteFileConfig) -> None:
    """Remove the complete ezhpcy-managed remote footprint."""
    remote_root = remote_file_config.data_dir / REMOTE_ROOT_NAME
    template = resources.files("ezhpcy").joinpath(PRUNE_ALL_SCRIPT_RESOURCE)
    script = _render_shell_script(template.read_text(encoding="utf-8"))
    remote_script = remote_root / "prune-all.sh"
    with ssh.sftp_client() as sftp:
        sftp.mkdir(remote_root, parents=True, exist_ok=True)
        sftp.write_text(remote_script, script)
        sftp.chmod(str(remote_script), 0o755)
    ssh.run(["bash", str(remote_script)])


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
    remote_file_config = ssh.get_file_config()

    if not all_data:
        prune_stale_payloads(ssh, remote_file_config)
        return

    remote_root = remote_file_config.data_dir / REMOTE_ROOT_NAME
    user_accepts = yes or Confirm.ask(
        "This will remove [bold red]all[/bold red] ezhpcy-managed remote "
        f"infrastructure and data under [bold blue]{remote_root}[/bold blue]. Proceed?",
        console=console,
        default=False,
    )
    if not user_accepts:
        console.print("[bold yellow]Aborted[/bold yellow]: prune cancelled.")
        raise typer.Exit(code=1)

    prune_all_remote_data(ssh, remote_file_config)
    console.print("[bold green]Success[/bold green]: all managed remote data removed.")
