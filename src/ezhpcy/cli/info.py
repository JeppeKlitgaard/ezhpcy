import typer
from rich.table import Table

from ezhpcy import console
from ezhpcy.cli.version import ezhpcy_version
from ezhpcy.utils import local_machine_id


def info_cmd(json: bool = False) -> None:
    """Show debug information for ezhpcy."""

    data = [
        {
            "key": "EZHPCY Version",
            "value": ezhpcy_version(),
            "description": "Current version of ezhpcy.",
        },
        {
            "key": "Machine ID",
            "value": local_machine_id(),
            "description": "Unique identifier for the local machine.",
        },
    ]

    if json:
        typer.echo(json.dumps(data, indent=4))
        return

    table = Table(title="EzHPCy Information")

    table.add_column("Key", justify="left", style="cyan", no_wrap=True)
    table.add_column("Value", justify="left", style="white", no_wrap=True)
    table.add_column("Description", justify="left", style="green", no_wrap=False)

    table.add_row("EZHPCY Version", ezhpcy_version(), "Current version of ezhpcy.")
    table.add_row(
        "Machine ID", local_machine_id(), "Unique identifier for the local machine."
    )

    console.print(table)
