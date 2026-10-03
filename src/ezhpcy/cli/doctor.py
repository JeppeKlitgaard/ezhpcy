from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from shutil import which

from rich.markup import escape
from rich.text import Text

from ezhpcy.cli._errors import rich_text, sentence
from ezhpcy.config import Config, ProfilePasswordSourceError, config
from ezhpcy.console import console, error_console
from ezhpcy.permissions import FilePermissionError
from ezhpcy.tunnel.ssh_config import (
    check_host_resolution,
    include_directive,
    profile_hosts,
    write_profiles_config,
)

_INCLUDE_HINT = (
    "If ~/.ssh/config does not include EzHPCy's SSH profiles yet, copy the two "
    "highlighted lines below to its top, before any Host or Match block:"
)


def echo_include_directive(*, err: bool = False) -> None:
    """Print the Include lines followed by a blank line, ready to copy verbatim."""
    # Unindented and unwrapped, even for long paths; the colour marks what to
    # copy and is dropped when output is not a terminal.
    (error_console if err else console).print(
        Text(include_directive() + "\n", style="bold cyan"), soft_wrap=True
    )


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


_STATUS_STYLES = {
    Status.OK: "bold green",
    Status.WARN: "bold yellow",
    Status.FAIL: "bold red",
}


@dataclass(frozen=True)
class Check:
    """The outcome of one doctor check; `message` and a `str` hint are Rich markup."""

    status: Status
    message: str
    hint: str | Text | None = None
    show_include: bool = False


def _check_profile(source: Config, name: str) -> Check:
    try:
        connection = source.resolve_profile(name).connection
    except ProfilePasswordSourceError as error:
        return Check(
            Status.FAIL,
            f"Profile {name} is invalid",
            hint=rich_text(sentence(error.message)),
        )

    password_file = connection.password_file
    if password_file is not None and not password_file.is_file():
        return Check(
            Status.FAIL,
            f"Profile {name} is invalid: connection.password_file "
            f"{escape(str(password_file))} does not exist",
        )
    if connection.user is None or connection.host is None:
        return Check(Status.OK, f"Profile {name} is valid")
    return Check(
        Status.OK,
        f"Profile {name} is valid: {escape(connection.user)}@{connection.host}",
    )


def check_config(source: Config = config) -> Iterator[Check]:
    """Check that the configuration file exists and every profile resolves."""
    config_file = source.local_file.config_file
    if not config_file.is_file():
        yield Check(
            Status.FAIL,
            f"Config file not found: {escape(str(config_file))}",
            hint="Run `ezhpcy config load <preset>` to start from a preset, "
            "or `ezhpcy config edit` to write one yourself.",
        )
        return
    yield Check(Status.OK, f"Config file found: {escape(str(config_file))}")

    if not source.profile:
        yield Check(
            Status.WARN,
            "No profiles are configured",
            hint="Add a [profile.NAME] table with `ezhpcy config edit`, or "
            "load a preset with `ezhpcy config load <preset>`.",
        )
        return

    for name in source.profile:
        yield _check_profile(source, name)


def check_ssh_config() -> Iterator[Check]:
    """Regenerate the profile hosts and check that OpenSSH resolves them."""
    ssh = which("ssh")
    if ssh is None:
        yield Check(
            Status.WARN,
            "SSH client not found on PATH, so `ssh <profile>` cannot be checked",
            hint="Install an OpenSSH client to connect to tunnels with `ssh`.",
        )
    else:
        yield Check(Status.OK, f"SSH client found: {escape(ssh)}")

    hosts = profile_hosts()
    try:
        write_profiles_config(hosts)
    except (OSError, FilePermissionError) as error:
        yield Check(
            Status.FAIL,
            f"Could not write SSH hosts: {escape(str(error))}",
        )
        return

    if not hosts:
        yield Check(
            Status.WARN,
            "No profile has both a user and a host, so the SSH setup cannot be checked",
            hint=_INCLUDE_HINT,
            show_include=True,
        )
        return
    if ssh is None:
        return

    for host in hosts:
        problem = check_host_resolution(host)
        if problem is None:
            yield Check(Status.OK, f"`ssh {host.alias}` goes through EzHPCy")
        else:
            yield Check(
                Status.FAIL,
                f"`ssh {host.alias}` bypasses EzHPCy: {escape(problem)}",
                hint=_INCLUDE_HINT,
                show_include=True,
            )


def _print_check(check: Check, printed_hints: set[str]) -> None:
    style = _STATUS_STYLES[check.status]
    console.print(f"  [{style}]{check.status:<4}[/]  {check.message}")
    # Several checks share a remedy; repeating it adds noise, not information.
    hint = Text.from_markup(check.hint) if isinstance(check.hint, str) else check.hint
    if hint is not None and hint.plain not in printed_hints:
        printed_hints.add(hint.plain)
        console.print(Text("        ") + hint, style="dim")
        if check.show_include:
            console.print()
            echo_include_directive()


def doctor_cmd() -> None:
    """Check the configuration and that OpenSSH picks up the profile hosts."""
    sections = {
        "Configuration": check_config,
        "SSH (lets `ssh <profile>` reach tunnels)": check_ssh_config,
    }
    printed_hints: set[str] = set()
    warnings = 0
    for index, (title, run_checks) in enumerate(sections.items()):
        if index > 0:
            console.print()
        console.print(f"[bold]{title}[/bold]")
        # Checks run lazily, so later checks never act on a broken earlier step.
        for check in run_checks():
            _print_check(check, printed_hints)
            if check.status is Status.FAIL:
                console.print()
                console.print(
                    "[bold red]Stopped at the first failure[/]; fix it and run "
                    "`ezhpcy doctor` again."
                )
                raise SystemExit(1)
            warnings += check.status is Status.WARN

    console.print()
    if warnings:
        console.print(f"[bold yellow]No failures[/], {warnings} warning(s).")
    else:
        console.print("[bold green]All checks passed.[/]")
