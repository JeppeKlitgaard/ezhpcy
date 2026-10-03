"""Public wrapper around Typer's private ``TyperChoice``.

Import ``Choice`` from here rather than ``typer._types``, so the private import
lives in one place if Typer moves it.
"""

from typer._types import TyperChoice


class Choice(TyperChoice[str]):
    """A Typer choice type built from runtime values, for use as ``click_type``.

    Typer only exposes choices through ``Literal`` or ``Enum`` annotations, and
    neither can be built from runtime values without breaking type checkers.
    """
