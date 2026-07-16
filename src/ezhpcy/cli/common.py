import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import keyring
import typer
from keyring.errors import KeyringError

from ezhpcy.cli.utils.bad_parameter import RichBadParameter
from ezhpcy.cli.utils.options_group import attach_hook
from ezhpcy.cli.utils.resolve import resolve_forbidden_none
from ezhpcy.config import ConnectionInfo, ProfilePasswordSourceError, config
from ezhpcy.constants import PACKAGE_NAME
from ezhpcy.types import ResolvedConfig

KEYRING_SERVICE_NAME = PACKAGE_NAME

HOST_ENV_VAR = "EZHPCY_HOST"
USER_ENV_VAR = "EZHPCY_USER"
PROFILE_ENV_VAR = "EZHPCY_PROFILE"
PASSWORD_ENV_VAR = "EZHPCY_PASSWORD"
PASSWORD_FILE_ENV_VAR = "EZHPCY_PASSWORD_FILE"
PASSWORD_FD_ENV_VAR = "EZHPCY_PASSWORD_FD"
PASSWORD_KEYRING_ENV_VAR = "EZHPCY_PASSWORD_KEYRING"

HostOpt = Annotated[
    str | None,
    typer.Option("--host", "-h", help="Login node address.", envvar=HOST_ENV_VAR),
]
UserOpt = Annotated[
    str | None,
    typer.Option(
        "--user", "-u", help="Username for the login node.", envvar=USER_ENV_VAR
    ),
]
ProfileOpt = Annotated[
    str | None,
    typer.Option(
        "--profile",
        "-p",
        help="Configured EzHPCy profile to use.",
        envvar=PROFILE_ENV_VAR,
    ),
]
ProfileArg = Annotated[
    str,
    typer.Argument(
        help="Configured EzHPCy profile to use.",
        envvar=PROFILE_ENV_VAR,
    ),
]
PasswordOpt = Annotated[
    str | None,
    typer.Option(
        "--password",
        help=(
            "Password for the login node. "
            "Note: Specifying this is potentially a security risk."
        ),
        envvar=PASSWORD_ENV_VAR,
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
        envvar=PASSWORD_FILE_ENV_VAR,
    ),
]
PasswordFdOpt = Annotated[
    int | None,
    typer.Option(
        "--password-fd",
        min=0,
        help="Read the password from an already-open file descriptor.",
        envvar=PASSWORD_FD_ENV_VAR,
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
        envvar=PASSWORD_KEYRING_ENV_VAR,
    ),
]


@dataclass(frozen=True)
class ProfileContext:
    name: str
    profile: ResolvedConfig
    configured_fields: frozenset[str] = frozenset()
    directly_configured_fields: frozenset[str] = frozenset()

    @property
    def connection(self) -> ConnectionInfo:
        return ConnectionInfo(
            user=self.profile.user,
            password=self.profile.password,
            host=self.profile.host,
        )


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
        # Read through a duplicate so EzHPCy never closes a descriptor owned by
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
    password_file: Path | None,
    password_fd: int | None,
    password_keyring: bool,
    user: str,
    host: str,
    config_password_file: Path | None = None,
    config_password_fd: int | None = None,
    config_password_keyring: bool = False,
) -> str | None:
    """Resolve an explicit password source, then the profile's source."""
    explicit_sources = [
        name
        for name, selected in (
            ("--password", password is not None),
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
    if password_file is not None:
        return _read_password_file(password_file)
    if password_fd is not None:
        return _read_password_fd(password_fd)
    if password_keyring:
        return _read_password_keyring(user=user, host=host)

    configured_sources = [
        name
        for name, selected in (
            ("password_file", config_password_file is not None),
            ("password_fd", config_password_fd is not None),
            ("password_keyring", config_password_keyring),
        )
        if selected
    ]
    if len(configured_sources) > 1:
        raise RichBadParameter(
            "profile password source options are mutually exclusive: "
            + ", ".join(configured_sources)
        )
    if config_password_file is not None:
        return _read_password_file(config_password_file)
    if config_password_fd is not None:
        return _read_password_fd(config_password_fd)
    if config_password_keyring:
        return _read_password_keyring(user=user, host=host)
    return None


def profile_context_from_options(
    *,
    profile: ProfileOpt = None,
    user: UserOpt = None,
    password: PasswordOpt = None,
    password_file: PasswordFileOpt = None,
    password_fd: PasswordFdOpt = None,
    password_keyring: PasswordKeyringOpt = False,
    host: HostOpt = None,
) -> ProfileContext:
    try:
        resolved_profile = config.resolve_profile(profile)
    except ProfilePasswordSourceError as error:
        raise RichBadParameter(error.rich_message()) from error
    except ValueError as error:
        raise RichBadParameter(str(error), param_hint="--profile") from error
    assert profile is not None

    user = resolve_forbidden_none(
        cli_value=user,
        config_value=resolved_profile.user,
        name="user",
        cli_param="--user",
        config_param=f"profile.{profile}.user",
    )
    resolved_host = resolve_forbidden_none(
        cli_value=host,
        config_value=(str(resolved_profile.host) if resolved_profile.host else None),
        name="host",
        cli_param="--host",
        config_param=f"profile.{profile}.host",
    )
    resolved_password = resolve_password(
        password=password,
        password_file=password_file,
        password_fd=password_fd,
        password_keyring=password_keyring,
        user=user,
        host=resolved_host,
        config_password_file=resolved_profile.password_file,
        config_password_fd=resolved_profile.password_fd,
        config_password_keyring=resolved_profile.password_keyring,
    )
    resolved_config = ResolvedConfig.model_validate(
        {
            **resolved_profile.model_dump(),
            "user": user,
            "password": resolved_password,
            "host": resolved_host,
        }
    )
    return ProfileContext(
        name=profile,
        profile=resolved_config,
        configured_fields=frozenset(resolved_profile.model_fields_set),
        directly_configured_fields=frozenset(config.profile[profile].model_fields_set),
    )


with_profile_options = attach_hook(
    profile_context_from_options, hook_output_kwarg="profile_context"
)


def direct_connection_info_from_options(
    *,
    host: HostOpt = None,
    user: UserOpt = None,
    password: PasswordOpt = None,
    password_file: PasswordFileOpt = None,
    password_fd: PasswordFdOpt = None,
    password_keyring: PasswordKeyringOpt = False,
) -> ConnectionInfo:
    if user is None:
        raise RichBadParameter("user must be set via --user", param_hint="--user")
    if host is None:
        raise RichBadParameter("host must be set via --host", param_hint="--host")
    return ConnectionInfo(
        user=user,
        host=host,
        password=resolve_password(
            password=password,
            password_file=password_file,
            password_fd=password_fd,
            password_keyring=password_keyring,
            user=user,
            host=host,
        ),
    )


with_direct_connection_options = attach_hook(
    direct_connection_info_from_options, hook_output_kwarg="conn_info"
)
