import logging
import sys
from collections.abc import Sequence
from typing import Annotated

import typer
from cyclopts import (
    App,
    CycloptsError,
    CycloptsPanel,
    Group,
    MissingArgumentError,
    Parameter,
)
from cyclopts.exceptions import STYLE_VALID_CHOICE
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from ezhpcy.cli._errors import CliUsageError
from ezhpcy.cli._options import DEBUG_ENV_VAR
from ezhpcy.cli.config import config_app
from ezhpcy.cli.doctor import doctor_cmd
from ezhpcy.cli.info import info_cmd
from ezhpcy.cli.keyring import keyring_app
from ezhpcy.cli.list_profiles import list_profiles_cmd
from ezhpcy.cli.provision import provision_cmd
from ezhpcy.cli.proxy import proxy_cmd
from ezhpcy.cli.prune import prune_cmd
from ezhpcy.cli.tunnel import tunnel_cmd
from ezhpcy.cli.version import version_cmd
from ezhpcy.config import config
from ezhpcy.logging import configure_logging


def _format_error(error: CycloptsError) -> Panel:
    """Render a parse error, listing the choices when a required one is missing."""
    message = error.__rich__()
    choices = error.argument.get_choices() if error.argument is not None else None
    # Cyclopts lists the choices for an invalid value, but not for a missing one.
    if isinstance(error, MissingArgumentError) and choices:
        message.append(" Choose from: ")
        message.append_text(
            Text(", ").join(
                Text(f'"{choice}"', style=STYLE_VALID_CHOICE) for choice in choices
            )
        )
        message.append(".")
    return CycloptsPanel(message)


GLOBAL_OPTIONS = Group("Global Options", sort_key=0)
COMMANDS = Group("Commands", sort_key=1)
CONFIGURATION_COMMANDS = Group("Configuration Commands", sort_key=2)
META_COMMANDS = Group("Meta Commands", sort_key=3)

app = App(
    name="ezhpcy",
    help="Utilities for working with HPC facilities.",
    # `-h` is short for `--host`.
    help_flags=["--help"],
    # The `version` command covers this.
    version_flags=[],
    group_commands=COMMANDS,
    # Flags have one spelling, e.g. `--yes` without `--no-yes`, unless they name
    # their negative (`--exclusive/--shared`).
    default_parameter=Parameter(negative=()),
    error_formatter=_format_error,
)


# Options that every command takes, such as `--debug`, need a meta app: it parses
# them wherever they appear (before or after the command, up to `--`), then runs
# `app` on the remaining tokens.
app.meta.group_parameters = GLOBAL_OPTIONS


@app.meta.default
def _meta(
    *tokens: Annotated[str, Parameter(show=False, allow_leading_hyphen=True)],
    debug: Annotated[
        bool,
        Parameter(
            name="--debug",
            help="Enable debug logging and include timestamps in log output.",
            env_var=DEBUG_ENV_VAR,
        ),
    ] = False,
) -> object:
    if debug or config.debug:
        configure_logging(logging.DEBUG, include_timestamp=True)
    return app(tokens)


# Within each help group, `sort_key` sets the order; it's alphabetical otherwise.
app.command(
    provision_cmd,
    name="provision",
    help="Provision EzHPCy worker infrastructure. Usually done automatically.",
    sort_key=1,
)
app.command(
    prune_cmd, name="prune", help="Prune EzHPCy-managed remote data.", sort_key=2
)
app.command(
    tunnel_cmd,
    name="tunnel",
    alias="t",
    help="Allocate a compute node and host an SSH tunnel to it.",
    sort_key=3,
)
app.command(
    proxy_cmd,
    name="proxy",
    help="Connect through a running tunnel (SSH ProxyCommand).",
    sort_key=4,
)

config_app.group = CONFIGURATION_COMMANDS
config_app.sort_key = 1
app.command(config_app)
app.command(
    doctor_cmd,
    name="doctor",
    help="Validates the EzHPCy configuration and suggests fixes if necessary.",
    group=CONFIGURATION_COMMANDS,
    sort_key=2,
)
keyring_app.group = CONFIGURATION_COMMANDS
keyring_app.sort_key = 3
app.command(keyring_app)

app.command(
    info_cmd,
    name="info",
    help="Show debug information.",
    group=META_COMMANDS,
    sort_key=1,
)
app.command(
    version_cmd,
    name="version",
    help="Show the EzHPCy version.",
    group=META_COMMANDS,
    sort_key=2,
)
app.command(
    list_profiles_cmd,
    name="list-profiles",
    help="List configured profiles.",
    group=META_COMMANDS,
    sort_key=3,
)

for sub_app in (config_app, keyring_app):
    # Sub-apps don't inherit these from `app`, unlike function commands.
    sub_app.help_flags = app.help_flags
    sub_app.version_flags = app.version_flags

# Cyclopts registers `--help` as a command; list it with `--debug` instead.
# Sub-apps hide their own `--help` unless told otherwise.
for help_app in (app, config_app, keyring_app):
    help_app["--help"].group = GLOBAL_OPTIONS
    help_app["--help"].show = True


def main(
    tokens: Sequence[str] | None = None,
    *,
    console: Console | None = None,
    error_console: Console | None = None,
) -> None:
    """Run the CLI and exit with the command's exit code."""
    error_console = error_console or app.error_console
    try:
        app.meta(tokens, console=console, error_console=error_console)
    # Cyclopts only handles its own parse errors, not errors from command bodies.
    except CliUsageError as error:
        # Like Cyclopts' own errors: the border is red, the message isn't.
        error_console.print(
            CycloptsPanel(Text.from_markup(error.message, style="default"))
        )
        sys.exit(2)
    # Temporary: most command bodies still raise Click's exceptions.
    except typer.BadParameter as error:
        message = error.format_message()
        if not isinstance(message, Text):
            message = Text(message)
        # Like Cyclopts' own errors: the border is red, the message isn't.
        message.style = "default"
        error_console.print(CycloptsPanel(message))
        sys.exit(2)
    except typer.Exit as error:
        sys.exit(error.exit_code)
