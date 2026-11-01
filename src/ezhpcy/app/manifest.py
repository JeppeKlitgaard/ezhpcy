"""
The apps manifest: a TOML file on this machine whose `[ezhpcy]` table describes
apps, one table per version of each, `[ezhpcy.app.<name>."<version>"]`. An app
(`App`) runs on a host; a table without `environment`, `run` or `endpoint` is a
link (`Link`) instead, which runs nothing and has only a URL. An app with actions
needs no `run` and `endpoint`: then it is its actions alone, with no server. Every
other top-level table belongs to other tools and is ignored; inside `[ezhpcy]`,
unknown keys are errors.

`Manifest.load` reads the file and checks everything that can be checked here:
the models' rules, the names and versions, the Pixi files and lock file, the
assets, and the placeholders. Placeholders (`{name}`, `str.format` syntax) are
rendered by `render`, the one renderer, which shares its parser with the checks.

Names follow the rules for a Python package's name, and are compared normalised
(`My_App` is `my-app`); versions are `v` and a PEP 440 version, compared as
such (`v1.2` is `v1.2.0`). `collect_apps` gathers several manifests' apps, and
`choose_app` picks one by name and version, the newest by default.
`builtin_manifests` are the manifests EzHPCy ships.

An app may have actions (`Action`), `[ezhpcy.app.<name>."<version>".action.<name>]`:
commands run on its host, in its environment, while it runs, by hand or by a
timer, which may take inputs as placeholders of their own. An app may take
arguments, `[ezhpcy.app.<name>."<version>".argument.<name>]`, given as it starts,
as placeholders for all of it. Both are described by a `Parameter`.
"""

import functools
import math
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self
lazy import hashlib
lazy import stat
lazy import tomllib
lazy from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
lazy from pathlib import Path

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    PrivateAttr,
    Tag,
    ValidationInfo,
    field_validator,
    model_validator,
)
lazy import minijinja
lazy from packaging.utils import canonicalize_name
lazy from packaging.version import InvalidVersion, Version
lazy from pydantic import ValidationError

from ezhpcy.types import Seconds
lazy from ezhpcy.app import ManifestError
lazy from ezhpcy.logging import get_logger

MANIFEST_FILE_NAME = "ezhpcy-apps.toml"
SCHEMA_VERSION = 1
# A Python package's name (PEP 508), which never contains `@`, so that
# `NAME@VERSION` is unambiguous.
_APP_NAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")
# `v`, then a PEP 440 version, which starts with a digit (an epoch's too).
_APP_VERSION = re.compile(r"^v[0-9]\S*$")
# A key TOML takes unquoted.
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
# The platform of the cluster's nodes, which the lock file must have packages for.
_PLATFORM = "linux-64"
_PIXI_MANIFEST_NAMES = frozenset({"pixi.toml", "pyproject.toml"})
_LOCK_FILE_NAME = "pixi.lock"
# Lock files from this version on list each package by its location.
_MIN_LOCK_VERSION = 6
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Every placeholder, and the environment variable that carries its value too.
# `local_port` and `alias` (a link's Host alias) are only known on this machine,
# so they have none.
ENVIRONMENT_VARIABLES = {
    "socket": "EZHPCY_SOCKET",
    "port": "EZHPCY_PORT",
    "run_dir": "EZHPCY_RUN_DIR",
    "assets": "EZHPCY_ASSETS",
    "workdir": "EZHPCY_WORKDIR",
    "token": "EZHPCY_TOKEN",
}
_HOST_PLACEHOLDERS = frozenset({"socket", "port", "run_dir", "assets", "workdir"})
# The app's, not the action's own run: an action has no `run_dir`.
_ACTION_PLACEHOLDERS = _HOST_PLACEHOLDERS - {"run_dir"}
# What a `path` input's default may use.
_PATH_DEFAULT_PLACEHOLDERS = frozenset({"assets", "workdir"})
# As `{{ flag }}` renders, for a command line, not as Python spells them.
_BOOLEAN_TEXT = {True: "true", False: "false"}
# Far more than a manifest's template needs, and an end to one that never would.
_TEMPLATE_FUEL = 1_000_000
# The placeholders each field takes. `{{ token }}` is not in `run.command`: argv can
# be read by other users on the host.
FIELD_PLACEHOLDERS = {
    "run.command": _HOST_PLACEHOLDERS,
    "run.env": _HOST_PLACEHOLDERS | {"token"},
    "access.url": frozenset({"token", "local_port", "alias"}),
    "access.status_hints": frozenset({"token", "local_port", "alias"}),
    "action.run.command": _ACTION_PLACEHOLDERS,
    "action.run.env": _ACTION_PLACEHOLDERS | {"token"},
    # Known before anything starts, so arguments alone.
    "action.trigger.every": frozenset[str](),
}
_ALL_PLACEHOLDERS = frozenset(ENVIRONMENT_VARIABLES) | {"local_port", "alias"}


@functools.cache
def _environment() -> minijinja.Environment:
    return minijinja.Environment(
        undefined_behavior="strict",
        auto_escape_callback=lambda _name: False,
        # What a template may do is bounded: it has no way to reach Python.
        fuel=_TEMPLATE_FUEL,
        finalizer=lambda value: (
            _BOOLEAN_TEXT.get(value, value) if isinstance(value, bool) else value
        ),
    )


def _why(error: minijinja.TemplateError) -> str:
    return str(error.message).removesuffix(" (in <string>:1)")


def placeholders(template: str) -> set[str]:
    """
    The placeholders `template` uses, in its blocks too. Raises `ValueError` if it
    isn't a valid template.
    """
    try:
        return set(_environment().undeclared_variables_in_str(template))
    except minijinja.TemplateError as error:
        raise ValueError(_why(error)) from None


