import typer

from ezhpcy.config import config


def dir_cmd() -> None:
    """Print the EzHPCy configuration directory."""
    typer.echo(config.local_file.config_dir)
