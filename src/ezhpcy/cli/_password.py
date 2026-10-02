import os
from pathlib import Path

import keyring
from keyring.errors import KeyringError

from ezhpcy.cli._options import KEYRING_SERVICE_NAME
from ezhpcy.cli.utils.bad_parameter import RichBadParameter


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
