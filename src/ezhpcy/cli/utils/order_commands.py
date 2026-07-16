from collections.abc import Sequence

import typer
import typer.core


# https://github.com/fastapi/typer/issues/428#issuecomment-2041956972
class OrderCommandsGroup(typer.core.TyperGroup):
    """A Typer group whose help output follows a declared command order."""

    command_order: Sequence[str] = ()

    def list_commands(self, ctx: typer.Context):
        """Return declared commands first, then any commands not declared."""
        ordered_commands = [
            command for command in self.command_order if command in self.commands
        ]
        additional_commands = [
            command for command in self.commands if command not in ordered_commands
        ]

        return ordered_commands + additional_commands
