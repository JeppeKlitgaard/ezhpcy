lazy import os
lazy from pathlib import Path

lazy import keyring
lazy from keyring.errors import KeyringError

from ezhpcy.logging import get_logger
lazy from ezhpcy.cli._errors import CliUsageError
lazy from ezhpcy.cli._options import KEYRING_SERVICE_NAME

logger = get_logger(__name__)


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


def _read_password_keyring(
    *, user: str, host: str, password_prompt: bool
) -> str | None:
    """Read the password from the keyring, or `None` to prompt for it instead."""
    account = f"{user}@{host}"
    try:
        password = keyring.get_password(KEYRING_SERVICE_NAME, account)
    except KeyringError as error:
        message = t"Could not read the password from the system keyring: {error}."
    else:
        if password is not None:
            return password
        message = (
            t"No password was found in the system keyring for service "
            t"{KEYRING_SERVICE_NAME:name} and account {account:name}. "
            t"Store one with {'ezhpcy keyring set':suggestion}."
        )

    if not password_prompt:
        raise CliUsageError(message)
    logger.warning(message + t" The password is asked for if needed.")
    return None


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
    password_prompt: bool = True,
    config_prefix: str = "connection",
) -> str | None:
    """Resolve the password from the first enabled source that has one.

    A source given on the command line replaces the configured ones, which are
    tried in order: `password_fd`, `password_file`, then `password_keyring`.
    The last resort is the prompt, if `password_prompt` allows it, which
    `connect_interactive` shows only when the server asks for a password. So a
    keyring without the password is passed over when prompting is allowed.

    `config_prefix` is where the profile's settings are, for error messages.
    """
    # At most one of these is set: Cyclopts rejects several
    # (the options' `PASSWORD_SOURCES` group).
    if password is not None:
        return password
    if password_file is not None:
        return _read_password_file(password_file, source="--password-file")
    if password_fd is not None:
        return _read_password_fd(password_fd, source="--password-fd")
    if password_keyring:
        return _read_password_keyring(
            user=user, host=host, password_prompt=password_prompt
        )

    if config_password_fd is not None:
        return _read_password_fd(
            config_password_fd, source=f"{config_prefix}.password_fd"
        )
    if config_password_file is not None:
        return _read_password_file(
            config_password_file, source=f"{config_prefix}.password_file"
        )
    if config_password_keyring:
        return _read_password_keyring(
            user=user, host=host, password_prompt=password_prompt
        )
    return None
