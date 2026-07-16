from json import dumps as json_dumps

import typer
from rich.table import Table

from ezhpcy import console
from ezhpcy.cli.version import ezhpcy_version
from ezhpcy.config import config
from ezhpcy.utils import local_machine_id


def info_cmd(json: bool = False) -> None:
    """Show debug information for EzHPCy."""

    datas = [
        # Section 1: General
        [
            {
                "key": "EzHPCy Version",
                "value": ezhpcy_version(),
                "description": "Current version of EzHPCy.",
            },
            {
                "key": "Machine ID",
                "value": local_machine_id(),
                "description": "Unique identifier for the local machine.",
            },
        ],
        # Section 2: Directories
        [
            {
                "key": "Cache Directory",
                "value": str(config.local_file.cache_dir),
                "description": "Directory where EzHPCy caches data.",
            },
            {
                "key": "Config Directory",
                "value": str(config.local_file.config_dir),
                "description": "Directory where EzHPCy configuration files are stored.",
            },
            {
                "key": "Data Directory",
                "value": str(config.local_file.data_dir),
                "description": "Directory where EzHPCy stores data.",
            },
            {
                "key": "Runtime Directory",
                "value": str(config.local_file.runtime_dir),
                "description": "Directory where EzHPCy stores runtime files.",
            },
        ],
    ]

    if json:
        # flat_data = [*section for section in datas]  # 3.15+
        flat_data = []
        for section in datas:
            flat_data.extend(section)

        typer.echo(json_dumps(flat_data, indent=4))
        return

    table = Table(title="EzHPCy Information")

    table.add_column("Key", justify="left", style="cyan", no_wrap=True)
    table.add_column("Value", justify="left", style="white", no_wrap=True)
    table.add_column("Description", justify="left", style="green", no_wrap=False)

    for i, section in enumerate(datas):
        if i > 0:
            table.add_section()

        for row in section:
            table.add_row(row["key"], row["value"], row["description"])

    console.print(table)
