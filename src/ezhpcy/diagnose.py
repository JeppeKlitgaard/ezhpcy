"""
Whether EzHPCy is set up on this machine: its configuration, and that OpenSSH
picks up its Host blocks.
"""

from dataclasses import dataclass
from enum import StrEnum
lazy import secrets
lazy from collections.abc import Iterator
lazy from dataclasses import replace
lazy from shutil import which
lazy from string.templatelib import Template
lazy from typing import Literal

lazy from ezhpcy.config import Config, StoredPasswordError, unless_disabled
lazy from ezhpcy.permissions import FilePermissionError
lazy from ezhpcy.tunnel.ssh_config import (
    AliasProblem,
    probe_hosts,
    prune_stale_active_configs,
    resolution_problem,
)


class CheckStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class Check:
    """The outcome of one check, in one section of `diagnose`."""

    status: CheckStatus
    message: Template
    hint: Template | None = None
    # The lines to add to ~/.ssh/config, if they are the fix.
    include_lines: str | None = None
    section: Literal["configuration", "ssh"] = "configuration"


def diagnose(config: Config) -> Iterator[Check]:
    """
    Check that the configuration file exists, that its top-level `connection`
    table and every profile resolve, and that OpenSSH gives each profile's alias
    EzHPCy's Host block, as it does for a running tunnel. Each check says how it
    went, those that pass too.

    Checks run as they are read, so that stopping at a `FAIL` keeps later checks
    from acting on a broken earlier step. The SSH checks remove stale Host blocks,
    and write and remove a probe in EzHPCy's SSH configuration. Raises nothing for
    what it finds.
    """
    yield from _check_config(config)
    for check in _check_ssh_config(config):
        yield replace(check, section="ssh")


def _check_profile(source: Config, profile_name: str | None) -> Check:
    """Check a profile resolves, or with `None` the top-level `connection` table."""
    subject = (
        t"Top-level {'connection':name} table"
        if profile_name is None
        else t"Profile {profile_name:name}"
    )
    try:
        source.reject_stored_password(profile_name)
    except StoredPasswordError as error:
        return Check(
            CheckStatus.FAIL,
            subject + t" is invalid",
            hint=error.message + t".",
        )

    connection = source.resolve_profile(profile_name).connection
    password_file = unless_disabled(connection.password_file)
    # `resolve_password` reads a descriptor first, and then never the file.
    reads_file = unless_disabled(connection.password_fd) is None
    if reads_file and password_file is not None and not password_file.is_file():
        return Check(
            CheckStatus.FAIL,
            subject + t" is invalid: {'connection.password_file':name} "
            t"{password_file:path} does not exist",
        )
    if connection.user is None or connection.host is None:
        return Check(CheckStatus.OK, subject + t" is valid")
    return Check(
        CheckStatus.OK,
        subject + t" is valid: {connection.user}@{connection.host}",
    )


def _check_config(source: Config) -> Iterator[Check]:
    config_file = source.local_file.config_file
    if not config_file.is_file():
        yield Check(
            CheckStatus.FAIL,
            t"Config file not found: {config_file:path}",
            hint=t"Run {'ezhpcy config load <preset>':suggestion} to start from a "
            t"preset, or {'ezhpcy config edit':suggestion} to write one yourself.",
        )
        return
    yield Check(CheckStatus.OK, t"Config file found: {config_file:path}")

    if source.connection.model_fields_set:
        yield _check_profile(source, None)

    if not source.profile:
        yield Check(
            CheckStatus.WARN,
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
        connection = source.resolve_profile(name).connection
        if connection.user is not None and connection.host is not None:
            aliases.append(name)
    return aliases


def _check_ssh_config(source: Config) -> Iterator[Check]:
    ssh = which("ssh")
    if ssh is None:
        yield Check(
            CheckStatus.WARN,
            t"SSH client not found on PATH, so {'ssh <alias>':suggestion} "
            t"cannot be checked",
            hint=t"Install an OpenSSH client to connect to tunnels with "
            t"{'ssh':suggestion}.",
        )
    else:
        yield Check(CheckStatus.OK, t"SSH client found: {ssh:path}")

    try:
        removed = prune_stale_active_configs()
    except OSError as error:
        yield Check(CheckStatus.FAIL, t"Could not remove stale SSH hosts: {error}")
        return
    if removed:
        names = ", ".join(path.name for path in removed)
        yield Check(CheckStatus.OK, t"Removed stale SSH configuration: {names}")
    if ssh is None:
        return

    aliases = _profile_aliases(source)
    # Without profiles, a made-up alias still shows whether the Include works.
    probed = aliases or [f"ezhpcy-doctor-{secrets.token_hex(4)}"]
    problems: dict[str, AliasProblem | None] = {}
    try:
        with probe_hosts(probed):
            problems = {alias: resolution_problem(alias) for alias in probed}
    except (OSError, FilePermissionError) as error:
        yield Check(CheckStatus.FAIL, t"Could not write the SSH probe: {error}")
        return

    for alias, problem in problems.items():
        if problem is None:
            yield Check(
                CheckStatus.OK,
                t"{f'ssh {alias}':suggestion} goes through EzHPCy"
                if aliases
                else t"OpenSSH reads EzHPCy's SSH configuration",
            )
        elif problem.ssh_failed:
            yield Check(
                CheckStatus.FAIL,
                t"OpenSSH cannot resolve {f'ssh {alias}':suggestion}: {problem.detail}",
                hint=problem.hint,
            )
        else:
            yield Check(
                CheckStatus.FAIL,
                t"{f'ssh {alias}':suggestion} bypasses EzHPCy: {problem.detail}"
                if aliases
                else t"OpenSSH does not read EzHPCy's SSH configuration: "
                t"{problem.detail}",
                hint=problem.hint,
                include_lines=problem.include_lines,
            )
