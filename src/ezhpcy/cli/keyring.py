lazy import keyring
lazy from keyring.errors import KeyringError
lazy from rich.prompt import Prompt

from ezhpcy.cli._options import KEYRING_SERVICE_NAME, ConnectionOptions
lazy from ezhpcy.cli._resolve import direct_connection_from_cli
lazy from ezhpcy.console import console


# ruff: ignore[B008]  # See the comment above the option dataclasses in _options.py
def set_cmd(*, connection_options: ConnectionOptions = ConnectionOptions()) -> None:
    """Store the login-node password in the system keyring."""
    connection = direct_connection_from_cli(connection_options)
    account = f"{connection.user}@{connection.host}"
    password = connection.password
    if password is None:
        password = Prompt.ask(
            f"Enter password for {account}",
            password=True,
            console=console,
        )

    try:
        keyring.set_password(KEYRING_SERVICE_NAME, account, password)
    except KeyringError as error:
        console.print(
            "[bold red]Error[/bold red]: Could not store the password in the "
            f"system keyring: {error}"
        )
        raise SystemExit(1) from error

    console.print(f"Stored password in the system keyring for {account}.")
