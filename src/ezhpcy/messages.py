"""
User-facing messages as template strings, whose parts say what they are.

An interpolation's format spec says what it is:

- `name`: an option, argument, setting or profile.
- `value`: a value the user gave, in double quotes.
- `choice`: a valid value, in double quotes.
- `suggestion`: something to run or write, in single quotes.
- `target`: what a command acts on, such as the host in `ssh gpu`, a suggestion
  without quotes.
- `path`: a local or remote file or directory.
- `detail`: something the program reports, such as a job ID, a host or a
  duration.
- `endpoint`: a `host:port`.
- `emphasis`: a word to stress, such as "all" before something destructive.
- `aside`: context that can be skipped.
- `hint`: something to run or write that the program offers, such as a command
  that mounts what it serves.

Without one, it's plain text. The quotes make a part stand out without colour
too. A format spec for the value may follow the kind, after a colon. A list or
tuple is several of the same kind, separated by commas, and a nested template is
rendered in place, its plain text in the enclosing kind:

    t"Choose from: {names:choice}."  # Choose from: "a", "b".
    t"Run {'ezhpcy doctor':suggestion}."  # Run 'ezhpcy doctor'.
    t"Retrying in {2.50:detail:g} seconds."  # Retrying in 2.5 seconds.
    t"Waiting {t'(timeout in {wait:detail})':aside}."  # An aside but `wait`.

A `MessageError`'s message is a template too, and the error is rendered as its
template wherever it is interpolated: `t"Could not start tunnel: {error}"`.

`message_parts` gives a message's parts to a program that shows them its own way.
"""

lazy from collections.abc import Iterator
lazy from string.templatelib import Interpolation, Template, convert

_QUOTES = {
    "": "",
    "name": "",
    "value": '"',
    "choice": '"',
    "suggestion": "'",
    "target": "",
    "path": "",
    "detail": "",
    "endpoint": "",
    "emphasis": "",
    "aside": "",
    "hint": "",
}


class MessageError(Exception):
    """An error whose message is a template, styled wherever it is shown."""

    def __init__(self, message: Template) -> None:
        self.message = message
        super().__init__(plain(message))


def message_parts(message: Template) -> Iterator[tuple[str, str, str]]:
    """The message's text in pieces, each with its kind, and its kind's quote."""
    return _parts(message, "")


def _parts(message: Template, plain_kind: str) -> Iterator[tuple[str, str, str]]:
    for part in message:
        if isinstance(part, str):
            yield part, plain_kind, _QUOTES[plain_kind]
            continue
        yield from _interpolation_parts(part)


def _interpolation_parts(part: Interpolation) -> Iterator[tuple[str, str, str]]:
    kind, _, spec = part.format_spec.partition(":")
    if kind not in _QUOTES:
        raise ValueError(f"unknown message part kind: {kind!r}")
    value = convert(part.value, part.conversion)
    items = value if isinstance(value, list | tuple) else [value]
    for index, item in enumerate(items):
        if index:
            yield ", ", "", ""
        if isinstance(item, MessageError):
            item = item.message
        if isinstance(item, Template):
            yield from _parts(item, kind)
        else:
            yield format(item, spec), kind, _QUOTES[kind]


def plain(message: Template) -> str:
    """Render the message as plain text."""
    return "".join(f"{quote}{text}{quote}" for text, _, quote in message_parts(message))
