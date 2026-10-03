lazy import os
lazy from pathlib import Path

lazy import keyring
lazy from keyring.errors import KeyringError

lazy from ezhpcy.cli._errors import CliUsageError
lazy from ezhpcy.cli._options import KEYRING_SERVICE_NAME


def _without_trailing_line_endings(value: str) -> str:
    """Remove line endings commonly added by secret files and pipes."""
    return value.rstrip("\r\n")


def _read_password_file(path: Path, *, source: str) -> str:
    try:
        return _without_trailing_line_endings(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as error:
        raise CliUsageError(
            t"Could not read the file: {error}.",
            param_hint=source,
            value=path,
        ) from error


def _read_password_fd(fd: int, *, source: str) -> str:
    try:
        # Read through a duplicate so EzHPCy never closes a descriptor owned by
        # its caller. The duplicate intentionally shares the original offset.
        with os.fdopen(os.dup(fd), encoding="utf-8") as password_stream:
            return _without_trailing_line_endings(password_stream.read())
    except (OSError, UnicodeError) as error:
        raise CliUsageError(
            t"Could not read the file descriptor: {error}.",
            param_hint=source,
            value=fd,
        ) from error


def _read_password_keyring(*, user: str, host: str) -> str:
    account = f"{user}@{host}"
    try:
        password = keyring.get_password(KEYRING_SERVICE_NAME, account)
    except KeyringError as error:
        raise CliUsageError(
            t"Could not read the password from the system keyring: {error}."
        ) from error

    if password is None:
        raise CliUsageError(
            t"No password was found in the system keyring for service "
            t"{KEYRING_SERVICE_NAME:name} and account {account:name}. "
            t"Store one with {'ezhpcy keyring set':suggestion}."
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
    config_prefix: str = "connection",
) -> str | None:
    """Resolve an explicit password source, then the profile's source.

    `config_prefix` is where the profile's settings are, for error messages.
    """
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
        raise CliUsageError(
            t"Password source options are mutually exclusive: {explicit_sources:name}."
        )

    if password is not None:
        return password
    if password_file is not None:
        return _read_password_file(password_file, source="--password-file")
    if password_fd is not None:
        return _read_password_fd(password_fd, source="--password-fd")
    if password_keyring:
        return _read_password_keyring(user=user, host=host)

    # At most one of password source is set since `Config.resolve_profile`
    # rejects a profile that sets several.
    # Thus no need to check again.

    if config_password_file is not None:
        return _read_password_file(
            config_password_file, source=f"{config_prefix}.password_file"
        )
    if config_password_fd is not None:
        return _read_password_fd(
            config_password_fd, source=f"{config_prefix}.password_fd"
        )
    if config_password_keyring:
        return _read_password_keyring(user=user, host=host)
    return None
