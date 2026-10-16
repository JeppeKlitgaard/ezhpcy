lazy import json as json_lib
lazy from typing import Annotated

lazy from cyclopts import Parameter

lazy from ezhpcy.console import console
lazy from ezhpcy.constants import EZHPCY_VERSION


def version_cmd(
    *,
    json: Annotated[
        bool,
        Parameter(name="--json", help="Output version information as JSON."),
    ] = False,
) -> None:
    """Show the EzHPCy version."""
    if json:
        console.out(json_lib.dumps({"version": EZHPCY_VERSION}))
    else:
        console.out(EZHPCY_VERSION)
