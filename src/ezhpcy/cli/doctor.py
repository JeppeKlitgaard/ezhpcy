lazy from rich.text import Text

lazy from ezhpcy import api
lazy from ezhpcy.cli import _include as include
lazy from ezhpcy.cli._console import console
lazy from ezhpcy.cli._messages import rich_text
lazy from ezhpcy.cli._options import ConfigArg

_STATUS_STYLES = {
    "ok": "bold green",
    "warn": "bold yellow",
    "fail": "bold red",
}
_SECTION_TITLES = {
    "configuration": "Configuration",
    "ssh": "SSH (lets `ssh <alias>` reach running tunnels)",
}


def _print_check(check: api.Check, printed_hints: set[str]) -> None:
    status = Text(f"{check.status:<4}", style=_STATUS_STYLES[check.status])
    console.print(Text("  ") + status + Text("  ") + rich_text(check.message))
    if check.hint is None:
        return
    # Several checks share a remedy; repeating it adds noise, not information.
    hint = rich_text(
        check.hint if check.include_lines is None else include.INCLUDE_HINT
    )
    if hint.plain not in printed_hints:
        printed_hints.add(hint.plain)
        console.print(Text("        ") + hint, style="dim")
        if check.include_lines is not None:
            console.print()
            include.echo_include_lines(check.include_lines)


def doctor_cmd(*, config: ConfigArg) -> None:
    """Check the configuration and that OpenSSH picks up EzHPCy's Host blocks."""
    printed_hints: set[str] = set()
    warnings = 0
    section = None
    # Checks run as they are read, so later checks never act on a broken earlier
    # step.
    for check in api.diagnose(config):
        if check.section != section:
            if section is not None:
                console.print()
            section = check.section
            console.print(Text(_SECTION_TITLES[section], style="bold"))
        _print_check(check, printed_hints)
        if check.status is api.CheckStatus.FAIL:
            console.print()
            console.print(
                Text("Stopped at the first failure", style="bold red")
                + rich_text(t"; fix it and run {'ezhpcy doctor':suggestion} again.")
            )
            raise SystemExit(1)
        warnings += check.status is api.CheckStatus.WARN

    console.print()
    if warnings:
        console.print(
            Text("No failures", style="bold yellow") + Text(f", {warnings} warning(s).")
        )
    else:
        console.print(Text("All checks passed.", style="bold green"))