def render(template: str, values: Mapping[str, object]) -> str:
    """
    Render `template` with `values`, which aren't rendered in turn. Every field
    that takes placeholders is rendered here. Raises `ValueError` if it can't be,
    such as for a value it uses that has none.
    """
    try:
        return _environment().render_str(template, **values)
    except minijinja.TemplateError as error:
        raise ValueError(_why(error)) from None


def render_command(templates: Iterable[str], values: Mapping[str, object]) -> list[str]:
    """
    The arguments of a command. One with a block that renders empty is left out, so
    that `{% if verbose %}--verbose{% endif %}` is one argument or none; an empty
    value alone stays, as an empty argument.
    """
    rendered = ((template, render(template, values)) for template in templates)
    return [text for template, text in rendered if text or "{%" not in template]


def _shown(name: str) -> str:
    return f"{{{{ {name} }}}}"


class _Table(BaseModel):
    """A table from `[ezhpcy]` down: frozen, and with no unknown keys."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Environment(_Table):
    """The app's Pixi environment, installed from the locked files."""

    # Relative to the manifest; absolute once loaded.
    pixi: Path
    environment: str = Field(default="default", min_length=1)

    @property
    def lock(self) -> Path:
        """`pixi.lock`, beside the Pixi manifest."""
        return self.pixi.with_name(_LOCK_FILE_NAME)

    @field_validator("pixi")
    @classmethod
    def resolve_pixi(cls, value: Path, info: ValidationInfo) -> Path:
        pixi = (_directory(info) / value).resolve()
        if pixi.name not in _PIXI_MANIFEST_NAMES:
            raise ValueError(
                f"must name a pixi.toml or pyproject.toml, not {value.name!r}"
            )
        if not pixi.is_file():
            raise ValueError(f"no such file: {pixi}")
        if pixi.name == "pyproject.toml":
            with pixi.open("rb") as file:
                if "pixi" not in tomllib.load(file).get("tool", {}):
                    raise ValueError(f"{pixi} has no [tool.pixi] table")
        return pixi

    @model_validator(mode="after")
    def check_lock(self) -> Self:
        if not self.lock.is_file():
            raise ValueError(
                f"{self.lock} does not exist; lock the environment with `pixi lock` "
                "beside the Pixi manifest"
            )
        _check_lock(self.lock.read_text(encoding="utf-8"), self.environment)
        return self


def _check_lock(text: str, environment: str) -> None:
    """
    Check the lock file has packages for `environment` on the cluster's platform,
    none of them from a path. No YAML parser is among the dependencies, so the
    lines are scanned by indentation, for the layout Pixi writes:

        version: 6
        environments:
          default:
            packages:
              linux-64:
              - conda: https://…
              - pypi: ./
    """
    lines = text.splitlines()
    version = next((line.split(":", 1)[1].strip() for line in lines[:1]), "")
    if not version.isdigit() or int(version) < _MIN_LOCK_VERSION:
        raise ValueError(
            f"pixi.lock has version {version or '(none)'}; lock it again with a "
            f"recent Pixi (lock file version {_MIN_LOCK_VERSION} or later)"
        )
    environments: dict[str, dict[str, list[str]]] = {}
    section = environment_name = platform = None
    in_packages = False
    for line in lines:
        indent = len(line) - len(line.lstrip(" "))
        content = line.strip()
        if not content:
            continue
        if indent == 0:
            section = content
            continue
        if section != "environments:":
            continue
        if indent == 2 and content.endswith(":"):
            environment_name, platform = content[:-1], None
            environments[environment_name] = {}
        elif indent == 4:
            platform = None
            in_packages = content == "packages:"
        elif indent == 6 and content.endswith(":") and in_packages:
            platform = content[:-1]
            assert environment_name is not None
            environments[environment_name][platform] = []
        elif platform is not None and (match := _PACKAGE.fullmatch(content)):
            assert environment_name is not None
            environments[environment_name][platform].append(match["location"])
    if environment not in environments:
        known = ", ".join(sorted(environments)) or "none"
        raise ValueError(
            f"pixi.lock has no environment {environment!r} (it has: {known})"
        )
    packages = environments[environment].get(_PLATFORM)
    if not packages:
        raise ValueError(
            f"pixi.lock has no packages for {_PLATFORM} in environment "
            f"{environment!r}; add {_PLATFORM} to the workspace's platforms and "
            "lock again"
        )
    if paths := [location for location in packages if "://" not in location]:
        raise ValueError(
            f"environment {environment!r} has path dependencies ({', '.join(paths)}), "
            "which EzHPCy can't install on the cluster yet"
        )


_PACKAGE = re.compile(r"- (?:conda|pypi): (?P<location>.+)")


def _directory(info: ValidationInfo) -> Path:
    """The manifest's directory, which `Manifest.load` gives as context."""
    context = info.context
    if not isinstance(context, dict) or "directory" not in context:
        raise ValueError("manifests are loaded with `Manifest.load`")
    return context["directory"]


@dataclass(frozen=True, slots=True)
class AssetFile:
    """One asset: where it goes under the assets directory, and where it is here."""

    relative: PurePosixPath
    path: Path
    executable: bool


class Assets(_Table):
    """Files uploaded beside the environment, by glob, relative to the manifest."""

    include: tuple[str, ...] = Field(min_length=1)
    exclude: tuple[str, ...] = ()
    _files: tuple[AssetFile, ...] = PrivateAttr(default=())

    @property
    def files(self) -> tuple[AssetFile, ...]:
        """The matched files, sorted by their relative path."""
        return self._files

    @model_validator(mode="after")
    def match_files(self, info: ValidationInfo) -> Self:
        self._files = _match_assets(_directory(info), self.include, self.exclude)
        return self


