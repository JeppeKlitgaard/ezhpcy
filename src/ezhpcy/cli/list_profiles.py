import typer
from rich.table import Table

from ezhpcy import console
from ezhpcy.config import config


def list_profiles_cmd() -> None:
    """List the profiles in the local configuration."""
    if not config.profile:
        typer.echo("No profiles configured.")
        return

    table = Table(box=None, show_edge=False)
    table.add_column("Profile", style="bold")
    table.add_column("Description")

    for name in sorted(config.profile, key=str.casefold):
        profile = config.profile[name]
        table.add_row(
            name,
            profile.description or "",
        )

    console.print(table)
