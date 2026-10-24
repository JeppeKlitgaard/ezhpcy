"""
The apps manifest: a TOML file on this machine whose `[ezhpcy]` table describes
apps, one table per version of each, `[ezhpcy.app.<name>."<version>"]`. An app
(`App`) runs on a host; a table without `environment`, `run` or `endpoint` is a
link (`Link`) instead, which runs nothing and has only a URL. Every
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
"""

import re
import string
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Literal, Self
lazy import hashlib
lazy import stat
lazy import tomllib
lazy from collections.abc import Iterable, Iterator, Mapping, Sequence
lazy from pathlib import Path, PurePosixPath
lazy from typing import Any

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
lazy from packaging.utils import canonicalize_name
lazy from packaging.version import InvalidVersion, Version
lazy from pydantic import ValidationError

from ezhpcy.types import PositiveByteSize, Seconds, TimeLimit
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
# The placeholders each field takes. `{token}` is not in `run.command`: argv can
# be read by other users on the host.
FIELD_PLACEHOLDERS = {
    "run.command": _HOST_PLACEHOLDERS,
    "run.env": _HOST_PLACEHOLDERS | {"token"},
    "access.url": frozenset({"token", "local_port", "alias"}),
    "access.status_hints": frozenset({"token", "local_port", "alias"}),
}
_ALL_PLACEHOLDERS = frozenset(ENVIRONMENT_VARIABLES) | {"local_port", "alias"}


def placeholders(template: str) -> set[str]:
    """
    The placeholders in `template`. Raises `ValueError` for broken braces, and for
    anything but a bare name: `{}`, `{0}`, `{a.b}`, `{a!r}`, `{a:>5}`.
    """
    names = set()
    for _literal, name, format_spec, conversion in _parse(template):
        if name is None:
            continue
        if not name.isidentifier():
            raise ValueError(f"{{{name}}} is not a placeholder name")
        if format_spec or conversion:
            raise ValueError(f"placeholder {{{name}}} must not have a format")
        names.add(name)
    return names


def render(template: str, values: Mapping[str, str]) -> str:
    """
    Fill `template`'s placeholders from `values`, with `{{` and `}}` as literal
    braces. Every field that takes placeholders is rendered here.
    """
    rendered = []
    for literal, name, _format_spec, _conversion in _parse(template):
        rendered.append(literal)
        if name is not None:
            rendered.append(values[name])
    return "".join(rendered)


def _parse(template: str) -> list[tuple[str, str | None, str | None, str | None]]:
    try:
        return list(string.Formatter().parse(template))
    except ValueError as error:
        raise ValueError(f"invalid braces: {error}") from None


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


class AppResources(_Table):
    """What the app needs from its job; the profile says which queue."""

    cores: int | None = Field(default=None, ge=1)
    gpus: int | None = Field(default=None, ge=0)
    exclusive: bool | None = None
    time_limit: TimeLimit | None = None
    memory: PositiveByteSize | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_queue(cls, values: object) -> object:
        if isinstance(values, dict) and "queue" in values:
            raise ValueError(
                "queue is not set by an app: it is a fact about the cluster, so "
                "it belongs in the profile's [resources]"
            )
        return values


def _check_env_name(name: str) -> str:
    if not _ENV_NAME.fullmatch(name):
        raise ValueError(f"{name!r} is not an environment variable name")
    if name.startswith("EZHPCY_"):
        raise ValueError(f"{name} is reserved for EzHPCy's own variables")
    return name


class Run(_Table):
    """What runs on the host, never through a shell."""

    command: tuple[str, ...] = Field(min_length=1)
    env: dict[Annotated[str, BeforeValidator(_check_env_name)], str] = Field(
        default_factory=dict
    )
    # Default: `<apps root>/work/<normalised name>/`.
    workdir: PurePosixPath | None = None

    @field_validator("command", "env")
    @classmethod
    def reject_nul(cls, value: tuple[str, ...] | dict[str, str]) -> object:
        if any(
            "\0" in item
            for item in (value.values() if isinstance(value, dict) else value)
        ):
            raise ValueError("must not contain NUL")
        return value

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

    url: str = "http://127.0.0.1:{local_port}/"
    # Lines shown under the URL in a tunnel's status, such as a command that
    # mounts it; they take the URL's placeholders.
    status_hints: tuple[str, ...] = ()
    # 0 lets the OS pick a free port.
    local_port: int = Field(default=0, ge=0, le=65535)


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

    def placeholder_problems(self) -> Iterator[tuple[str, str]]:
        """Each field whose placeholders are wrong: (path, why)."""
        available = self.available()
        for field, path, template in self.templates():
            try:
                names = placeholders(template)
            except ValueError as error:
                yield path, str(error)
                continue
            for name in sorted(names):
                if name not in _ALL_PLACEHOLDERS:
                    yield path, f"unknown placeholder {{{name}}}"
                elif name not in FIELD_PLACEHOLDERS[field]:
                    yield path, f"{field} does not take {{{name}}}"
                elif name not in available:
                    yield path, f"{{{name}}} {self.unavailable(name)}"


class App(_Entry):
    """An app: a server run on a host, and forwarded to this machine."""

    environment: Environment
    assets: Assets | None = None
    resources: AppResources = Field(default_factory=AppResources)
    run: Run
    endpoint: Endpoint
    ready: Ready = Field(default_factory=Ready)
    access: Access = Field(default_factory=Access)

    def templates(self) -> Iterator[tuple[str, str, str]]:
        for index, argument in enumerate(self.run.command):
            yield "run.command", f"run.command[{index}]", argument
        for name, value in self.run.env.items():
            yield "run.env", f"run.env.{name}", value
        yield from _access_templates(self.access)

    def available(self) -> set[str]:
        available = set(_ALL_PLACEHOLDERS) - {"alias"}
        available.discard("port" if self.endpoint.transport == "socket" else "socket")
        if self.assets is None:
            available.discard("assets")
        return available

    def unavailable(self, name: str) -> str:
        match name:
            case "socket" | "port":
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
    host its tunnel publishes in an editor (`{alias}`, the Host alias).
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


def combined_resources(apps: Sequence[Entry]) -> dict[str, Any]:
    """
    What `apps` need from one job together, by field of `Resources`, for those
    fields any of them sets: the most any of them asks for, and exclusive if
    any of them is. Links need nothing.
    """
    combined: dict[str, Any] = {}
    for app in apps:
        if isinstance(app, Link):
            continue
        for name, value in app.resources.model_dump(exclude_unset=True).items():
            combined[name] = max(combined[name], value) if name in combined else value
    return combined


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
