from dataclasses import dataclass
from enum import StrEnum
lazy from collections.abc import Iterator
lazy from shutil import which
lazy from string.templatelib import Template

lazy from rich.text import Text

from ezhpcy.config import Config, ProfilePasswordSourceError, config
lazy from ezhpcy.cli._errors import sentence
lazy from ezhpcy.cli._output import rich_text
lazy from ezhpcy.console import console
lazy from ezhpcy.permissions import FilePermissionError
lazy from ezhpcy.tunnel.ssh_config import (
    check_host_resolution,
    echo_include_directive,
    profile_hosts,
    write_profiles_config,
)

_INCLUDE_HINT = (
    t"If {'~/.ssh/config':path} does not include EzHPCy's SSH profiles yet, copy "
    t"the two highlighted lines below to its top, before any Host or Match block:"
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
    """The outcome of one doctor check."""

    status: Status
    message: Template
    hint: Template | None = None
    show_include: bool = False


def _check_profile(source: Config, name: str) -> Check:
    try:
        connection = source.resolve_profile(name).connection
    except ProfilePasswordSourceError as error:
        return Check(
            Status.FAIL,
            t"Profile {name:name} is invalid",
            hint=sentence(error.message),
        )

    password_file = connection.password_file
    if password_file is not None and not password_file.is_file():
        return Check(
            Status.FAIL,
            t"Profile {name:name} is invalid: {'connection.password_file':name} "
            t"{password_file:path} does not exist",
        )
    if connection.user is None or connection.host is None:
        return Check(Status.OK, t"Profile {name:name} is valid")
    return Check(
        Status.OK,
        t"Profile {name:name} is valid: {connection.user}@{connection.host}",
    )


def check_config(source: Config = config) -> Iterator[Check]:
    """Check that the configuration file exists and every profile resolves."""
    config_file = source.local_file.config_file
    if not config_file.is_file():
        yield Check(
            Status.FAIL,
            t"Config file not found: {config_file:path}",
            hint=t"Run {'ezhpcy config load <preset>':suggestion} to start from a "
            t"preset, or {'ezhpcy config edit':suggestion} to write one yourself.",
        )
        return
    yield Check(Status.OK, t"Config file found: {config_file:path}")

    if not source.profile:
        yield Check(
            Status.WARN,
            t"No profiles are configured",
            hint=t"Add a {'[profile.NAME]':suggestion} table with "
            t"{'ezhpcy config edit':suggestion}, or load a preset with "
            t"{'ezhpcy config load <preset>':suggestion}.",
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
            t"SSH client not found on PATH, so {'ssh <profile>':suggestion} "
            t"cannot be checked",
            hint=t"Install an OpenSSH client to connect to tunnels with "
            t"{'ssh':suggestion}.",
        )
    else:
        yield Check(Status.OK, t"SSH client found: {ssh:path}")

    hosts = profile_hosts()
    try:
        write_profiles_config(hosts)
    except (OSError, FilePermissionError) as error:
        yield Check(Status.FAIL, t"Could not write SSH hosts: {error}")
        return

    if not hosts:
        yield Check(
            Status.WARN,
            t"No profile has both a user and a host, so the SSH setup cannot be "
            t"checked",
            hint=_INCLUDE_HINT,
            show_include=True,
        )
        return
    if ssh is None:
        return

    for host in hosts:
        problem = check_host_resolution(host)
        if problem is None:
            yield Check(
                Status.OK, t"{f'ssh {host.alias}':suggestion} goes through EzHPCy"
            )
        else:
            yield Check(
                Status.FAIL,
                t"{f'ssh {host.alias}':suggestion} bypasses EzHPCy: {problem}",
                hint=_INCLUDE_HINT,
                show_include=True,
            )


def _print_check(check: Check, printed_hints: set[str]) -> None:
    status = Text(f"{check.status:<4}", style=_STATUS_STYLES[check.status])
    console.print(Text("  ") + status + Text("  ") + rich_text(check.message))
    if check.hint is None:
        return
    # Several checks share a remedy; repeating it adds noise, not information.
    hint = rich_text(check.hint)
    if hint.plain not in printed_hints:
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
        console.print(Text(title, style="bold"))
        # Checks run lazily, so later checks never act on a broken earlier step.
        for check in run_checks():
            _print_check(check, printed_hints)
            if check.status is Status.FAIL:
                console.print()
                console.print(
                    Text("Stopped at the first failure", style="bold red")
                    + rich_text(t"; fix it and run {'ezhpcy doctor':suggestion} again.")
                )
                raise SystemExit(1)
            warnings += check.status is Status.WARN

    console.print()
    if warnings:
        console.print(
            Text("No failures", style="bold yellow") + Text(f", {warnings} warning(s).")
        )
    else:
        console.print(Text("All checks passed.", style="bold green"))
