import typer
from rich.table import Table

from ezhpcy import console
from ezhpcy.cli.version import ezhpcy_version
from ezhpcy.config import config
from ezhpcy.utils import local_machine_id


def info_cmd(json: bool = False) -> None:
    """Show debug information for EzHPCy."""

    data = [
        {
            "key": "EZHPCY Version",
            "value": ezhpcy_version(),
            "description": "Current version of EzHPCy.",
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

    # General
    table.add_row("EzHPCy Version", ezhpcy_version(), "Current version of EzHPCy.")
    table.add_row(
        "Machine ID", local_machine_id(), "Unique identifier for the local machine."
    )

    table.add_section()

    # Directories
    table.add_row(
        "Cache Directory",
        str(config.local_file.cache_dir),
        "Directory where EzHPCy caches data.",
    )
    table.add_row(
        "Config Directory",
        str(config.local_file.config_dir),
        "Directory where EzHPCy configuration files are stored.",
    )
    table.add_row(
        "Data Directory",
        str(config.local_file.data_dir),
        "Directory where EzHPCy stores data.",
    )
    table.add_row(
        "Runtime Directory",
        str(config.local_file.runtime_dir),
        "Directory where EzHPCy stores runtime files.",
    )

    console.print(table)
