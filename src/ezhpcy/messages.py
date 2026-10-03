"""User-facing messages as template strings, so the CLI can style their parts.

An interpolation's format spec says what it is: `name` (an option, argument,
setting or profile), `value` (a value the user gave), `choice` (a valid value) or
`suggestion` (something to run or write). Without one, it's plain text. Values and
choices are shown in double quotes. A list or tuple is several of the same kind,
separated by commas, and a nested template is rendered in place:

    t"Choose from: {names:choice}."  # Choose from: "a", "b".
"""

from collections.abc import Iterator
from string.templatelib import Interpolation, Template, convert

KINDS = frozenset({"", "name", "value", "choice", "suggestion"})
QUOTED_KINDS = frozenset({"value", "choice"})


def parts(message: Template) -> Iterator[tuple[str, str]]:
    """Yield the message's text in pieces, each with its kind ("" for plain)."""
    for part in message:
        if isinstance(part, str):
            yield part, ""
            continue
        yield from _interpolation_parts(part)


def _interpolation_parts(part: Interpolation) -> Iterator[tuple[str, str]]:
    kind = part.format_spec
    if kind not in KINDS:
        raise ValueError(f"unknown message part kind: {kind!r}")
    value = convert(part.value, part.conversion)
    items = value if isinstance(value, list | tuple) else [value]
    for index, item in enumerate(items):
        if index:
            yield ", ", ""
        if isinstance(item, Template):
            yield from parts(item)
        else:
            yield str(item), kind


def plain(message: Template) -> str:
    """Render the message as plain text."""
    return "".join(
        f'"{text}"' if kind in QUOTED_KINDS else text for text, kind in parts(message)
    )
