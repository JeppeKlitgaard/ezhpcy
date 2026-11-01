"""
Messages (see `ezhpcy.messages`) on the terminal: each part styled by its kind.

Printed, errors, warnings and aborts go to stderr with Cyclopts' usage errors,
prompts and the log.
"""

from dataclasses import dataclass
lazy from string.templatelib import Template

from cyclopts.exceptions import (
    STYLE_NAME,
    STYLE_OFFENDING_VALUE,
    STYLE_SOURCE,
    STYLE_SUGGESTION,
    STYLE_VALID_CHOICE,
)
lazy from rich.console import Console
lazy from rich.text import Text

lazy from ezhpcy.cli._console import console, error_console
lazy from ezhpcy.messages import message_parts


@dataclass(frozen=True)
class _Split:
    """
    Style the last part, after the last of `separators`, apart from the rest.
    Without a separator, it's all the last part.
    """

    separators: str
    last_style: str
    # By default, the separator is styled with the part before it.
    separator_style: str | None = None


@dataclass(frozen=True)
class _Style:
    style: str | None
    # Whether the quotes take the style too.
    styled_quotes: bool = False
    split: _Split | None = None


_DETAIL_STYLE = "bold cyan"

# In the style of Cyclopts' own errors, e.g.
# `Invalid value "nope" for PRESET. Choose from: "dtu", "generic".`
# Cyclopts has no style for paths, details or emphasis; an aside is dimmed like
# its `from <CONFIG>` suffixes. A suggestion's quotes are part of what to run or
# write, and a target is styled as one, without them. Paths are styled as Rich's
# highlighter styles them in reprs (local paths on Windows use backslashes), and
# an endpoint's host as a detail.
_STYLES = {
    "": _Style(None),
    "name": _Style(STYLE_NAME),
    "value": _Style(STYLE_OFFENDING_VALUE),
    "choice": _Style(STYLE_VALID_CHOICE),
    "suggestion": _Style(STYLE_SUGGESTION, styled_quotes=True),
    "target": _Style(STYLE_SUGGESTION),
    "path": _Style("repr.path", split=_Split("/\\", "repr.filename")),
    "detail": _Style(_DETAIL_STYLE),
    "endpoint": _Style(
        _DETAIL_STYLE, split=_Split(":", "cyan", separator_style="bold white")
    ),
    "emphasis": _Style("bold red"),
    "aside": _Style(STYLE_SOURCE),
    "hint": _Style("green"),
}


def rich_text(message: Template) -> Text:
    """Render the message with each part styled by its kind."""
    text = Text()
    for part, kind, quote in message_parts(message):
        style = _STYLES[kind]
        if style.styled_quotes:
            text.append(f"{quote}{part}{quote}", style=style.style)
        elif (split := style.split) is not None:
            at = max(part.rfind(separator) for separator in split.separators)
            head, separator, last = (
                (part[:at], part[at], part[at + 1 :]) if at >= 0 else ("", "", part)
            )
            if split.separator_style is None:
                head, separator = head + separator, ""
            text.append(head, style=style.style)
            text.append(separator, style=split.separator_style)
            text.append(last, style=split.last_style)
        else:
            text.append(quote)
            text.append(part, style=style.style)
            text.append(quote)
    return text


def duration(seconds: float) -> str:
    """`seconds` as `1 h 5 min`, `2 min 30 s`, or `45 s` under a minute."""
    minutes, seconds = divmod(round(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes} min"
    return f"{minutes} min {seconds} s" if minutes else f"{seconds} s"


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
