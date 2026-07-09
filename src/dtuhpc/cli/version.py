import json as json_lib
from importlib.metadata import PackageNotFoundError, version

import typer
from typing_extensions import Annotated


def dtuhpc_version() -> str:
    try:
        return version("dtuhpc")
    except PackageNotFoundError:
        return "unknown"


def version_cmd(
    json: Annotated[
        bool,
        typer.Option("--json", help="Output version information as JSON."),
    ] = False,
) -> None:
    """Show the dtuhpc version."""
    current_version = dtuhpc_version()
    if json:
        typer.echo(json_lib.dumps({"version": current_version}))
    else:
        typer.echo(current_version)
