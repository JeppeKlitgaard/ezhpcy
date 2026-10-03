import typer
from rich.markup import escape

from ezhpcy.console import console
from ezhpcy.permissions import FilePermissionError
from ezhpcy.tunnel.ssh_config import (
    check_host_resolution,
    include_directive,
    profile_hosts,
    write_profiles_config,
)


def echo_include_directive(*, err: bool = False) -> None:
    """Print the Include lines followed by a blank line, ready to copy verbatim."""
    # Unindented and unwrapped (typer, not rich, which wraps long paths); the
    # colour marks what to copy and is dropped when output is not a TTY.
    typer.echo(typer.style(include_directive(), fg="cyan", bold=True) + "\n", err=err)


def ssh_config_cmd() -> None:
    """Regenerate the profile SSH hosts and show how OpenSSH picks them up."""
    hosts = profile_hosts()
    try:
        path = write_profiles_config(hosts)
    except (OSError, FilePermissionError) as error:
        console.print(
            f"[bold red]Could not write SSH configuration:[/] {escape(str(error))}"
        )
        raise typer.Exit(code=1) from error

    console.print(
        "Copy the two highlighted lines below to the top of "
        "[bold]~/.ssh/config[/bold], before any Host or Match block:\n"
    )
    echo_include_directive()

    if not hosts:
        console.print(
            "No profile defines both a user and a host; "
            f"wrote empty {escape(str(path))}."
        )
        return
    console.print(f"Profile hosts in [bold]{escape(str(path))}[/bold]:")
    for host in hosts:
        problem = check_host_resolution(host)
        if problem is None:
            console.print(f"  [bold green]ok[/]    {host.alias}")
        else:
            console.print(f"  [bold red]fail[/]  {host.alias}: {escape(problem)}")
