import logging
from typing import Annotated

import typer

from ezhpcy.cli.broker import broker_cmd
from ezhpcy.cli.common import DEBUG_ENV_VAR
from ezhpcy.cli.compute import compute_cmd
from ezhpcy.cli.config import config_app
from ezhpcy.cli.info import info_cmd
from ezhpcy.cli.keyring import keyring_app
from ezhpcy.cli.list_profiles import list_profiles_cmd
from ezhpcy.cli.provision import provision_cmd
from ezhpcy.cli.proxy import proxy_cmd
from ezhpcy.cli.prune import prune_cmd
from ezhpcy.cli.ssh_config import ssh_config_cmd
from ezhpcy.cli.utils.group import EzhpcyTyperGroup
from ezhpcy.cli.version import version_cmd
from ezhpcy.config import config
from ezhpcy.logging import configure_logging

app = typer.Typer(
    cls=EzhpcyTyperGroup,
    help="Utilities for working with HPC facilities.",
    no_args_is_help=True,
)


@app.callback()
def main(
    debug: Annotated[
        bool,
        typer.Option(
            "--debug",
            help="Enable debug logging and include timestamps in log output.",
            envvar=DEBUG_ENV_VAR,
        ),
    ] = False,
) -> None:
    if debug or config.debug:
        configure_logging(logging.DEBUG, include_timestamp=True)


app.command(
    name="provision",
    help="Provision EzHPCy worker infrastructure. Usually done automatically.",
)(provision_cmd)
app.command(name="prune", help="Prune EzHPCy-managed remote data.")(prune_cmd)
app.command(
    name="compute, c",
    help="Allocate a compute node and start its SSH tunnel.",
)(compute_cmd)
app.command(name="broker", help="Run the foreground worker-stream broker.")(broker_cmd)
app.command(name="proxy", help="Proxy SSH bytes through a running tunnel broker.")(
    proxy_cmd
)

# Configuration Commands
app.add_typer(config_app, name="config", rich_help_panel="Configuration Commands")
app.command(
    name="ssh-config",
    help="Print the OpenSSH configuration for the broker-backed worker.",
    rich_help_panel="Configuration Commands",
)(ssh_config_cmd)
app.add_typer(keyring_app, name="keyring", rich_help_panel="Configuration Commands")


# Meta Commands
app.command(
    name="info", help="Show debug information.", rich_help_panel="Meta Commands"
)(info_cmd)
app.command(
    name="version", help="Show the EzHPCy version.", rich_help_panel="Meta Commands"
)(version_cmd)
app.command(
    name="list-profiles",
    help="List configured profiles.",
    rich_help_panel="Meta Commands",
)(list_profiles_cmd)

# Keep the CLI's help order in one place, after every command has been registered.
EzhpcyTyperGroup.command_order = (
    "provision",
    "prune",
    "compute, c",
    "broker",
    "proxy",
    # Configuration Commands
    "config",
    "ssh-config",
    "keyring",
    # Meta Commands
    "info",
    "version",
    "list-profiles",
)
