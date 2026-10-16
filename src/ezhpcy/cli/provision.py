lazy import asyncio
lazy from typing import Annotated

lazy from cyclopts import Parameter
lazy from rich.prompt import Confirm

from ezhpcy.cli._options import ConnectionOptions, OptionalProfileArg
lazy from ezhpcy.cli._output import print_aborted, print_success, rich_text
lazy from ezhpcy.cli._resolve import connection_from_cli, resolve_profile_config
lazy from ezhpcy.cli.utils.prompt import ask
lazy from ezhpcy.cli.utils.ssh import connect_or_exit
lazy from ezhpcy.console import error_console
lazy from ezhpcy.provision_host import provision_worker_infrastructure
lazy from ezhpcy.provision_local import WorkerCredentials
lazy from ezhpcy.utils import local_machine_id


# ruff: ignore[B008]  # See the comment above the option dataclasses in _options.py
async def provision_cmd(
    profile: OptionalProfileArg = None,
    /,
    *,
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
    conn_info = connection_from_cli(
        connection_options,
        resolve_profile_config(profile).connection,
        profile_name=profile,
    )
    credentials = WorkerCredentials.for_login(
        conn_info, machine_id=await asyncio.to_thread(local_machine_id)
    )
    async with await connect_or_exit(conn_info) as login:
        remote_state = await login.remote_state()
        remote_root = remote_state.package_cache_dir()

        user_accepts = yes or ask(
            Confirm.ask,
            rich_text(
                t"This will provision or repair EzHPCy worker infrastructure under "
                t"{remote_root:path}. Proceed?"
            ),
            console=error_console,
            default=True,
        )
        if not user_accepts:
            print_aborted(t"Provisioning cancelled.")
            raise SystemExit(1)

        await provision_worker_infrastructure(login, remote_state, credentials)
    print_success(t"Remote provisioning completed.")
