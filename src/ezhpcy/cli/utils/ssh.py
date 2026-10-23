"""
The CLI's side of connecting to the login node (see `ezhpcy.login`): its Rich
prompts for a password and an unknown host key, and connecting for a command.

The prompts block the event loop while they wait, which is harmless while
connecting (see `ezhpcy.cli.utils.prompt.ask`).
"""

lazy from pydantic import SecretStr
lazy from rich.prompt import Confirm, Prompt

from ezhpcy.logging import get_logger
lazy from ezhpcy import login as login_module
lazy from ezhpcy.cli._options import ConnectionOptions
lazy from ezhpcy.cli._resolve import login_usage_error
lazy from ezhpcy.cli.utils.prompt import ask
lazy from ezhpcy.console import error_console
lazy from ezhpcy.login import PASSWORD_ATTEMPTS, ConnectionFailed, Prompts
lazy from ezhpcy.messages import print_error, print_warning, rich_text
lazy from ezhpcy.ssh import RemoteHost
lazy from ezhpcy.types import ConfigError, Login

logger = get_logger(__name__)


def cli_prompts() -> Prompts:
    """
    Prompts at the terminal, for one connection: a hidden password, saying from
    the second time on that the last one was wrong, and whether to trust a host
    key, by its type and fingerprint. Ctrl+C at a prompt raises
    `KeyboardInterrupt`.
    """
    asked = 0

    async def ask_password(user: str, host: str) -> str:
        nonlocal asked
        if asked:
            print_error(
                t"Invalid password. Try again or press Ctrl+C to abort.",
                label=f"Error [{asked}/{PASSWORD_ATTEMPTS}]",
            )
        asked += 1
        return ask(
            Prompt.ask,
            rich_text(t"Enter password for {user}@{host}"),
            password=True,
            console=error_console,
        )

    async def accept_host_key(
        host: str, _port: int, key_type: str, fingerprint: str
    ) -> bool:
        print_warning(t"The host key for {host:name} is not in any known hosts file.")
        error_console.print(rich_text(t"Key type: {key_type}"))
        error_console.print(rich_text(t"Key fingerprint: {fingerprint}"))
        return ask(
            Confirm.ask,
            "Do you want to accept this host key?: ",
            console=error_console,
            default=False,
            case_sensitive=False,
        )

    return Prompts(ask_password=ask_password, accept_host_key=accept_host_key)


async def connect_or_exit(
    login: Login,
    password: SecretStr | None,
    options: ConnectionOptions,
    *,
    profile_name: str | None,
) -> RemoteHost:
    """
    Connect for a command, with the CLI's prompts. If it can't connect, print
    `Error: Could not connect to user@host: …` and exit with status 1; a password
    source that can't be read is a usage error.
    """
    try:
        return await login_module.connect(
            login, prompts=cli_prompts(), password=password
        )
    except ConfigError as error:
        raise login_usage_error(error, options, profile_name=profile_name) from error
    except ConnectionFailed as error:
        logger.debug("Could not connect.", exc_info=True)
        target = f"{login.user}@{login.host}"
        print_error(t"Could not connect to {target:name}: {error}")
        raise SystemExit(1) from error
