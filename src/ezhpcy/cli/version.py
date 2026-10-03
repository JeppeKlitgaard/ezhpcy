import json as json_lib
from typing import Annotated

from cyclopts import Parameter

from ezhpcy import console
from ezhpcy.utils import ezhpcy_version


def version_cmd(
    *,
    json: Annotated[
        bool,
        Parameter(name="--json", help="Output version information as JSON."),
    ] = False,
) -> None:
    """Show the EzHPCy version."""
    current_version = ezhpcy_version()
    if json:
        console.out(json_lib.dumps({"version": current_version}))
    else:
        console.out(current_version)
