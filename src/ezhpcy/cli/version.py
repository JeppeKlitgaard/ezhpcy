import json as json_lib
from typing import Annotated

import typer

from ezhpcy.utils import ezhpcy_version


def version_cmd(
    json: Annotated[
        bool,
        typer.Option("--json", help="Output version information as JSON."),
    ] = False,
) -> None:
    """Show the ezhpcy version."""
    current_version = ezhpcy_version()
    if json:
        typer.echo(json_lib.dumps({"version": current_version}))
    else:
        typer.echo(current_version)
