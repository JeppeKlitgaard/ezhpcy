lazy from typing import Annotated

lazy from cyclopts import Parameter
lazy from rich.prompt import Confirm

from ezhpcy.cli._options import ConfigArg, ConnectionOptions, OptionalProfileArg
from ezhpcy.constants import APPS_DIRECTORY_NAME
lazy from ezhpcy.cli._resolve import login_from_cli, select_profile
lazy from ezhpcy.cli.utils.prompt import ask
lazy from ezhpcy.cli.utils.ssh import connect_or_exit
lazy from ezhpcy.console import error_console
lazy from ezhpcy.messages import print_aborted, print_success, rich_text
lazy from ezhpcy.provision_host import prune_all_remote_data, prune_stale_data


# ruff: ignore[B008]  # See the comment above the option dataclasses in _options.py
async def prune_cmd(
    profile: OptionalProfileArg = None,
    /,
    *,
    config: ConfigArg,
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
    select_profile(config, profile)
    login, password = login_from_cli(config, connection_options, profile_name=profile)
    if not all_data:
        async with await connect_or_exit(
            login, password, connection_options, profile_name=profile
        ) as host:
            await prune_stale_data(host, await host.remote_state())
        return

    # Asked before connecting (see `ask`).
    target = f"{login.user}@{login.host}"
    apps_root = config.resolve_profile(profile).apps.root
    apps = (
        t"{str(apps_root):path}"
        if apps_root is not None
        else t"{f'$XDG_CACHE_HOME/{APPS_DIRECTORY_NAME}':path}"
    )
    user_accepts = yes or ask(
        Confirm.ask,
        rich_text(
            t"This will remove {'all':emphasis} EzHPCy-managed infrastructure and "
            t"data from {target:name}: the {'ezhpcy':path} directories under its "
            t"{'$XDG_CACHE_HOME':path} and {'$XDG_STATE_HOME':path}, and apps' "
            t"environments, assets and {'workdirs':emphasis}, with whatever apps "
            t"wrote there, in {apps}. Proceed?"
        ),
        console=error_console,
        default=False,
    )
    if not user_accepts:
        print_aborted(t"Prune cancelled.")
        raise SystemExit(1)

    async with await connect_or_exit(
        login, password, connection_options, profile_name=profile
    ) as host:
        remote_state = await host.remote_state()
        await prune_all_remote_data(
            host, remote_state, remote_state.apps_root(apps_root)
        )
    print_success(t"All managed remote data removed.")
