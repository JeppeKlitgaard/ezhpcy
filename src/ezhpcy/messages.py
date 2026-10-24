"""
User-facing messages as template strings, styled by what their parts are.

An interpolation's format spec says what it is:

- `name`: an option, argument, setting or profile.
- `value`: a value the user gave, in double quotes.
- `choice`: a valid value, in double quotes.
- `suggestion`: something to run or write, in single quotes.
- `target`: what a command acts on, such as the host in `ssh gpu`, styled as a
  suggestion without quotes.
- `path`: a local or remote file or directory.
- `detail`: something the program reports, such as a job ID, a host or a
  duration.
- `endpoint`: a `host:port`, its host styled as a detail and its port apart.
- `emphasis`: a word to stress, such as "all" before something destructive.
- `aside`: context that can be skipped, shown dimmed.
- `hint`: something to run or write that the program offers, such as a command
  that mounts what it serves, in green but not bold, unlike a suggestion.

Without one, it's plain text. The quotes make a part stand out without colour
too. A format spec for the value may follow the kind, after a colon. A list or
tuple is several of the same kind, separated by commas, and a nested template is
rendered in place, its plain text in the enclosing kind:

    t"Choose from: {names:choice}."  # Choose from: "a", "b".
    t"Run {'ezhpcy doctor':suggestion}."  # Run 'ezhpcy doctor'.
    t"Retrying in {2.50:detail:g} seconds."  # Retrying in 2.5 seconds.
    t"Waiting {t'(timeout in {wait:detail})':aside}."  # Dimmed but `wait`.

A `MessageError`'s message is a template too, and the error is rendered as its
template wherever it is interpolated: `t"Could not start tunnel: {error}"`.

Printed, errors, warnings and aborts go to stderr with Cyclopts' usage errors,
prompts and the log.
"""

from dataclasses import dataclass
lazy from collections.abc import Iterator
lazy from string.templatelib import Interpolation, Template, convert

from cyclopts.exceptions import (
    STYLE_NAME,
    STYLE_OFFENDING_VALUE,
    STYLE_SOURCE,
    STYLE_SUGGESTION,
    STYLE_VALID_CHOICE,
)
lazy from rich.console import Console
lazy from rich.text import Text

lazy from ezhpcy.console import console, error_console


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
class _Kind:
    style: str | None
    quote: str = ""
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
_KINDS = {
    "": _Kind(None),
    "name": _Kind(STYLE_NAME),
    "value": _Kind(STYLE_OFFENDING_VALUE, '"'),
    "choice": _Kind(STYLE_VALID_CHOICE, '"'),
    "suggestion": _Kind(STYLE_SUGGESTION, "'", styled_quotes=True),
    "target": _Kind(STYLE_SUGGESTION),
    "path": _Kind("repr.path", split=_Split("/\\", "repr.filename")),
    "detail": _Kind(_DETAIL_STYLE),
    "endpoint": _Kind(
        _DETAIL_STYLE, split=_Split(":", "cyan", separator_style="bold white")
    ),
    "emphasis": _Kind("bold red"),
    "aside": _Kind(STYLE_SOURCE),
    "hint": _Kind("green"),
}


class MessageError(Exception):
    """An error whose message is a template, styled wherever it is shown."""

    def __init__(self, message: Template) -> None:
        self.message = message
        super().__init__(plain(message))


def _parts(
    message: Template, plain_kind: _Kind = _KINDS[""]
) -> Iterator[tuple[str, _Kind]]:
    """Yield the message's text in pieces, each with its kind."""
    for part in message:
        if isinstance(part, str):
            yield part, plain_kind
            continue
        yield from _interpolation_parts(part)


def _interpolation_parts(part: Interpolation) -> Iterator[tuple[str, _Kind]]:
    name, _, spec = part.format_spec.partition(":")
    if name not in _KINDS:
        raise ValueError(f"unknown message part kind: {name!r}")
    kind = _KINDS[name]
    value = convert(part.value, part.conversion)
    items = value if isinstance(value, list | tuple) else [value]
    for index, item in enumerate(items):
        if index:
            yield ", ", _KINDS[""]
        if isinstance(item, MessageError):
            item = item.message
        if isinstance(item, Template):
            yield from _parts(item, kind)
        else:
            yield format(item, spec), kind


def plain(message: Template) -> str:
    """Render the message as plain text."""
    return "".join(f"{kind.quote}{text}{kind.quote}" for text, kind in _parts(message))


def rich_text(message: Template) -> Text:
    """Render the message with each part styled by its kind."""
    text = Text()
    for part, kind in _parts(message):
        if kind.styled_quotes:
            text.append(f"{kind.quote}{part}{kind.quote}", style=kind.style)
        elif (split := kind.split) is not None:
            at = max(part.rfind(separator) for separator in split.separators)
            head, separator, last = (
                (part[:at], part[at], part[at + 1 :]) if at >= 0 else ("", "", part)
            )
            if split.separator_style is None:
                head, separator = head + separator, ""
            text.append(head, style=kind.style)
            text.append(separator, style=split.separator_style)
            text.append(last, style=split.last_style)
        else:
            text.append(kind.quote)
            text.append(part, style=kind.style)
            text.append(kind.quote)
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
