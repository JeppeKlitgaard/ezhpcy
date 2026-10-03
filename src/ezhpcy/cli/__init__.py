lazy import logging
lazy import sys
lazy from collections.abc import Sequence
lazy from typing import Annotated

from cyclopts import (
    App,
    CycloptsError,
    CycloptsPanel,
    Group,
    MissingArgumentError,
    Parameter,
)
lazy from cyclopts.exceptions import STYLE_VALID_CHOICE
lazy from rich.console import Console
lazy from rich.panel import Panel
lazy from rich.text import Text

lazy from ezhpcy.cli._errors import CliUsageError
lazy from ezhpcy.cli._options import DEBUG_ENV_VAR
lazy from ezhpcy.config import config as ezhpcy_config  # To avoid shadowing
lazy from ezhpcy.logging import configure_logging


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
# `app` on the remaining tokens. Each option names its group: setting
# `app.meta.group_parameters` instead would also move every option of a lazily
# registered command there, because Cyclopts resolves those with the meta app as
# their parent.
@app.meta.default
def _meta(
    *tokens: Annotated[str, Parameter(show=False, allow_leading_hyphen=True)],
    debug: Annotated[
        bool,
        Parameter(
            name="--debug",
            help="Enable debug logging and include timestamps in log output.",
            env_var=DEBUG_ENV_VAR,
            group=GLOBAL_OPTIONS,
        ),
    ] = False,
) -> object:
    if debug or ezhpcy_config.debug:
        configure_logging(logging.DEBUG, include_timestamp=True)
    return app(tokens)


# Commands are registered by import path, so a command's module is imported only
# when that command runs.
# Sub-apps: they don't inherit help or version flags by default; pass explicitly
app.command(
    "ezhpcy.cli.provision:provision_cmd",
    name="provision",
    help="Provision EzHPCy worker infrastructure. Usually done automatically.",
    sort_key=1,
)
app.command(
    "ezhpcy.cli.prune:prune_cmd",
    name="prune",
    help="Prune EzHPCy-managed remote data.",
    sort_key=2,
)
app.command(
    "ezhpcy.cli.tunnel:tunnel_cmd",
    name="tunnel",
    alias="t",
    help="Allocate a compute node and host an SSH tunnel to it.",
    sort_key=3,
)
app.command(
    "ezhpcy.cli.proxy:proxy_cmd",
    name="proxy",
    help="Connect through a running tunnel (SSH ProxyCommand).",
    sort_key=4,
)


## ezhpcy config
config_app = App(
    name="config",
    help="Manage the EzHPCy configuration file.",
    group=CONFIGURATION_COMMANDS,
    sort_key=1,
    help_flags=app.help_flags,
    version_flags=app.version_flags,
)
# ezhpcy config edit
config_app.command(
    "ezhpcy.cli.config.edit:edit_cmd",
    name="edit",
    help="Open the configuration file in an editor.",
)
# ezhpcy config load
config_app.command(
    "ezhpcy.cli.config.load:load_cmd",
    name="load",
    help="Load a packaged configuration preset.",
)
app.command(config_app)

# ezhpcy doctor
app.command(
    "ezhpcy.cli.doctor:doctor_cmd",
    name="doctor",
    help="Validates the EzHPCy configuration and suggests fixes if necessary.",
    group=CONFIGURATION_COMMANDS,
    sort_key=2,
)

## ezhpcy keyring
keyring_app = App(
    name="keyring",
    help="Manage login-node passwords in the system keyring.",
    group=CONFIGURATION_COMMANDS,
    sort_key=3,
    help_flags=app.help_flags,
    version_flags=app.version_flags,
)
# ezhpcy keyring set
keyring_app.command(
    "ezhpcy.cli.keyring:set_cmd", name="set", help="Store a login-node password."
)
app.command(keyring_app)

# ezhpcy info
app.command(
    "ezhpcy.cli.info:info_cmd",
    name="info",
    help="Show debug information.",
    group=META_COMMANDS,
    sort_key=1,
)

# ezhpcy version
app.command(
    "ezhpcy.cli.version:version_cmd",
    name="version",
    help="Show the EzHPCy version.",
    group=META_COMMANDS,
    sort_key=2,
)

# ezhpcy list-profiles
app.command(
    "ezhpcy.cli.list_profiles:list_profiles_cmd",
    name="list-profiles",
    help="List configured profiles.",
    group=META_COMMANDS,
    sort_key=3,
)

# Add shell completion installation command.
# Powershell not currently supported, see:
# https://github.com/BrianPugh/cyclopts/issues/985
app.register_install_completion_command(
    help="Install completion for the current shell (bash, zsh or fish).",
    group=GLOBAL_OPTIONS,
)

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
        error_console.print(CycloptsPanel(error.__rich__()))
        sys.exit(2)
