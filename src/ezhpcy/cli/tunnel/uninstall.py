from typing import Annotated

import typer
from rich.prompt import Confirm

from ezhpcy import console
from ezhpcy.cli.tunnel.common import (
    HostOpt,
    PasswordOpt,
    UserOpt,
    connection_info_from_options,
    local_machine_or_fail,
)
from ezhpcy.cli.tunnel.install import INSTALL_DIR_NAME
from ezhpcy.cli.utils.ssh import InteractiveSSHClient
from ezhpcy.config import config


def uninstall_cmd(
    user: UserOpt = config.connection.user,
    password: PasswordOpt = config.connection.password,
    host: HostOpt = config.connection.host,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Proceed with uninstallation without asking for confirmation.",
        ),
    ] = False,
) -> None:
    """Uninstall ezhpcy from the remote HPC host."""
    local_machine_or_fail()

    conn_info = connection_info_from_options(user=user, password=password, host=host)
    ssh = InteractiveSSHClient(conn_info)
    ssh.interactive_connect()

    remote_file_config = ssh.get_file_config()
    remote_install_dir = remote_file_config.data_dir / INSTALL_DIR_NAME
    remote_uninstall_script = remote_install_dir / "uninstall.sh"

    console.print("[bold green]Success[/bold green]: connected to host.")
    user_accepts = yes or Confirm.ask(
        f"This will uninstall [bold purple]ezhpcy[/bold purple] from the remote host using [bold blue]{remote_uninstall_script}[/bold blue]. Proceed?",
        console=console,
        default=True,
    )

    if not user_accepts:
        console.print(
            "[bold yellow]Aborted[/bold yellow]: uninstallation cancelled by user."
        )
        raise typer.Exit(code=1)

    console.print("Running remote uninstaller.")
    uninstall_output = ssh.run(["bash", str(remote_uninstall_script)])
    if uninstall_output:
        console.print(uninstall_output.rstrip())

    console.print("[bold green]Success[/bold green]: remote uninstallation completed.")
