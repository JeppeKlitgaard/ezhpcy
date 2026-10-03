lazy from typing import Annotated

lazy from cyclopts import Parameter
lazy from rich.prompt import Confirm

from ezhpcy.cli._options import ConnectionOptions, OptionalProfileArg
lazy from ezhpcy import console
lazy from ezhpcy.cli._resolve import connection_from_cli, resolve_profile_config
lazy from ezhpcy.cli.utils.ssh import InteractiveSSHClient
lazy from ezhpcy.provision_host import provision_worker_infrastructure
lazy from ezhpcy.utils import local_machine_id


# ruff: ignore[B008]  # See the comment above the option dataclasses in _options.py
def provision_cmd(
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
    connection = connection_from_cli(
        connection_options, resolve_profile_config(profile).connection, profile=profile
    )
    ssh = InteractiveSSHClient(connection, password_prompt=connection.password_prompt)
    ssh.interactive_connect()
    remote_state = ssh.get_remote_state()
    remote_root = remote_state.package_cache_dir()

    user_accepts = yes or Confirm.ask(
        "This will provision or repair [bold purple]EzHPCy[/bold purple] worker "
        f"infrastructure under [bold blue]{remote_root}[/bold blue]. Proceed?",
        console=console,
        default=True,
    )
    if not user_accepts:
        console.print("[bold yellow]Aborted[/bold yellow]: provisioning cancelled.")
        raise SystemExit(1)

    remote_username = connection.user
    assert remote_username is not None
    remote_host = str(connection.host)
    provision_worker_infrastructure(
        ssh,
        remote_state,
        remote_username=remote_username,
        remote_host=remote_host,
        machine_id=local_machine_id(),
    )
    console.print("[bold green]Success[/bold green]: remote provisioning completed.")
