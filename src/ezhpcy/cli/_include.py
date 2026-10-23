"""
The Include lines that make `ssh <alias>` read EzHPCy's Host blocks, as the CLI
shows them: after a hint that points at them, ready to copy.
"""

lazy from string.templatelib import Template

lazy from rich.text import Text

lazy from ezhpcy.console import console, error_console
lazy from ezhpcy.tunnel.ssh_config import AliasProblem, include_directive

INCLUDE_HINT = (
    t"If {'~/.ssh/config':path} does not include EzHPCy's SSH profiles yet, copy "
    t"the two highlighted lines below to its top, before any Host or Match block:"
)


def hint(problem: AliasProblem) -> Template:
    """The problem's fix, pointing at the Include lines printed after it."""
    return INCLUDE_HINT if problem.include_lines is not None else problem.hint


def explanation(problem: AliasProblem) -> Template:
    """The problem and its fix; follow it with `print_include_lines`."""
    return t"{problem.detail}. {hint(problem)}"


def print_include_lines(problem: AliasProblem) -> None:
    """Print the Include lines to stderr, after a blank line, if they are the fix."""
    if problem.include_lines is not None:
        error_console.print()
        echo_include_directive(err=True)


def echo_include_directive(*, err: bool = False) -> None:
    """Print the Include lines followed by a blank line, ready to copy verbatim."""
    # Unindented and unwrapped, even for long paths; the colour marks what to
    # copy and is dropped when output is not a terminal.
    (error_console if err else console).print(
        Text(include_directive() + "\n", style="bold cyan"), soft_wrap=True
    )
