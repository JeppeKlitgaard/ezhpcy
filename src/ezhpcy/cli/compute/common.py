import typer

from ezhpcy.console import console
from ezhpcy.detect.host_type import HostType, get_host_type


def compute_node_or_fail() -> None:
    """
    Fails the command if the current HostType is not COMPUTE_NODE.
    """
    host_type = get_host_type()
    if host_type != HostType.COMPUTE_NODE:
        console.print(
            f"[bold red]ERROR[/bold red]: This command should be run on a compute node (detected: [bold blue]{host_type.value}[/bold blue])."
        )
        raise typer.Exit(code=1)