def _match_assets(
    directory: Path, include: Sequence[str], exclude: Sequence[str]
) -> tuple[AssetFile, ...]:
    for pattern in (*include, *exclude):
        parts = PurePosixPath(pattern).parts
        if not parts or PurePosixPath(pattern).is_absolute() or ".." in parts:
            raise ValueError(
                f"pattern {pattern!r} must be relative to the manifest's directory, "
                "without '..'"
            )
    root = directory.resolve()
    files: dict[PurePosixPath, Path] = {}
    for pattern in include:
        matched = [path for path in directory.glob(pattern) if path.is_file()]
        if not matched:
            raise ValueError(f"pattern {pattern!r} matches no files")
        for path in matched:
            relative = PurePosixPath(path.relative_to(directory).as_posix())
            if any(relative.full_match(excluded) for excluded in exclude):
                continue
            if not path.resolve().is_relative_to(root):
                raise ValueError(
                    f"{relative} is outside the manifest's directory once symbolic "
                    "links are resolved"
                )
            files[relative] = path
    return tuple(
        AssetFile(relative, path, _executable(path))
        for relative, path in sorted(files.items())
    )


def _executable(path: Path) -> bool:
    # On Windows, only by extension (`.exe`, `.bat`, …), as `os.stat` says.
    return bool(path.stat().st_mode & stat.S_IXUSR)


def _check_env_name(name: str) -> str:
    if not _ENV_NAME.fullmatch(name):
        raise ValueError(f"{name!r} is not an environment variable name")
    if name.startswith("EZHPCY_"):
        raise ValueError(f"{name} is reserved for EzHPCy's own variables")
    return name


class Command(_Table):
    """A command run on the host, never through a shell."""

    command: tuple[str, ...] = Field(min_length=1)
    env: dict[Annotated[str, BeforeValidator(_check_env_name)], str] = Field(
        default_factory=dict
    )

    @field_validator("command", "env")
    @classmethod
    def reject_nul(cls, value: tuple[str, ...] | dict[str, str]) -> object:
        if any(
            "\0" in item
            for item in (value.values() if isinstance(value, dict) else value)
        ):
            raise ValueError("must not contain NUL")
        return value


class Run(Command):
    """What runs on the host as the app."""

    # Default: `<apps root>/work/<normalised name>/`.
    workdir: PurePosixPath | None = None

    @field_validator("workdir")
    @classmethod
    def require_absolute(cls, value: PurePosixPath | None) -> PurePosixPath | None:
        if value is not None and not value.is_absolute():
            raise ValueError("must be an absolute path on the host")
        return value


class Endpoint(_Table):
    """Where the app listens on its host."""

    # A Unix socket in the run directory, or `127.0.0.1:<port>`.
    transport: Literal["socket", "tcp"]
    # Whether the app checks its clients itself (e.g. Jupyter's token).
    authenticates: bool = False


class Ready(_Table):
    """How EzHPCy tells the app is ready, through its host's connection."""

    # `tcp`: the channel opens. `banner`: the first bytes start with `prefix`.
    # `http`: `GET path` answers with a status in `status`.
    check: Literal["tcp", "banner", "http"] = "tcp"
    path: str = Field(default="/", pattern=r"^/")
    prefix: str | None = Field(default=None, min_length=1)
    # The lowest and highest status that count as ready.
    status: tuple[int, int] = (200, 399)
    # From when the launch script reports the app started.
    timeout: Seconds = Field(default=timedelta(seconds=120), gt=timedelta(0))

    def accepts(self, status: int) -> bool:
        low, high = self.status
        return low <= status <= high

    @model_validator(mode="after")
    def check_fields(self) -> Self:
        given = self.model_fields_set
        if self.check != "http" and (wrong := sorted(given & {"path", "status"})):
            raise ValueError(f"{', '.join(wrong)} only apply to check = 'http'")
        if self.check == "banner" and self.prefix is None:
            raise ValueError("check = 'banner' needs a prefix")
        if self.check != "banner" and "prefix" in given:
            raise ValueError("prefix only applies to check = 'banner'")
        low, high = self.status
        if not 100 <= low <= high <= 599:
            raise ValueError("status must be [lowest, highest], within 100–599")
        return self


class Access(_Table):
    """How the user reaches the app on this machine."""

    url: str = "http://127.0.0.1:{{ local_port }}/"
    # Lines shown under the URL in a tunnel's status, such as a command that
    # mounts it; they take the URL's placeholders.
    status_hints: tuple[str, ...] = ()
    # 0 lets the OS pick a free port.
    local_port: int = Field(default=0, ge=0, le=65535)


