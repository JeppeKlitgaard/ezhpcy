import json

import typer

from ezhpcy import console
from ezhpcy.cli.utils.ssh import InteractiveSSHClient
from ezhpcy.utils import ezhpcy_version


def get_remote_version(ssh: InteractiveSSHClient) -> str:
    remote_version_json_str = ssh.run(
        "python3 -m ezhpcy.cli.version --json", hide=True
    ).stdout.strip()
    remote_version = json.loads(remote_version_json_str)["version"]
    return remote_version


def do_versions_match(ssh: InteractiveSSHClient) -> bool:
    local_version = ezhpcy_version()
    remote_version = get_remote_version(ssh)
    return local_version == remote_version


def assert_version_match(ssh: InteractiveSSHClient) -> None:
    local_version = ezhpcy_version()
    remote_version = get_remote_version(ssh)

    console.print(
        f"[bold red]ERROR[/bold red]: ezhpcy versions do not match between local ({local_version}) and remote ({remote_version})."
    )
    console.print(
        "[bold yellow]Hint[/bold yellow]: Reinstall remote ezhpcy to match the local version by running: [bold blue]ezhpcy tunnel reinstall[/bold blue] on your local machine."
    )

    raise typer.Exit(code=1)
