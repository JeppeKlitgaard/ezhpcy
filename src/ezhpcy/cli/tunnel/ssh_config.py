import sys
from pathlib import Path
from typing import Annotated

import typer

from ezhpcy.cli.tunnel.common import HostOpt, ProfileOpt, UserOpt
from ezhpcy.cli.utils.bad_parameter import RichBadParameter
from ezhpcy.cli.utils.resolve import resolve_forbidden_none
from ezhpcy.config import config
from ezhpcy.constants import (
    WORKER_CLIENT_KEY_NAME,
    WORKER_HOST_ALIAS,
)
from ezhpcy.utils import local_machine_id


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
    profile_name: str,
    alias: str = WORKER_HOST_ALIAS,
    ssh_dir: Path,
    python_executable: Path = Path(sys.executable),
) -> str:
    """Render the stable worker alias consumed by OpenSSH and VS Code."""
    user = _single_token(user, name="--user")
    profile_name = _single_token(profile_name, name="--profile")
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
            f"{_config_path(python_executable)} -m ezhpcy.cli.entry proxy "
            f"--profile {profile_name}",
            "",
        )
    )


def ssh_config_cmd(
    profile: ProfileOpt = None,
    user: UserOpt = None,
    host: HostOpt = None,
    alias: Annotated[
        str,
        typer.Option(
            "--alias",
            help="Stable Host alias to expose to OpenSSH and VS Code.",
        ),
    ] = WORKER_HOST_ALIAS,
) -> None:
    """Print an OpenSSH Host block for the broker-backed worker connection."""
    try:
        resolved_profile = config.resolve_profile(profile)
    except ValueError as error:
        raise RichBadParameter(str(error), param_hint="--profile") from error
    profile_name = profile or config.default_profile
    assert profile_name is not None
    resolved_user = resolve_forbidden_none(
        cli_value=user,
        config_value=resolved_profile.user,
        name="user",
        cli_param="--user",
        config_param=f"profile.{profile_name}.user",
    )
    resolved_host = resolve_forbidden_none(
        cli_value=host,
        config_value=(str(resolved_profile.host) if resolved_profile.host else None),
        name="host",
        cli_param="--host",
        config_param=f"profile.{profile_name}.host",
    )
    ssh_dir = config.local_file.ssh_dir(
        local_machine_id(), user=resolved_user, host=resolved_host
    )
    typer.echo(
        render_worker_ssh_config(
            user=resolved_user,
            profile_name=profile_name,
            alias=alias,
            ssh_dir=ssh_dir,
        ),
        nl=False,
    )