class Parameter(_Table):
    """
    One value an app takes as it starts (an argument), or an action as it runs (an
    input), by `type`; a frontend asks for it as its type suggests. A `path` is an
    absolute path on the app's host, not on this machine.
    """

    type: Literal["string", "integer", "number", "boolean", "choice", "path"]
    label: str | None = Field(default=None, min_length=1)
    # A `path`'s may use `{{ workdir }}` and `{{ assets }}`, and an input's the
    # arguments, rendered in `RunningApp.actions`.
    default: bool | int | float | str | None = None
    # If not, it may have no value: `{% if name is defined %}` tells.
    required: bool = True
    # Bounds of an `integer` or a `number`, inclusive.
    min: int | float | None = None
    max: int | float | None = None
    # What a `choice` chooses from.
    choices: tuple[str, ...] = ()
    # What a `path` must be, if it exists: by default either.
    kind: Literal["file", "directory"] | None = None
    must_exist: bool = False

    def parse(self, text: str) -> object:
        """
        What a user typed, as a value of its type, for `check`. Raises `ValueError`
        if it can't be one.
        """
        try:
            match self.type:
                case "integer":
                    return int(text)
                case "number":
                    return float(text)
                case "boolean":
                    return _BOOLEANS[text.strip().lower()]
                case _:
                    return text
        except KeyError, ValueError:
            raise ValueError(f"{text!r} is not {_A_TYPE[self.type]}") from None

    def check(self, value: object) -> object:
        """
        `value`, checked as far as can be without its host, as its placeholder's
        value, which a template may compare or calculate with. Raises `ValueError`,
        saying why it can't be.
        """
        match self.type:
            case "integer" if isinstance(value, int) and not isinstance(value, bool):
                self._check_bounds(value)
                return value
            case "number" if (
                isinstance(value, int | float)
                and not isinstance(value, bool)
                and math.isfinite(value)
            ):
                self._check_bounds(value)
                return value
            case "boolean" if isinstance(value, bool):
                return value
            case "string" if isinstance(value, str):
                text = value
            case "choice" if isinstance(value, str):
                if value not in self.choices:
                    raise ValueError(f"{value!r} is none of {list(self.choices)}")
                text = value
            case "path" if isinstance(value, str | PurePosixPath):
                if not PurePosixPath(value).is_absolute():
                    raise ValueError(f"{str(value)!r} is not an absolute path")
                text = str(value)
            case _:
                raise ValueError(f"{value!r} is not {_A_TYPE[self.type]}")
        if "\0" in text:
            raise ValueError("must not contain NUL")
        return text

    def _check_bounds(self, value: float) -> None:
        if self.min is not None and value < self.min:
            raise ValueError(f"{value} is less than the minimum, {self.min}")
        if self.max is not None and value > self.max:
            raise ValueError(f"{value} is more than the maximum, {self.max}")

    @model_validator(mode="after")
    def check_fields(self) -> Self:
        for fields, types in _PARAMETER_FIELDS.items():
            if self.type not in types and (wrong := fields & self.model_fields_set):
                raise ValueError(
                    f"{', '.join(sorted(wrong))} only apply to type = "
                    + " or ".join(repr(name) for name in sorted(types))
                )
        for bound in (self.min, self.max):
            if self.type == "integer" and isinstance(bound, float):
                raise ValueError("min and max of an integer must be integers")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min must not be more than max")
        if self.type == "choice":
            if not self.choices:
                raise ValueError("type = 'choice' needs choices")
            if len(set(self.choices)) != len(self.choices):
                raise ValueError("choices must differ")
        if not self.required and self.default is not None:
            raise ValueError("required = false is for a value with no default")
        # A path's may have placeholders, which `App` checks.
        if self.default is not None and self.type != "path":
            try:
                self.check(self.default)
            except ValueError as error:
                raise ValueError(f"default: {error}") from None
        return self


_A_TYPE = {
    "string": "a string",
    "integer": "an integer",
    "number": "a number",
    "boolean": "true or false",
    "choice": "a string",
    "path": "a path",
}
_BOOLEANS = {"true": True, "yes": True, "false": False, "no": False}
_PARAMETER_FIELDS = {
    frozenset({"min", "max"}): frozenset({"integer", "number"}),
    frozenset({"choices"}): frozenset({"choice"}),
    frozenset({"kind", "must_exist"}): frozenset({"path"}),
}


def _check_parameter_name(name: object) -> object:
    if isinstance(name, str):
        # `self` is what templates are rendered with a method of.
        if not name.isidentifier() or name == "self":
            raise ValueError(f"{name!r} can't be a placeholder's name")
        if name in _ALL_PLACEHOLDERS:
            raise ValueError(f"{name!r} is the name of a built-in placeholder")
    return name


type _Parameters = dict[
    Annotated[str, BeforeValidator(_check_parameter_name)], Parameter
]


class Trigger(_Table):
    """What runs an action: the user, a timer, or both."""

    manual: bool = True
    # Timers run in the client, while the app runs. A template takes the app's
    # arguments, and gives seconds.
    every: Seconds | str | None = None

    @field_validator("every")
    @classmethod
    def check_every(cls, value: timedelta | str | None) -> timedelta | str | None:
        if isinstance(value, str) and "{{" not in value:
            raise ValueError("must be a number of seconds, or a template giving one")
        if isinstance(value, timedelta) and value <= timedelta(0):
            raise ValueError("must be more than 0 seconds")
        return value

    @model_validator(mode="after")
    def check_some_trigger(self) -> Self:
        if not self.manual and self.every is None:
            raise ValueError("needs manual = true, or every")
        return self


def _rendered_every(template: str, values: Mapping[str, object]) -> timedelta:
    text = render(template, values)
    try:
        seconds = float(text)
    except ValueError:
        seconds = 0.0
    if not (math.isfinite(seconds) and seconds > 0):
        raise ValueError(
            f"trigger.every gives {text!r}, not a number of seconds above 0"
        )
    return timedelta(seconds=seconds)


