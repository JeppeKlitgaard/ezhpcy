from enum import Enum
lazy import difflib
lazy import tomllib
lazy from importlib import resources
lazy from importlib.resources.abc import Traversable
lazy from pathlib import Path
lazy from typing import Annotated

lazy from cyclopts import Parameter
lazy from jinja2 import Environment, StrictUndefined, TemplateError
lazy from pydantic import ValidationError
lazy from rich.prompt import Confirm, Prompt
lazy from rich.text import Text

lazy from ezhpcy import console
lazy from ezhpcy.cli._errors import CliUsageError
lazy from ezhpcy.cli._options import UserOpt
lazy from ezhpcy.config import Config, config

PRESET_DIRECTORY = "static/config/presets"
PRESET_SUFFIX = ".toml.j2"


def _available_presets() -> dict[str, Traversable]:
    preset_directory = resources.files("ezhpcy").joinpath(PRESET_DIRECTORY)
    presets = {
        resource.name.removesuffix(PRESET_SUFFIX): resource
        for resource in preset_directory.iterdir()
        if resource.is_file() and resource.name.endswith(PRESET_SUFFIX)
    }
    return dict(sorted(presets.items(), key=lambda item: item[0].casefold()))


_PRESETS = _available_presets()
# An enum built at runtime, so the CLI matches presets case-insensitively and
# lists them in help.
Preset = Enum("Preset", {name: name for name in _PRESETS})


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
        Preset,
        Parameter(help="Packaged configuration preset to load."),
    ],
    /,
    *,
    user: UserOpt = None,
    yes: Annotated[
        bool,
        Parameter(
            name=["--yes", "-y"],
            help="Overwrite an existing configuration without confirmation.",
        ),
    ] = False,
) -> None:
    """Render a packaged preset into the EzHPCy configuration file."""
    preset_name = preset.value
    preset_resource = _PRESETS[preset_name]

    try:
        template_text = preset_resource.read_text(encoding="utf-8")
    except OSError as error:
        raise CliUsageError(
            t"Could not read it: {error}.",
            param_hint="PRESET",
            value=preset_name,
        ) from error

    if user is None:
        user = Prompt.ask("Username", console=console)
    try:
        rendered = _render_preset(template_text, preset=preset_name, user=user)
        Config.from_mapping(tomllib.loads(rendered))
    except (TemplateError, tomllib.TOMLDecodeError, ValidationError) as error:
        raise CliUsageError(
            t"It produced an invalid configuration: {error}",
            param_hint="PRESET",
            value=preset_name,
        ) from error
    config_file = config.local_file.config_file

    if config_file.exists():
        try:
            current = config_file.read_text(encoding="utf-8")
        except OSError as error:
            raise CliUsageError(
                t"Could not read configuration file {config_file}: {error}."
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
            diff or Text("(no changes)\n", style="dim"),
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
            raise SystemExit(1)

    try:
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(rendered, encoding="utf-8")
    except OSError as error:
        raise CliUsageError(
            t"Could not write configuration file {config_file}: {error}."
        ) from error

    console.print(
        f"[bold green]Success[/bold green]: loaded the [bold purple]{preset_name}[/bold purple] "
        f"preset into [bold blue]{config_file}[/bold blue]."
    )
