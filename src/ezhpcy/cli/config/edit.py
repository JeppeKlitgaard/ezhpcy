import os
import shutil
import subprocess
from typing import Annotated

import typer

from ezhpcy.config import config

DEFAULT_EDITOR = "notepad" if os.name == "nt" else "vi"
IS_WINDOWS = os.name == "nt"


def _launch_editor(editor_command: str, config_file: str) -> None:
    executable = shutil.which(editor_command) or editor_command
    if IS_WINDOWS:
        startfile = getattr(os, "startfile", None)
        if startfile is None:
            raise OSError("Windows application launcher is unavailable")
        startfile(executable, arguments=f'"{config_file}"')
    else:
        subprocess.Popen([executable, config_file])


def edit_cmd(
    editor: Annotated[
        str | None,
        typer.Argument(
            help="Editor command to use (for example, 'code'). Uses VISUAL, "
            "EDITOR, or the platform default when omitted."
        ),
    ] = None,
) -> None:
    """Open the ezhpcy configuration file in an editor."""
    config_file = config.local_file.config_file

    try:
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.touch(exist_ok=True)
    except OSError as error:
        raise typer.BadParameter(
            f"Could not create configuration file {config_file}: {error}"
        ) from error

    editor_command = (
        editor
        or os.environ.get("VISUAL")
        or os.environ.get("EDITOR")
        or DEFAULT_EDITOR
    )

    try:
        _launch_editor(editor_command, str(config_file))
    except OSError as error:
        raise typer.BadParameter(
            f"Could not open configuration file {config_file}: {error}",
            param_hint="editor",
        ) from error
