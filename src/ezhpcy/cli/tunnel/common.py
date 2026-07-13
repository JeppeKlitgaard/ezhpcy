import os
from pathlib import Path
from typing import Annotated

import keyring
import typer
from keyring.errors import KeyringError

from ezhpcy.cli.utils.bad_parameter import RichBadParameter
from ezhpcy.cli.utils.options_group import attach_hook
from ezhpcy.cli.utils.resolve import resolve_forbidden_none
from ezhpcy.config import ConnectionInfo, config
from ezhpcy.console import console
from ezhpcy.detect import HostType, get_host_type

KEYRING_SERVICE_NAME = "ezhpcy"
PASSWORD_ENV_VAR = "EZHPCY_PASSWORD"

UserOpt = Annotated[
    str | None, typer.Option("--user", "-u", help="Username for the login node.")
]
PasswordOpt = Annotated[
    str | None,
    typer.Option(
        "--password",
        "-p",
        help="Password for the login node. Defaults to config.",
    ),
]
PasswordEnvOpt = Annotated[
    bool,
    typer.Option(
        "--password-env",
        help=f"Read the password from the {PASSWORD_ENV_VAR} environment variable.",
    ),
]
PasswordFileOpt = Annotated[
    Path | None,
    typer.Option(
        "--password-file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="Read the password from a UTF-8 file.",
    ),
]
PasswordFdOpt = Annotated[
    int | None,
    typer.Option(
        "--password-fd",
        min=0,
        help="Read the password from an already-open file descriptor.",
    ),
]
PasswordKeyringOpt = Annotated[
    bool,
    typer.Option(
        "--password-keyring",
        help=(
            "Read the password from the system keyring service "
            f"'{KEYRING_SERVICE_NAME}' under USER@HOST."
        ),
    ),
]
HostOpt = Annotated[
    str | None, typer.Option("--host", "-h", help="Login node address.")
]


def _without_trailing_line_endings(value: str) -> str:
    """Remove line endings commonly added by secret files and pipes."""
    return value.rstrip("\r\n")


def _read_password_file(path: Path) -> str:
    try:
        return _without_trailing_line_endings(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as error:
        raise RichBadParameter(
            f"could not read password file {path}: {error}",
            param_hint="--password-file",
        ) from error


def _read_password_fd(fd: int) -> str:
    try:
        # Read through a duplicate so ezhpcy never closes a descriptor owned by
        # its caller. The duplicate intentionally shares the original offset.
        with os.fdopen(os.dup(fd), encoding="utf-8") as password_stream:
            return _without_trailing_line_endings(password_stream.read())
    except (OSError, UnicodeError) as error:
        raise RichBadParameter(
            f"could not read password file descriptor {fd}: {error}",
            param_hint="--password-fd",
        ) from error


def _read_password_keyring(*, user: str, host: str) -> str:
    account = f"{user}@{host}"
    try:
        password = keyring.get_password(KEYRING_SERVICE_NAME, account)
    except KeyringError as error:
        raise RichBadParameter(
            f"could not read password from the system keyring: {error}",
            param_hint="--password-keyring",
        ) from error

    if password is None:
        raise RichBadParameter(
            "no password was found in the system keyring for "
            f"service '{KEYRING_SERVICE_NAME}' and account '{account}'",
            param_hint="--password-keyring",
        )
    return password


def resolve_password(
    *,
    password: str | None,
    password_env: bool,
    password_file: Path | None,
    password_fd: int | None,
    password_keyring: bool,
    user: str,
    host: str,
) -> str | None:
    """Resolve one explicit password source, followed by environment and config."""
    explicit_sources = [
        name
        for name, selected in (
            ("--password", password is not None),
            ("--password-env", password_env),
            ("--password-file", password_file is not None),
            ("--password-fd", password_fd is not None),
            ("--password-keyring", password_keyring),
        )
        if selected
    ]
    if len(explicit_sources) > 1:
        raise RichBadParameter(
            "password source options are mutually exclusive: "
            + ", ".join(explicit_sources)
        )

    if password is not None:
        return password
    if password_env:
        try:
            return os.environ[PASSWORD_ENV_VAR]
        except KeyError:
            raise RichBadParameter(
                f"environment variable {PASSWORD_ENV_VAR} is not set",
                param_hint="--password-env",
            ) from None
    if password_file is not None:
        return _read_password_file(password_file)
    if password_fd is not None:
        return _read_password_fd(password_fd)
    if password_keyring:
        return _read_password_keyring(user=user, host=host)

    return config.connection.password


def connection_info_from_options(
    *,
    user: UserOpt = config.connection.user,
    password: PasswordOpt = None,
    password_env: PasswordEnvOpt = False,
    password_file: PasswordFileOpt = None,
    password_fd: PasswordFdOpt = None,
    password_keyring: PasswordKeyringOpt = False,
    host: HostOpt = config.connection.host,
) -> ConnectionInfo:
    user = resolve_forbidden_none(
        cli_value=user,
        config_value=config.connection.user,
        name="user",
        cli_param="--user",
        config_param="connection.user",
    )
    resolved_host = str(host or config.connection.host)
    resolved_password = resolve_password(
        password=password,
        password_env=password_env,
        password_file=password_file,
        password_fd=password_fd,
        password_keyring=password_keyring,
        user=user,
        host=resolved_host,
    )
    return ConnectionInfo(
        user=user,
        password=resolved_password,
        host=resolved_host,
    )


with_connection_options = attach_hook(
    connection_info_from_options, hook_output_kwarg="conn_info"
)


def local_machine_or_fail() -> None:
    """
    Fails the command if the current HostType is not OTHER.
    """
    host_type = get_host_type()
    if host_type != HostType.OTHER:
        console.print(
            f"[bold red]ERROR[/bold red]: This command should be run on your local machine (detected: [bold blue]{host_type.value}[/bold blue])."
        )
        raise typer.Exit(code=1)
