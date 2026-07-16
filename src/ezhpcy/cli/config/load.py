import difflib
import tomllib
from importlib import resources
from pathlib import Path
from typing import Annotated, Literal

import typer
from jinja2 import Environment, StrictUndefined, TemplateError
from pydantic import ValidationError
from rich.prompt import Confirm, Prompt
from rich.text import Text

from ezhpcy import console
from ezhpcy.cli.common import UserOpt
from ezhpcy.config import Config, config

PRESET_DIRECTORY = "static/config/presets"
PRESET_SUFFIX = ".toml.j2"


def _available_presets() -> dict[str, tuple[str, resources.abc.Traversable]]:
    preset_directory = resources.files("ezhpcy").joinpath(PRESET_DIRECTORY)
    return {
        resource.name.removesuffix(PRESET_SUFFIX).casefold(): (
            resource.name.removesuffix(PRESET_SUFFIX),
            resource,
        )
        for resource in preset_directory.iterdir()
        if resource.is_file() and resource.name.endswith(PRESET_SUFFIX)
    }


_AVAILABLE_PRESET_NAMES = sorted(
    (name for name, _resource in _available_presets().values()),
    key=str.casefold,
)
PresetName = Literal[*_AVAILABLE_PRESET_NAMES]


def _render_preset(template_text: str, *, preset: str, user: str) -> str:
    environment = Environment(
        autoescape=False,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    return environment.from_string(template_text).render(preset=preset, user=user)


def _diff_text(
    current: str, replacement: str, *, config_file: Path, preset: str
) -> Text:
    lines = difflib.unified_diff(
        current.splitlines(),
        replacement.splitlines(),
        fromfile=str(config_file),
        tofile=f"{preset} preset",
        lineterm="",
    )
    output = Text()
    for line in lines:
        if line.startswith(("--- ", "+++ ")):
            style = "bold cyan"
        elif line.startswith("@@"):
            style = "magenta"
        elif line.startswith("+"):
            style = "green"
        elif line.startswith("-"):
            style = "red"
        else:
            style = None
        output.append(line + "\n", style=style)
    return output


def load_cmd(
    preset: Annotated[
        PresetName,
        typer.Argument(
            help="Packaged configuration preset to load.",
            case_sensitive=False,
        ),
    ],
    user: UserOpt = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Overwrite an existing configuration without confirmation.",
        ),
    ] = False,
) -> None:
    """Render a packaged preset into the EzHPCy configuration file."""
    presets = _available_presets()
    preset_entry = presets.get(preset.casefold())
    if preset_entry is None:
        available = (
            ", ".join(
                sorted((name for name, _resource in presets.values()), key=str.casefold)
            )
            or "none"
        )
        raise typer.BadParameter(
            f"Unknown preset {preset!r}. Available presets: {available}.",
            param_hint="preset",
        )
    preset_name, preset_resource = preset_entry

    try:
        template_text = preset_resource.read_text(encoding="utf-8")
    except OSError as error:
        raise typer.BadParameter(
            f"Could not read preset {preset_name!r}: {error}",
            param_hint="preset",
        ) from error

    if user is None:
        user = Prompt.ask("Username", console=console)
    try:
        rendered = _render_preset(template_text, preset=preset_name, user=user)
        Config.from_mapping(tomllib.loads(rendered))
    except (TemplateError, tomllib.TOMLDecodeError, ValidationError) as error:
        raise typer.BadParameter(
            f"Preset {preset_name!r} produced invalid configuration: {error}",
            param_hint="preset",
        ) from error
    config_file = config.local_file.config_file

    if config_file.exists():
        try:
            current = config_file.read_text(encoding="utf-8")
        except OSError as error:
            raise typer.BadParameter(
                f"Could not read configuration file {config_file}: {error}"
            ) from error

        console.print(
            "[bold yellow]Warning[/bold yellow]: loading this preset will "
            f"overwrite [bold blue]{config_file}[/bold blue]."
        )
        diff = _diff_text(
            current,
            rendered,
            config_file=config_file,
            preset=preset_name,
        )
        console.print(
            diff if diff else Text("(no changes)\n", style="dim"),
            end="",
            soft_wrap=True,
        )

        if not yes and not Confirm.ask(
            "Overwrite the existing configuration?",
            console=console,
            default=False,
        ):
            console.print(
                "[bold yellow]Aborted[/bold yellow]: configuration unchanged."
            )
            raise typer.Exit(code=1)

    try:
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(rendered, encoding="utf-8")
    except OSError as error:
        raise typer.BadParameter(
            f"Could not write configuration file {config_file}: {error}"
        ) from error

    console.print(
        f"[bold green]Success[/bold green]: loaded the [bold purple]{preset_name}[/bold purple] "
        f"preset into [bold blue]{config_file}[/bold blue]."
    )
