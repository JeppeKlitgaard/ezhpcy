import typer.core
import re


class AliasGroup(typer.core.TyperGroup):
    # https://github.com/fastapi/typer/issues/132#issuecomment-1714516903

    _CMD_SPLIT_P = r"[,| ?\/]"

    def _group_cmd_name(self, default_name):
        for cmd in self.commands.values():
            if cmd.name and default_name in re.split(self._CMD_SPLIT_P, cmd.name):
                return cmd.name
        return default_name

    def get_command(self, ctx, cmd_name):
        cmd_name = self._group_cmd_name(cmd_name)
        return super().get_command(ctx, cmd_name)
