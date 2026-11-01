lazy from typing import Annotated

lazy from cyclopts import Parameter
lazy from rich.prompt import Confirm

from ezhpcy.cli._options import ConfigArg, ConnectionOptions, OptionalProfileArg
lazy from ezhpcy.cli._console import error_console
lazy from ezhpcy.cli._messages import print_aborted, print_success, rich_text
lazy from ezhpcy.cli._resolve import login_from_cli, select_profile
lazy from ezhpcy.cli.utils.prompt import ask
lazy from ezhpcy.cli.utils.ssh import connect_or_exit


# ruff: ignore[B008]  # See the comment above the option dataclasses in _options.py
async def provision_cmd(
    profile: OptionalProfileArg = None,
    /,
    *,
    config: ConfigArg,
    connection_options: ConnectionOptions = ConnectionOptions(),
    yes: Annotated[
        bool,
        Parameter(
            name=["--yes", "-y"],
            help="Proceed with provisioning without asking for confirmation.",
        ),
    ] = False,
) -> None:
    """Idempotently provision worker infrastructure on the remote HPC host."""
    select_profile(config, profile)
    login, password = login_from_cli(config, connection_options, profile_name=profile)
    # Asked before connecting (see `ask`).
    target = f"{login.user}@{login.host}"
    user_accepts = yes or ask(
        Confirm.ask,
        rich_text(
            t"This will provision or repair EzHPCy worker infrastructure on "
            t"{target:name}, under its {'$XDG_CACHE_HOME/ezhpcy':path}. Proceed?"
        ),
        console=error_console,
        default=True,
    )
    if not user_accepts:
        print_aborted(t"Provisioning cancelled.")
        raise SystemExit(1)

    async with connect_or_exit(
        login, password, connection_options, profile_name=profile
    ) as host:
        await host.provision()
    print_success(t"Remote provisioning completed.")
