lazy from typing import Annotated

lazy from cyclopts import Parameter
lazy from rich.prompt import Confirm

from ezhpcy.cli._options import ConnectionOptions, OptionalProfileArg
lazy from ezhpcy.cli._output import print_aborted, print_success, rich_text
lazy from ezhpcy.cli._resolve import connection_from_cli, resolve_profile_config
lazy from ezhpcy.cli.utils.prompt import ask
lazy from ezhpcy.cli.utils.ssh import connect_or_exit
lazy from ezhpcy.console import error_console
lazy from ezhpcy.provision_host import prune_all_remote_data, prune_stale_data


# ruff: ignore[B008]  # See the comment above the option dataclasses in _options.py
async def prune_cmd(
    profile: OptionalProfileArg = None,
    /,
    *,
    connection_options: ConnectionOptions = ConnectionOptions(),
    all_data: Annotated[
        bool,
        Parameter(
            name=["--all", "-a"],
            help="Remove all EzHPCy-managed remote infrastructure and data.",
        ),
    ] = False,
    yes: Annotated[
        bool,
        Parameter(
            name=["--yes", "-y"],
            help="Remove all remote data without asking for confirmation.",
        ),
    ] = False,
) -> None:
    """Prune stale installations and Pixi data."""
    conn_info = connection_from_cli(
        connection_options,
        resolve_profile_config(profile).connection,
        profile_name=profile,
    )
    async with await connect_or_exit(conn_info) as login:
        remote_state = await login.remote_state()

        if not all_data:
            await prune_stale_data(login, remote_state)
            return

        user_accepts = yes or ask(
            Confirm.ask,
            rich_text(
                t"This will remove {'all':emphasis} EzHPCy-managed remote "
                t"infrastructure and data under "
                t"{remote_state.package_cache_root():path} and "
                t"{remote_state.package_state_root():path}. Proceed?"
            ),
            console=error_console,
            default=False,
        )
        if not user_accepts:
            print_aborted(t"Prune cancelled.")
            raise SystemExit(1)

        await prune_all_remote_data(login, remote_state)
    print_success(t"All managed remote data removed.")
