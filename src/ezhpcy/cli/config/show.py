import re
from dataclasses import dataclass
lazy import json
lazy from collections.abc import Iterable, Iterator, Sequence
lazy from string.templatelib import Template
lazy from typing import Annotated

lazy from cyclopts import Parameter
lazy from pydantic import BaseModel

lazy from ezhpcy.cli._options import ShownProfileArg
lazy from ezhpcy.cli._resolve import check_profile_name
lazy from ezhpcy.config import Config, config
lazy from ezhpcy.console import console
lazy from ezhpcy.messages import rich_text

# The top-level table that every profile's connection inherits, as an origin.
_TOP_LEVEL_CONNECTION = "[connection]"
# Never shown: it's accepted only to be rejected, and it would be a secret.
_HIDDEN = frozenset({"password"})
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class _Setting:
    key: str
    # As dumped to JSON, which is also how it's written in TOML.
    value: object
    configured: bool
    # Where an inherited value is configured, if not where it's shown.
    inherited_from: str | None = None


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else json.dumps(key, ensure_ascii=False)


def _toml_value(value: object) -> str:
    """Write a JSON value as TOML. A JSON string is a TOML basic string too."""
    match value:
        case None:
            return "(not set)"
        case bool():
            return "true" if value else "false"
        case list():
            return "[" + ", ".join(map(_toml_value, value)) + "]"
        case dict() if not value:
            return "{}"
        case dict():
            items = (f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items())
            return "{ " + ", ".join(items) + " }"
        case _:
            return json.dumps(value, ensure_ascii=False)


def _settings(
    model: BaseModel,
    layers: Sequence[tuple[str, BaseModel]],
    *,
    prefix: str = "",
    exclude: Iterable[str] = (),
) -> Iterator[_Setting]:
    """
    Yield the model's settings that aren't tables. Each is configured by the first
    of `layers`, the model's own and then those it inherits, that sets it.
    """
    skipped = {*_HIDDEN, *exclude}
    values = model.model_dump(mode="json", exclude=skipped)
    for name in type(model).model_fields:
        if name in skipped or isinstance(getattr(model, name), BaseModel):
            continue
        origin = None
        if name in model.model_fields_set:
            origin = next(
                (
                    layer
                    for layer, layer_model in layers
                    if name in layer_model.model_fields_set
                ),
                None,
            )
        yield _Setting(
            prefix + name,
            values[name],
            configured=origin is not None,
            inherited_from=None if origin == layers[0][0] else origin,
        )


def _tables(
    model: BaseModel, path: str, *, exclude: Iterable[str] = ()
) -> Iterator[tuple[str, list[_Setting]]]:
    """Yield the model's settings by table, `path` naming the model's own."""
    if settings := list(_settings(model, [(path, model)], exclude=exclude)):
        yield path, settings
    for name in type(model).model_fields:
        value = getattr(model, name)
        if name not in exclude and isinstance(value, BaseModel):
            yield from _tables(value, f"{path}.{name}" if path else name)


def _profile_settings(source: Config, profile_name: str) -> Iterator[_Setting]:
    """Yield the profile's settings as resolved, with sub-configs as dotted keys."""
    profiles = [
        (name, source.profile[name]) for name in source.inheritance_chain(profile_name)
    ]
    resolved = source.resolve_profile(profile_name)
    yield from _settings(resolved, profiles)
    # `inherit` itself isn't inherited.
    own = source.profile[profile_name]
    yield _Setting("inherit", own.inherit, configured="inherit" in own.model_fields_set)
    for section in type(resolved).model_fields:
        sub_config = getattr(resolved, section)
        if not isinstance(sub_config, BaseModel):
            continue
        layers = [(name, getattr(profile, section)) for name, profile in profiles]
        if section == "connection":
            layers.append((_TOP_LEVEL_CONNECTION, source.connection))
        yield from _settings(sub_config, layers, prefix=f"{section}.")


def _line(setting: _Setting) -> Template:
    value = _toml_value(setting.value)
    if not setting.configured:
        return t"{f'{setting.key} = {value}  # default':aside}"
    line = t"{setting.key:name} = {value}"
    if setting.inherited_from is None:
        return line
    return line + t"  {f'# from {setting.inherited_from}':aside}"


def show_cmd(
    profile: ShownProfileArg = None,
    /,
    *,
    only_profile: Annotated[
        bool,
        Parameter(
            name="--only-profile",
            help="Show only the profiles, without the settings outside them.",
        ),
    ] = False,
) -> None:
    """Show the configuration, marking inherited and default settings."""
    if profile is None:
        profile_names = list(config.profile)
    else:
        check_profile_name(profile)
        profile_names = [profile]
    # `ezhpcy info` shows the local files, which aren't configurable.
    tables = (
        []
        if only_profile
        else list(_tables(config, "", exclude={"local_file", "profile"}))
    )
    tables += [
        (f"profile.{_toml_key(name)}", list(_profile_settings(config, name)))
        for name in profile_names
    ]

    for index, (header, settings) in enumerate(tables):
        if index:
            console.print()
        if header:
            console.print(rich_text(t"[{header:name}]"), soft_wrap=True)
        for setting in settings:
            console.print(rich_text(_line(setting)), soft_wrap=True)
