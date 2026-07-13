import sys
from pathlib import Path
from typing import Annotated

import typer

from ezhpcy.cli.tunnel.common import UserOpt
from ezhpcy.cli.utils.resolve import resolve_forbidden_none
from ezhpcy.config import config
from ezhpcy.constants import (
    SSH_DIRECTORY_NAME,
    WORKER_CLIENT_KEY_NAME,
    WORKER_HOST_ALIAS,
)


def _config_path(path: Path) -> str:
    """Render an absolute path safely in OpenSSH configuration syntax."""
    value = path.expanduser().resolve().as_posix()
    return f'"{value.replace(chr(34), chr(92) + chr(34))}"'


def _single_token(value: str, *, name: str) -> str:
    if not value or any(character.isspace() for character in value):
        raise typer.BadParameter("must be one non-whitespace token", param_hint=name)
    return value


def render_worker_ssh_config(
    *,
    user: str,
    alias: str = WORKER_HOST_ALIAS,
    ssh_dir: Path,
    python_executable: Path = Path(sys.executable),
) -> str:
    """Render the stable worker alias consumed by OpenSSH and VS Code."""
    user = _single_token(user, name="--user")
    alias = _single_token(alias, name="--alias")
    identity = ssh_dir / WORKER_CLIENT_KEY_NAME
    known_hosts = ssh_dir / "worker_known_hosts"
    return "\n".join(
        (
            f"Host {alias}",
            f"    HostName {alias}",
            f"    User {user}",
            f"    IdentityFile {_config_path(identity)}",
            "    IdentitiesOnly yes",
            f"    UserKnownHostsFile {_config_path(known_hosts)}",
            f"    HostKeyAlias {WORKER_HOST_ALIAS}",
            "    StrictHostKeyChecking yes",
            "    ProxyCommand "
            f"{_config_path(python_executable)} -m ezhpcy.cli.entry proxy",
            "",
        )
    )


def ssh_config_cmd(
    user: UserOpt = config.connection.user,
    alias: Annotated[
        str,
        typer.Option(
            "--alias",
            help="Stable Host alias to expose to OpenSSH and VS Code.",
        ),
    ] = WORKER_HOST_ALIAS,
) -> None:
    """Print an OpenSSH Host block for the broker-backed worker connection."""
    resolved_user = resolve_forbidden_none(
        cli_value=user,
        config_value=config.connection.user,
        name="user",
        cli_param="--user",
        config_param="connection.user",
    )
    ssh_dir = config.local_file.config_dir / SSH_DIRECTORY_NAME
    typer.echo(
        render_worker_ssh_config(
            user=resolved_user,
            alias=alias,
            ssh_dir=ssh_dir,
        ),
        nl=False,
    )
