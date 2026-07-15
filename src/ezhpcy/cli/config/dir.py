import typer

from ezhpcy.config import get_config


def dir_cmd() -> None:
    """Print the ezhpcy configuration directory."""
    typer.echo(get_config().local_file.config_dir)
