lazy from rich.table import Table
lazy from rich.text import Text

lazy from ezhpcy.config import config
lazy from ezhpcy.console import console


def list_profiles_cmd() -> None:
    """List the profiles in the local configuration."""
    if not config.profile:
        console.print("No profiles configured.")
        return

    table = Table(box=None, show_edge=False)
    table.add_column("Profile", style="bold")
    table.add_column("Description")

    for name, profile_config in config.profile.items():
        # Text, so Rich doesn't read a description's `[...]` as markup.
        table.add_row(Text(name), Text(profile_config.description or ""))

    console.print(table)
