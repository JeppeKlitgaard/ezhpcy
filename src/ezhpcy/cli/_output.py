"""
Styled CLI output for messages (see `ezhpcy.messages`).

Errors, warnings, aborts and prompts go to stderr
with Cyclopts' usage errors and the log.
"""

lazy from string.templatelib import Template

from cyclopts.exceptions import (
    STYLE_NAME,
    STYLE_OFFENDING_VALUE,
    STYLE_SUGGESTION,
    STYLE_VALID_CHOICE,
)
lazy from rich.console import Console
lazy from rich.text import Text

lazy from ezhpcy.console import console, error_console
lazy from ezhpcy.messages import QUOTED_KINDS, parts

# The kinds of message part (see `ezhpcy.messages`) in the style of Cyclopts' own
# errors, e.g. `Invalid value "nope" for PRESET. Choose from: "dtu", "generic".`
# Cyclopts has no style for paths or emphasis.
_STYLES = {
    "": None,
    "name": STYLE_NAME,
    "value": STYLE_OFFENDING_VALUE,
    "choice": STYLE_VALID_CHOICE,
    "suggestion": STYLE_SUGGESTION,
    "path": "bold blue",
    "emphasis": "bold red",
}


def rich_text(message: Template) -> Text:
    """Render a message with each part styled by its kind."""
    text = Text()
    for part, kind in parts(message):
        if kind in QUOTED_KINDS:
            text.append('"')
            text.append(part, style=_STYLES[kind])
            text.append('"')
        else:
            text.append(part, style=_STYLES[kind])
    return text


def print_error(message: Template, *, label: str = "Error") -> None:
    """Print a runtime failure as `Error: message`, the label in bold red."""
    _print_labelled(error_console, label, "bold red", message)


def print_warning(message: Template) -> None:
    """Print `Warning: message`, the label in bold yellow."""
    _print_labelled(error_console, "Warning", "bold yellow", message)


def print_aborted(message: Template) -> None:
    """Print `Aborted: message` for a declined confirmation, the label in bold yellow."""
    _print_labelled(error_console, "Aborted", "bold yellow", message)


def print_success(message: Template) -> None:
    """Print `Success: message`, the label in bold green."""
    _print_labelled(console, "Success", "bold green", message)


def _print_labelled(to: Console, label: str, style: str, message: Template) -> None:
    to.print(Text(label, style=style) + Text(": ") + rich_text(message))