class Action(_Table):
    """A command run on an app's host, in its environment, while it runs."""

    description: str | None = Field(default=None, min_length=1)
    run: Command
    trigger: Trigger = Field(default_factory=Trigger)
    input: _Parameters = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_timer_has_values(self) -> Self:
        if self.trigger.every is not None:
            for name, parameter in self.input.items():
                if parameter.required and parameter.default is None:
                    raise ValueError(
                        f"input {name!r} needs a default, as a timer asks nobody"
                    )
        return self

    def templates(self) -> Iterator[tuple[str, str, str]]:
        """Its fields that take placeholders: (field, path, template)."""
        for index, argument in enumerate(self.run.command):
            yield "action.run.command", f"run.command[{index}]", argument
        for name, value in self.run.env.items():
            yield "action.run.env", f"run.env.{name}", value

    def rendered(self, values: Mapping[str, object]) -> Action:
        """
        It as a run of its app has it, from `values`: its `path` inputs' defaults
        and its timer's interval rendered.
        """
        inputs = {
            name: parameter.model_copy(
                update={"default": render(str(parameter.default), values)}
            )
            if parameter.type == "path" and parameter.default is not None
            else parameter
            for name, parameter in self.input.items()
        }
        trigger = self.trigger
        if isinstance(trigger.every, str):
            every = _rendered_every(trigger.every, values)
            trigger = trigger.model_copy(update={"every": every})
        return self.model_copy(update={"input": inputs, "trigger": trigger})


def _check_action_name(name: object) -> object:
    if isinstance(name, str) and not _BARE_KEY.fullmatch(name):
        raise ValueError(
            f"invalid action name {name!r}: letters, digits, '_' and '-' only"
        )
    return name


class _Entry(_Table):
    """
    One version of an app or a link, `[ezhpcy.app.<name>."<version>"]`, frozen.
    Shown as `name version`; `normalised_name` is the name compared, and used
    in paths.
    """

    description: str | None = Field(default=None, min_length=1)
    _name: str = PrivateAttr(default="")
    _version: str = PrivateAttr(default="")

    @property
    def name(self) -> str:
        """The app's name, as the manifest spells it."""
        return self._name

    @property
    def version(self) -> str:
        """The version's key, as the manifest spells it: `v1.2.3`."""
        return self._version

    @property
    def normalised_name(self) -> str:
        """The name as names are compared (PEP 503): `My_App` is `my-app`."""
        return canonicalize_name(self._name)

    def __str__(self) -> str:
        return f"{self._name} {self._version}"

    def templates(self) -> Iterator[tuple[str, str, str]]:
        """Every field that takes placeholders: (field, path, template)."""
        raise NotImplementedError

    def available(self) -> set[str]:
        """The placeholders that have values for it."""
        raise NotImplementedError

    def unavailable(self, name: str) -> str:
        """Why the placeholder `name` has no value for it."""
        raise NotImplementedError

    def own_placeholders(self) -> set[str]:
        """The placeholders it defines itself."""
        return set()

    def placeholder_problem(
        self, field: str, name: str, own: Collection[str]
    ) -> str | None:
        """What is wrong with `field` using the placeholder `name`, if anything."""
        if name in own:
            return None
        if name not in _ALL_PLACEHOLDERS:
            return f"unknown placeholder {_shown(name)}"
        if name not in FIELD_PLACEHOLDERS[field]:
            return f"{field} does not take {_shown(name)}"
        if name not in self.available():
            return f"{_shown(name)} {self.unavailable(name)}"
        return None

    def placeholder_problems(self) -> Iterator[tuple[str, str]]:
        """Each field whose placeholders are wrong: (path, why)."""
        for field, path, template in self.templates():
            try:
                names = placeholders(template)
            except ValueError as error:
                yield path, str(error)
                continue
            for name in sorted(names):
                problem = self.placeholder_problem(field, name, self.own_placeholders())
                if problem is not None:
                    yield path, problem


