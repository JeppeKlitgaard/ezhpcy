import re

import typer.core
from typer._click import Command, Context


class AliasGroup(typer.core.TyperGroup):
    """A Typer group whose commands can be invoked by any of their aliases.

    A command named e.g. ``"list | ls"`` can be invoked as ``list`` or ``ls``.
    """

    # https://github.com/fastapi/typer/issues/132#issuecomment-1714516903

    _CMD_SPLIT_P = r"[,| ?\/]"

    def _group_cmd_name(self, default_name: str) -> str:
        """Return the full name of the command that has `default_name` as an alias."""
        for cmd in self.commands.values():
            if cmd.name and default_name in re.split(self._CMD_SPLIT_P, cmd.name):
                return cmd.name
        return default_name

    def get_command(self, ctx: Context, cmd_name: str) -> Command | None:
        """Resolve `cmd_name` as an alias before looking up the command."""
        cmd_name = self._group_cmd_name(cmd_name)
        return super().get_command(ctx, cmd_name)
