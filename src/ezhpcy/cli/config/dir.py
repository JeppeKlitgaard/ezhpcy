import typer

from ezhpcy.config import config


def dir_cmd() -> None:
    """Print the ezhpcy configuration directory."""
    typer.echo(config.local_file.config_dir)