class App(_Entry):
    """
    An app: a server run on a host, and forwarded to this machine, with actions
    if it has any; or actions alone, without `run` and `endpoint`.
    """

    environment: Environment
    assets: Assets | None = None
    run: Run | None = None
    endpoint: Endpoint | None = None
    ready: Ready = Field(default_factory=Ready)
    access: Access = Field(default_factory=Access)
    argument: _Parameters = Field(default_factory=dict)
    action: dict[Annotated[str, BeforeValidator(_check_action_name)], Action] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def check_server_or_actions(self) -> Self:
        if (self.run is None) != (self.endpoint is None):
            raise ValueError("run and endpoint go together: give both, or neither")
        if self.run is None:
            if not self.action:
                raise ValueError("needs run and endpoint, or an action")
            if wrong := sorted(self.model_fields_set & {"ready", "access"}):
                raise ValueError(f"{', '.join(wrong)} only apply with run and endpoint")
        return self

    def checked_arguments(self, given: Mapping[str, object]) -> dict[str, object]:
        """
        The values of its arguments for a run: those `given`, checked as far as can
        be without its host, and the defaults, but for those of `path`s, which only
        the host can render. Raises `ManifestError` for a value it can't take, so
        that a frontend can tell before connecting.
        """
        for unknown in given.keys() - self.argument.keys():
            raise ManifestError(
                t"app {self!s:name} has no argument {unknown:value}; choose from: "
                t"{sorted(self.argument):choice}"
            )
        values: dict[str, object] = {}
        for name, parameter in self.argument.items():
            value = given.get(name, parameter.default)
            if value is None:
                if parameter.required:
                    raise ManifestError(
                        t"app {self!s:name} needs a value for argument {name:name}"
                    )
            elif name in given or parameter.type != "path":
                try:
                    values[name] = parameter.check(value)
                except ValueError as error:
                    raise ManifestError(
                        t"argument {name:name} of app {self!s:name}: {error}"
                    ) from error
        for name, action in self.action.items():
            if isinstance(every := action.trigger.every, str):
                try:
                    _rendered_every(every, values)
                except ValueError as error:
                    raise ManifestError(
                        t"action {name:name} of app {self!s:name}: {error}"
                    ) from error
        return values

    def own_placeholders(self) -> set[str]:
        return set(self.argument)

    def placeholder_problems(self) -> Iterator[tuple[str, str]]:
        yield from super().placeholder_problems()
        for name, action in self.action.items():
            for path, why in self._action_problems(action):
                yield f"action.{name}.{path}", why
        for name, parameter in self.argument.items():
            if name not in self._names_used():
                yield f"argument.{name}", "is not used by anything"
            if parameter.type == "path" and parameter.default is not None:
                yield from self._path_default_problems(
                    f"argument.{name}.default", str(parameter.default), ()
                )

    def _names_used(self) -> set[str]:
        """Every placeholder its templates that can be read use."""
        templates = [template for _field, _path, template in self.templates()]
        for action in self.action.values():
            templates += [template for _field, _path, template in action.templates()]
            if isinstance(action.trigger.every, str):
                templates.append(action.trigger.every)
            templates += [
                str(parameter.default)
                for parameter in action.input.values()
                if parameter.type == "path" and parameter.default is not None
            ]
        used: set[str] = set()
        for template in templates:
            try:
                used |= placeholders(template)
            except ValueError:
                # Reported as what it is, not as what it might have used.
                return set(self.argument)
        return used

    def _action_problems(self, action: Action) -> Iterator[tuple[str, str]]:
        """
        As `placeholder_problems`, for one action, whose inputs are placeholders
        too, each of which it must use.
        """
        own = self.own_placeholders() | action.input.keys()
        used: set[str] = set()
        readable = True
        for field, path, template in action.templates():
            try:
                names = placeholders(template)
            except ValueError as error:
                readable = False
                yield path, str(error)
                continue
            used |= names
            for name in sorted(names):
                if (problem := self.placeholder_problem(field, name, own)) is not None:
                    yield path, problem
        if isinstance(every := action.trigger.every, str):
            try:
                for name in sorted(placeholders(every)):
                    problem = self.placeholder_problem(
                        "action.trigger.every", name, self.own_placeholders()
                    )
                    if problem is not None:
                        yield "trigger.every", problem
            except ValueError as error:
                yield "trigger.every", str(error)
        for name, parameter in action.input.items():
            if name in self.argument:
                yield f"input.{name}", "is the name of an argument of the app"
            if readable and name not in used:
                yield f"input.{name}", "is not used by run.command or run.env"
            if parameter.type == "path" and parameter.default is not None:
                yield from self._path_default_problems(
                    f"input.{name}.default",
                    str(parameter.default),
                    self.own_placeholders(),
                )

    def _path_default_problems(
        self, path: str, default: str, own: Collection[str]
    ) -> Iterator[tuple[str, str]]:
        try:
            names = placeholders(default)
        except ValueError as error:
            yield path, str(error)
            return
        allowed = _PATH_DEFAULT_PLACEHOLDERS | set(own)
        for name in sorted(names):
            if name not in allowed:
                yield path, f"a path's default does not take {_shown(name)}"
            elif name not in own and name not in self.available():
                yield path, f"{_shown(name)} {self.unavailable(name)}"
        if names <= allowed:
            rendered = render(default, dict.fromkeys(names, "/absolute"))
            if not PurePosixPath(rendered).is_absolute():
                yield path, "must be an absolute path on the host"

    def templates(self) -> Iterator[tuple[str, str, str]]:
        if self.run is None:
            return
        for index, argument in enumerate(self.run.command):
            yield "run.command", f"run.command[{index}]", argument
        for name, value in self.run.env.items():
            yield "run.env", f"run.env.{name}", value
        yield from _access_templates(self.access)

    def available(self) -> set[str]:
        available = set(_ALL_PLACEHOLDERS) - {"alias"}
        if self.endpoint is None:
            available -= {"socket", "port", "token", "local_port"}
        else:
            available.discard(
                "port" if self.endpoint.transport == "socket" else "socket"
            )
        if self.assets is None:
            available.discard("assets")
        return available

    def unavailable(self, name: str) -> str:
        if self.endpoint is None and name in {"socket", "port", "token", "local_port"}:
            return "is not set without run and endpoint"
        match name:
            case "socket" | "port":
                assert self.endpoint is not None
                return (
                    f"is not set for endpoint.transport = {self.endpoint.transport!r}"
                )
            case "alias":
                return "is set only for links, which have no [run]"
            case _:
                return "is not set without [assets]"


class LinkAccess(_Table):
    """Where a link points."""

    url: str
    # As an app's (`Access`).
    status_hints: tuple[str, ...] = ()


class Link(_Entry):
    """
    A link: an app with nothing to run, only a URL, such as one that opens the
    host its tunnel publishes in an editor (`{{ alias }}`, the Host alias).
    """

    access: LinkAccess

    def templates(self) -> Iterator[tuple[str, str, str]]:
        yield from _access_templates(self.access)

    def available(self) -> set[str]:
        return {"alias"}

    def unavailable(self, name: str) -> str:
        return "is not set for a link, which runs nothing"

    def url(self, alias: str) -> str:
        """Its URL, for the host published under the Host alias `alias`."""
        return render(self.access.url, {"alias": alias})

    def status_hints(self, alias: str) -> tuple[str, ...]:
        """Its status hints, for the host published under the Host alias `alias`."""
        return tuple(
            render(hint, {"alias": alias}) for hint in self.access.status_hints
        )


def _access_templates(access: Access | LinkAccess) -> Iterator[tuple[str, str, str]]:
    yield "access.url", "access.url", access.url
    for index, hint in enumerate(access.status_hints):
        yield "access.status_hints", f"access.status_hints[{index}]", hint


