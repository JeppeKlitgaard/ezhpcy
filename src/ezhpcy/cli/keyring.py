lazy import keyring
lazy from keyring.errors import KeyringError, PasswordDeleteError
lazy from rich.prompt import Prompt

from ezhpcy.cli._options import (
    KEYRING_SERVICE_NAME,
    ConnectionOptions,
    HostOpt,
    UserOpt,
)
lazy from ezhpcy.cli._resolve import direct_login_from_cli
lazy from ezhpcy.console import console, error_console
lazy from ezhpcy.messages import print_error, rich_text


# ruff: ignore[B008]  # See the comment above the option dataclasses in _options.py
def set_cmd(*, connection_options: ConnectionOptions = ConnectionOptions()) -> None:
    """Store the login-node password in the system keyring."""
    login, given = direct_login_from_cli(connection_options)
    account = f"{login.user}@{login.host}"
    if given is not None:
        password = given.get_secret_value()
    else:
        password = Prompt.ask(
            rich_text(t"Enter password for {account}"),
            password=True,
            console=error_console,
        )

    try:
        keyring.set_password(KEYRING_SERVICE_NAME, account, password)
    except KeyringError as error:
        print_error(t"Could not store the password in the system keyring: {error}")
        raise SystemExit(1) from error

    console.print(rich_text(t"Stored password in the system keyring for {account}."))


def unset_cmd(*, host: HostOpt = None, user: UserOpt = None) -> None:
    """Remove the login-node password from the system keyring."""
    # Validates the user and host like `set`; without password options it reads none.
    login, _ = direct_login_from_cli(ConnectionOptions(host=host, user=user))
    account = f"{login.user}@{login.host}"

    try:
        keyring.delete_password(KEYRING_SERVICE_NAME, account)
    # Backends raise it when the account has no password.
    except PasswordDeleteError as error:
        print_error(t"No password is stored in the system keyring for {account}.")
        raise SystemExit(1) from error
    except KeyringError as error:
        print_error(t"Could not remove the password from the system keyring: {error}")
        raise SystemExit(1) from error

    console.print(rich_text(t"Removed password from the system keyring for {account}."))
