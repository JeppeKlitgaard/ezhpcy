from dataclasses import dataclass
from enum import StrEnum
lazy import secrets
lazy from collections.abc import Iterator
lazy from shutil import which
lazy from string.templatelib import Template

lazy from rich.text import Text

from ezhpcy.config import Config, ProfilePasswordSourceError, config
lazy from ezhpcy.cli._errors import sentence
lazy from ezhpcy.console import console
lazy from ezhpcy.constants import LOOPBACK_HOST
lazy from ezhpcy.messages import rich_text
lazy from ezhpcy.permissions import FilePermissionError
lazy from ezhpcy.tunnel.lock import alias_locked
lazy from ezhpcy.tunnel.ssh_config import (
    PROBE_HOST_NAME,
    echo_include_directive,
    probe_hosts,
    prune_stale_active_configs,
    resolve_alias,
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


def _check_profile(source: Config, profile_name: str) -> Check:
    try:
        connection = source.resolve_profile(profile_name).connection
    except ProfilePasswordSourceError as error:
        return Check(
            Status.FAIL,
            t"Profile {profile_name:name} is invalid",
            hint=sentence(error.message),
        )

    password_file = connection.password_file
    if password_file is not None and not password_file.is_file():
        return Check(
            Status.FAIL,
            t"Profile {profile_name:name} is invalid: {'connection.password_file':name} "
            t"{password_file:path} does not exist",
        )
    if connection.user is None or connection.host is None:
        return Check(Status.OK, t"Profile {profile_name:name} is valid")
    return Check(
        Status.OK,
        t"Profile {profile_name:name} is valid: {connection.user}@{connection.host}",
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


def _profile_aliases(source: Config) -> list[str]:
    """The profiles a tunnel can start from as they are: with a user and a host."""
    aliases = []
    for name in source.profile:
        connection = source.resolve_profile(
            name, validate_password_source=False
        ).connection
        if connection.user is not None and connection.host is not None:
            aliases.append(name)
    return aliases


def _resolves_to_ezhpcy(alias: str) -> str | None:
    """
    `None` if `ssh <alias>` gets EzHPCy's block for it, a running tunnel's or
    the probe's; otherwise what went wrong.
    """
    resolved = resolve_alias(alias)
    if resolved is None:
        return "`ssh` is no longer on PATH"
    if isinstance(resolved, str):
        return resolved
    hostname = resolved.get("hostname", [""])[0]
    if hostname == PROBE_HOST_NAME:
        return None
    if hostname == LOOPBACK_HOST and alias_locked(alias):
        return None
    return f"OpenSSH resolves it to {hostname}"


def check_ssh_config(source: Config = config) -> Iterator[Check]:
    """
    Remove stale Host blocks, then check that OpenSSH gives each profile's alias
    EzHPCy's Host block, as it does for a running tunnel.
    """
    ssh = which("ssh")
    if ssh is None:
        yield Check(
            Status.WARN,
            t"SSH client not found on PATH, so {'ssh <alias>':suggestion} "
            t"cannot be checked",
            hint=t"Install an OpenSSH client to connect to tunnels with "
            t"{'ssh':suggestion}.",
        )
    else:
        yield Check(Status.OK, t"SSH client found: {ssh:path}")

    try:
        removed = prune_stale_active_configs()
    except OSError as error:
        yield Check(Status.FAIL, t"Could not remove stale SSH hosts: {error}")
        return
    if removed:
        names = ", ".join(path.name for path in removed)
        yield Check(Status.OK, t"Removed stale SSH configuration: {names}")
    if ssh is None:
        return

    aliases = _profile_aliases(source)
    # Without profiles, a made-up alias still shows whether the Include works.
    probed = aliases or [f"ezhpcy-doctor-{secrets.token_hex(4)}"]
    problems: dict[str, str | None] = {}
    try:
        with probe_hosts(probed):
            problems = {alias: _resolves_to_ezhpcy(alias) for alias in probed}
    except (OSError, FilePermissionError) as error:
        yield Check(Status.FAIL, t"Could not write the SSH probe: {error}")
        return

    if not aliases:
        [problem] = problems.values()
        if problem is None:
            yield Check(Status.OK, t"OpenSSH reads EzHPCy's SSH configuration")
        else:
            yield Check(
                Status.FAIL,
                t"OpenSSH does not read EzHPCy's SSH configuration: {problem}",
                hint=_INCLUDE_HINT,
                show_include=True,
            )
        return
    for alias, problem in problems.items():
        if problem is None:
            yield Check(Status.OK, t"{f'ssh {alias}':suggestion} goes through EzHPCy")
        else:
            yield Check(
                Status.FAIL,
                t"{f'ssh {alias}':suggestion} bypasses EzHPCy: {problem}",
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
    """Check the configuration and that OpenSSH picks up EzHPCy's Host blocks."""
    sections = {
        "Configuration": check_config,
        "SSH (lets `ssh <alias>` reach running tunnels)": check_ssh_config,
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