# The keys that make an entry an app rather than a link.
_APP_KEYS = frozenset({"environment", "run", "endpoint"})
# Tags in validation errors' locations, which `_location` leaves out. Never a
# name or a version, which have no `<`.
_APP_TAG, _LINK_TAG = "<app>", "<link>"


def _entry_kind(value: object) -> str:
    if isinstance(value, dict):
        return _APP_TAG if value.keys() & _APP_KEYS else _LINK_TAG
    return _LINK_TAG if isinstance(value, Link) else _APP_TAG


type Entry = Annotated[
    Annotated[App, Tag(_APP_TAG)] | Annotated[Link, Tag(_LINK_TAG)],
    Discriminator(_entry_kind),
]


def _check_app_name(name: object) -> object:
    if isinstance(name, str) and not _APP_NAME.fullmatch(name):
        raise ValueError(
            f"invalid app name {name!r}: names are those of Python packages, "
            "letters, digits, '.', '_' and '-', starting and ending with a letter "
            "or digit"
        )
    return name


def parse_version(version: str) -> Version:
    """
    An app's version, `v` and a PEP 440 version, as a PEP 440 version. Raises
    `ValueError` for anything else.
    """
    try:
        if _APP_VERSION.fullmatch(version):
            return Version(version[1:])
    except InvalidVersion:
        pass
    raise ValueError(
        f"invalid version {version!r}: versions are 'v' and a PEP 440 version, "
        'such as "v1.0", "v2.1.3" or "v2.0rc1"'
    )


def _check_app_version(version: object) -> object:
    if isinstance(version, str):
        parse_version(version)
    return version


type AppName = Annotated[str, BeforeValidator(_check_app_name)]
type AppVersion = Annotated[str, BeforeValidator(_check_app_version)]


class EzhpcyTable(_Table):
    """`[ezhpcy]`: EzHPCy's part of the file."""

    # `schema` would shadow `BaseModel.schema`.
    schema_version: Literal[1] = Field(alias="schema")
    app: dict[AppName, dict[AppVersion, Entry]]

    @field_validator("schema_version", mode="before")
    @classmethod
    def check_schema(cls, value: object) -> object:
        if value != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema {value!r}; this version of EzHPCy reads "
                f"schema = {SCHEMA_VERSION}"
            )
        return value

    @field_validator("app")
    @classmethod
    def name_apps(
        cls, apps: dict[str, dict[str, Entry]]
    ) -> dict[str, dict[str, Entry]]:
        names: dict[str, str] = {}
        for name, versions in apps.items():
            normalised = canonicalize_name(name)
            if (other := names.setdefault(normalised, name)) != name:
                raise ValueError(
                    f"apps {other!r} and {name!r} are one app: names are compared "
                    f"normalised, as {normalised!r}"
                )
            seen: dict[Version, str] = {}
            for version, app in versions.items():
                parsed = parse_version(version)
                if (equal := seen.setdefault(parsed, version)) != version:
                    raise ValueError(
                        f"app {name!r} has versions {equal!r} and {version!r}, "
                        "which are one version"
                    )
                app._name, app._version = name, version
        return apps


class ManifestFile(BaseModel):
    """The whole file: other tools' tables are ignored."""

    model_config = ConfigDict(extra="ignore")

    ezhpcy: EzhpcyTable


@dataclass(frozen=True)
class Manifest:
    """
    A loaded manifest: its absolute `path`, and its apps, by normalised name,
    then by version as written.
    """

    path: Path
    apps: Mapping[str, Mapping[str, Entry]]

    @classmethod
    def load(cls, path: Path) -> Manifest:
        """
        Read and check the manifest at `path`, a file, or a directory holding
        `ezhpcy-apps.toml`. Raises `ManifestError`, listing every problem found.
        """
        if path.is_dir():
            path = path / MANIFEST_FILE_NAME
        path = path.resolve()
        try:
            with path.open("rb") as file:
                data = tomllib.load(file)
        except OSError as error:
            raise ManifestError(
                t"Could not read manifest {path:path}: {error}"
            ) from error
        except tomllib.TOMLDecodeError as error:
            raise ManifestError(t"{path:path} is not valid TOML: {error}") from error
        _check_layout(path, data)
        try:
            manifest = ManifestFile.model_validate(
                data, context={"directory": path.parent}
            )
        except ValidationError as error:
            raise _invalid(path, _validation_problems(error)) from error
        apps = manifest.ezhpcy.app
        problems = [
            (_location(("ezhpcy", "app", app.name, app.version)) + f".{field}", why)
            for versions in apps.values()
            for app in versions.values()
            for field, why in app.placeholder_problems()
        ]
        if problems:
            raise _invalid(path, problems)
        return cls(
            path,
            {
                canonicalize_name(name): dict(versions)
                for name, versions in apps.items()
            },
        )


def _check_layout(path: Path, data: dict[str, object]) -> None:
    """The file's outline, with a message of its own for each way to miss it."""
    ezhpcy = data.get("ezhpcy")
    if not isinstance(ezhpcy, dict):
        raise ManifestError(
            t"{path:path} has no {'[ezhpcy]':name} table; EzHPCy reads apps from "
            t"{'[ezhpcy.app.<name>."<version>"]':name} tables, beside "
            t"{'schema = 1':suggestion} in {'[ezhpcy]':name}"
        )
    apps = ezhpcy.get("app")
    if not isinstance(apps, dict) or not any(
        not isinstance(versions, dict) or versions for versions in apps.values()
    ):
        raise ManifestError(
            t"{path:path} has no apps; describe each version of each in a table "
            t"{'[ezhpcy.app.<name>."<version>"]':name}, such as "
            t"{'[ezhpcy.app.lab."v1.0"]':suggestion}"
        )
    for name, versions in apps.items():
        if isinstance(versions, dict) and set(versions) & (
            set(App.model_fields) | set(Link.model_fields)
        ):
            raise ManifestError(
                t"{path:path}: app {name:name} has no version; each version of an "
                t"app is a table of its own, such as "
                t"{f'[ezhpcy.app.{name}."v1.0"]':suggestion}"
            )


