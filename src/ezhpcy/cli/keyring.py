import keyring
import typer
from cyclopts import App
from keyring.errors import KeyringError
from rich.prompt import Prompt

from ezhpcy.cli._options import KEYRING_SERVICE_NAME
from ezhpcy.cli._resolve import with_direct_connection_options
from ezhpcy.console import console
from ezhpcy.types import ConnectionInfo

keyring_app = App(
    name="keyring", help="Manage login-node passwords in the system keyring."
)


@with_direct_connection_options
def set_cmd(conn_info: ConnectionInfo) -> None:
    """Store the login-node password in the system keyring."""
    account = f"{conn_info.user}@{conn_info.host}"
    password = conn_info.password
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
        raise typer.Exit(code=1) from error

    console.print(f"Stored password in the system keyring for {account}.")


keyring_app.command(set_cmd, name="set", help="Store a login-node password.")
