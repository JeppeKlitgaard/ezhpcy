from typing import Annotated

import typer
from rich.prompt import Confirm

from ezhpcy import console
from ezhpcy.cli.common import with_connection
from ezhpcy.cli.utils.ssh import InteractiveSSHClient
from ezhpcy.provision_host import provision_worker_infrastructure
from ezhpcy.types import ConnectionInfo
from ezhpcy.utils import local_machine_id


@with_connection
def provision_cmd(
    connection: ConnectionInfo,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Proceed with provisioning without asking for confirmation.",
        ),
    ] = False,
) -> None:
    """Idempotently provision worker infrastructure on the remote HPC host."""
    ssh = InteractiveSSHClient(connection, password_prompt=connection.password_prompt)
    ssh.interactive_connect()
    remote_state = ssh.get_remote_state()
    remote_root = remote_state.package_cache_dir()

    user_accepts = yes or Confirm.ask(
        "This will provision or repair [bold purple]EzHPCy[/bold purple] worker "
        f"infrastructure under [bold blue]{remote_root}[/bold blue]. Proceed?",
        console=console,
        default=True,
    )
    if not user_accepts:
        console.print("[bold yellow]Aborted[/bold yellow]: provisioning cancelled.")
        raise typer.Exit(code=1)

    remote_username = connection.user
    assert remote_username is not None
    remote_host = str(connection.host)
    provision_worker_infrastructure(
        ssh,
        remote_state,
        remote_username=remote_username,
        remote_host=remote_host,
        machine_id=local_machine_id(),
    )
    console.print("[bold green]Success[/bold green]: remote provisioning completed.")