def _validation_problems(error: ValidationError) -> list[tuple[str, str]]:
    problems = []
    for details in error.errors(include_url=False):
        location = _location(details["loc"])
        if details["type"] == "extra_forbidden":
            message = "unknown key"
        elif details["type"] == "value_error":
            message = str(details["ctx"]["error"])
        else:
            message = details["msg"]
        problems.append((location, message))
    return problems


def _location(loc: Iterable[int | str]) -> str:
    """A field's path as written in TOML: `ezhpcy.app.lab."v1.0".run.command[2]`."""
    location = ""
    for part in loc:
        if isinstance(part, int):
            location += f"[{part}]"
        # Pydantic's marker for a dict's key, which the path names already, and
        # the tag of an app or a link.
        elif part not in {"[key]", _APP_TAG, _LINK_TAG}:
            key = part if _BARE_KEY.fullmatch(part) else f'"{part}"'
            location += f".{key}" if location else key
    return location


def _invalid(path: Path, problems: list[tuple[str, str]]) -> ManifestError:
    message = t"invalid manifest {path:path}:"
    for location, why in problems:
        message += t"\n  {location:name}: {why}"
    return ManifestError(message)


def collect_apps(
    manifests: Sequence[Manifest], defaults: Sequence[Manifest] = ()
) -> dict[str, dict[str, Entry]]:
    """
    The apps of `manifests` and of a front end's `defaults`, by normalised
    name, then by version as written. A version of an app in one of `manifests`
    replaces the same version in `defaults`. Raises `ManifestError` for the same
    version of an app in two of `manifests`, or in two of `defaults`.
    """
    found: dict[tuple[str, Version], tuple[Entry, Manifest, bool]] = {}
    for manifest, default in [
        *((manifest, True) for manifest in defaults),
        *((manifest, False) for manifest in manifests),
    ]:
        for name, versions in manifest.apps.items():
            for version, app in versions.items():
                key = (name, parse_version(version))
                if key in found:
                    other, source, other_default = found[key]
                    if other_default == default:
                        raise ManifestError(
                            t"{str(other):name} of {source.path:path} and "
                            t"{str(app):name} of {manifest.path:path} are one "
                            t"version of one app"
                        )
                    get_logger(__name__).debug(
                        t"App {str(app):name} of {manifest.path:path} replaces the "
                        t"default one of {source.path:path}."
                    )
                found[key] = (app, manifest, default)
    apps: dict[str, dict[str, Entry]] = {}
    for (name, _version), (app, _manifest, _default) in found.items():
        apps.setdefault(name, {})[app.version] = app
    return apps


def builtin_manifests() -> list[Path]:
    """The manifests EzHPCy ships, one directory each in `ezhpcy/app/builtins/`."""
    builtins = Path(__file__).parent / "builtins"
    return sorted(path.parent for path in builtins.glob(f"*/{MANIFEST_FILE_NAME}"))


def choose_app(
    apps: Mapping[str, Mapping[str, Entry]], name: str, version: str | None = None
) -> Entry:
    """
    The app called `name`, compared normalised, at `version`, compared as PEP
    440 versions; or without `version`, its newest that isn't a pre-release.
    Raises `ManifestError`, listing what there is to choose from.
    """
    versions = apps.get(canonicalize_name(name))
    if not versions:
        if not apps:
            raise ManifestError(
                t"there is no app {name:value}: no manifest with apps was given"
            )
        raise ManifestError(
            t"there is no app {name:value}; choose from: {sorted(apps):choice}"
        )
    choices = sorted(versions.values(), key=lambda app: parse_version(app.version))
    if version is not None:
        try:
            wanted = parse_version(version)
        except ValueError as error:
            raise ManifestError(t"{error}") from None
        for app in choices:
            if parse_version(app.version) == wanted:
                return app
        raise ManifestError(
            t"app {name:value} has no version {version:value}; choose from: "
            t"{[app.version for app in choices]:choice}"
        )
    finals = [app for app in choices if not parse_version(app.version).is_prerelease]
    if not finals:
        raise ManifestError(
            t"app {name:value} has only pre-releases, which are chosen only by "
            t"version: {[f'{name}@{app.version}' for app in choices]:choice}"
        )
    return finals[-1]


def environment_key(environment: Environment) -> str:
    """The environment's identity: its Pixi manifest, lock file and name."""
    return _digest(
        environment.pixi.name.encode(),
        environment.pixi.read_bytes(),
        environment.lock.read_bytes(),
        environment.environment.encode(),
    )


def assets_key(files: Sequence[AssetFile]) -> str:
    """The asset tree's identity: each file's path, executable bit and content."""
    fields = []
    for file in sorted(files, key=lambda file: file.relative):
        fields += [
            str(file.relative).encode(),
            b"x" if file.executable else b"-",
            file.path.read_bytes(),
        ]
    return _digest(*fields)


def _digest(*fields: bytes) -> str:
    # Each field is length-prefixed, so that no two lists of fields hash alike.
    hasher = hashlib.sha256()
    for field in fields:
        hasher.update(len(field).to_bytes(8, "big"))
        hasher.update(field)
    return hasher.hexdigest()
