lazy import keyring
lazy from keyring.errors import KeyringError
lazy from rich.prompt import Prompt

from ezhpcy.cli._options import KEYRING_SERVICE_NAME, ConnectionOptions
lazy from ezhpcy.cli._resolve import direct_connection_from_cli
lazy from ezhpcy.console import console, error_console
lazy from ezhpcy.messages import print_error, rich_text


# ruff: ignore[B008]  # See the comment above the option dataclasses in _options.py
def set_cmd(*, connection_options: ConnectionOptions = ConnectionOptions()) -> None:
    """Store the login-node password in the system keyring."""
    conn_info = direct_connection_from_cli(connection_options)
    account = f"{conn_info.user}@{conn_info.host}"
    password = conn_info.password
    if password is None:
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
