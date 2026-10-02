import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from ezhpcy.config import config
from ezhpcy.console import console

IS_WINDOWS = os.name == "nt"
DEFAULT_EDITOR = "notepad" if IS_WINDOWS else "vi"
# Windows runs batch files, such as VS Code's `code.cmd`, through cmd.exe even
# without a shell, and cmd.exe interprets these characters in their arguments.
_BATCH_FILE_SUFFIXES = frozenset({".bat", ".cmd"})
_BATCH_METACHARACTERS = frozenset('%&|<>^"')


def _unquote(argument: str) -> str:
    if len(argument) >= 2 and argument[0] == argument[-1] == '"':
        return argument[1:-1]
    return argument


def _editor_command(editor_command: str, config_file: str) -> list[str]:
    """Split an editor command such as `code --wait` and resolve its executable."""
    if executable := shutil.which(editor_command):
        # An unquoted path with spaces, e.g. C:/Program Files/Editor/editor.exe.
        arguments = [executable]
    else:
        arguments = shlex.split(editor_command, posix=not IS_WINDOWS)
        if IS_WINDOWS:
            # Non-POSIX splitting keeps backslashes in paths, but also quotes.
            arguments = [_unquote(argument) for argument in arguments]
        if not arguments:
            raise ValueError("the editor command is empty")
        arguments[0] = shutil.which(arguments[0]) or arguments[0]

    command = [*arguments, config_file]
    if IS_WINDOWS and Path(command[0]).suffix.lower() in _BATCH_FILE_SUFFIXES:
        unsafe = [
            argument
            for argument in command[1:]
            if _BATCH_METACHARACTERS.intersection(argument)
        ]
        if unsafe:
            raise ValueError(
                f"batch-file editor {command[0]} cannot safely be given "
                f"arguments containing any of {''.join(sorted(_BATCH_METACHARACTERS))}: "
                + ", ".join(unsafe)
            )
    return command


def edit_cmd(
    editor: Annotated[
        str | None,
        typer.Argument(
            help="Editor command to use (for example, 'code --wait'). Uses "
            "VISUAL, EDITOR, or the platform default when omitted."
        ),
    ] = None,
) -> None:
    """Open the EzHPCy configuration file in an editor and wait for it to close."""
    config_file = config.local_file.config_file

    try:
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.touch(exist_ok=True)
    except OSError as error:
        raise typer.BadParameter(
            f"Could not create configuration file {config_file}: {error}"
        ) from error

    editor_command = (
        editor or os.environ.get("VISUAL") or os.environ.get("EDITOR") or DEFAULT_EDITOR
    )

    try:
        command = _editor_command(editor_command, str(config_file))
        # Wait so that a terminal editor such as vi keeps the terminal until it
        # exits, instead of competing with the shell for it.
        result = subprocess.run(command, check=False)
    except ValueError as error:
        raise typer.BadParameter(
            f"Invalid editor command {editor_command!r}: {error}",
            param_hint="editor",
        ) from error
    except OSError as error:
        raise typer.BadParameter(
            f"Could not open configuration file {config_file}: {error}",
            param_hint="editor",
        ) from error

    if result.returncode != 0:
        console.print(
            f"[bold red]Error[/bold red]: editor exited with status {result.returncode}."
        )
        raise typer.Exit(code=1)
