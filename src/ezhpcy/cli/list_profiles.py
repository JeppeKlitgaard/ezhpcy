lazy from rich.table import Table
lazy from rich.text import Text

lazy from ezhpcy.cli._console import console
lazy from ezhpcy.cli._options import ConfigArg


def list_profiles_cmd(*, config: ConfigArg) -> None:
    """List the profiles in the local configuration."""
    if not config.profiles:
        console.print("No profiles configured.")
        return

    table = Table(box=None, show_edge=False)
    table.add_column("Profile", style="bold")
    table.add_column("Description")

    for name, profile in config.profiles.items():
        # Text, so Rich doesn't read a description's `[...]` as markup.
        table.add_row(Text(name), Text(profile.description or ""))

    console.print(table)
