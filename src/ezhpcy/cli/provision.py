lazy import asyncio
lazy from typing import Annotated

lazy from cyclopts import Parameter
lazy from rich.prompt import Confirm

from ezhpcy.cli._options import ConfigArg, ConnectionOptions, OptionalProfileArg
lazy from ezhpcy.cli._resolve import login_from_cli, select_profile
lazy from ezhpcy.cli.utils.prompt import ask
lazy from ezhpcy.cli.utils.ssh import connect_or_exit
lazy from ezhpcy.console import error_console
lazy from ezhpcy.messages import print_aborted, print_success, rich_text
lazy from ezhpcy.provision_host import provision_worker_infrastructure
lazy from ezhpcy.provision_local import WorkerCredentials
lazy from ezhpcy.utils import local_machine_id


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
    credentials = WorkerCredentials.for_login(
        login, machine_id=await asyncio.to_thread(local_machine_id)
    )
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

    async with await connect_or_exit(
        login, password, connection_options, profile_name=profile
    ) as host:
        remote_state = await host.remote_state()
        await provision_worker_infrastructure(host, remote_state, credentials)
    print_success(t"Remote provisioning completed.")
