import logging
import re
from typing import Annotated
lazy import os
lazy import tomllib
lazy from collections.abc import Mapping, Sequence
lazy from datetime import timedelta
lazy from pathlib import Path
lazy from typing import Any, Literal, Self

from platformdirs import PlatformDirs
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    PrivateAttr,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)
lazy from pydantic import ValidationError
lazy from pydantic.fields import FieldInfo

from ezhpcy.constants import LOGIN_KNOWN_HOSTS_NAME, PACKAGE_NAME, SSH_DIRECTORY_NAME
from ezhpcy.types import ConfigError, PasswordConfig
lazy from ezhpcy.app.manifest import App, Manifest, choose_app, collect_apps
lazy from ezhpcy.logging import get_logger
lazy from ezhpcy.types import (
    JobConfig,
    Login,
    MissingSetting,
    ProfileConfig,
    ResolvedProfileConfig,
    config_error,
    submission_sections,
    validated,
)
lazy from ezhpcy.utils import ssh_connection_id

_DIRS = PlatformDirs(PACKAGE_NAME, appauthor=False)
# Profile names double as OpenSSH Host aliases, so they exclude whitespace and the
# pattern characters `*`, `?`, `!` and `,`.
_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def unless_disabled[T](value: T | Literal[False] | None) -> T | None:
    """A password setting's value, or `None` if it is unset or `false`."""
    # `is`, since `0 == False`, and 0 is a valid descriptor.
    return None if value is False else value


def validate_alias(alias: str) -> str:
    """Check a profile name or OpenSSH Host alias is safe as both and as a file name."""
    if _ALIAS_PATTERN.fullmatch(alias) is None:
        raise ValueError(
            f"invalid host alias {alias!r}: profile names and host aliases may "
            "contain only letters, digits, '.', '_', and '-', and must start "
            "with a letter or digit"
        )
    return alias


class StoredPasswordError(ConfigError):
    """A `password` in the configuration, where passwords must not be stored."""


def _default_config_file() -> Path:
    if config_file := os.environ.get("EZHPCY_CONFIG_FILE"):
        return Path(config_file).expanduser()

    return _DIRS.user_config_path / "ezhpcy.toml"


class LocalFileConfig(BaseModel):
    """File locations for EzHPCy on the local system."""

    cache_dir: Path = _DIRS.user_cache_path
    config_dir: Path = _DIRS.user_config_path
    data_dir: Path = _DIRS.user_data_path
    runtime_dir: Path = _DIRS.user_runtime_path
    log_dir: Path = _DIRS.user_log_path
    config_file: Path = Field(default_factory=_default_config_file)

    @property
    def login_known_hosts_file(self) -> Path:
        """Return where login-node host keys accepted at EzHPCy's prompt are kept."""
        return self.config_dir / LOGIN_KNOWN_HOSTS_NAME

    def ssh_dir(self, machine_id: str, *, user: str, host: str) -> Path:
        """Return the SSH credential directory for one machine and endpoint."""
        return (
            self.config_dir
            / SSH_DIRECTORY_NAME
            / machine_id
            / ssh_connection_id(user, host)
        )


def _parse_log_level(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return logging.getLevelNamesMapping()[value.strip().upper()]
    except KeyError:
        raise ValueError(f"unknown logging level: {value!r}") from None


def _format_log_level(value: int) -> str | int:
    name = logging.getLevelName(value)
    return name if name in logging.getLevelNamesMapping() else value


# A level by number or by name, in any case. Dumped to JSON by name, if it has one.
LogLevel = Annotated[
    int,
    BeforeValidator(_parse_log_level),
    PlainSerializer(_format_log_level, when_used="json"),
]


class ConsoleLoggingConfig(BaseModel):
    """The log on the terminal. `debug` (or `--debug`) enables it at DEBUG."""

    enabled: bool = True
    level: LogLevel = logging.INFO

    model_config = ConfigDict(extra="forbid")


class FileLoggingConfig(BaseModel):
    """Each run's log file, in `LocalFileConfig.log_dir`."""

    enabled: bool = True
    level: LogLevel = logging.DEBUG
    # How many runs' log files to keep, the current one's included.
    keep: int = Field(default=10, ge=1)

    model_config = ConfigDict(extra="forbid")


class LoggingConfig(BaseModel):
    console: ConsoleLoggingConfig = ConsoleLoggingConfig()
    file: FileLoggingConfig = FileLoggingConfig()

    model_config = ConfigDict(extra="forbid")


class _ConfigValues(BaseModel):
    local_file: LocalFileConfig = LocalFileConfig()
    logging: LoggingConfig = LoggingConfig()
    debug: bool = False
    auto_provision: bool = True
    # Every profile inherits these, and a run without a profile uses them.
    connection: PasswordConfig = PasswordConfig()
    profile: dict[str, ProfileConfig] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_profiles(self) -> Self:
        for name in self.profile:
            validate_alias(name)
            self.resolve_profile(name)
        return self

    def resolve_profile(self, profile_name: str | None = None) -> ResolvedProfileConfig:
        """Resolve a profile, or the top-level settings alone when `None`."""
        top_level = {"connection": self.connection.model_dump(exclude_unset=True)}
        if profile_name is None:
            return ResolvedProfileConfig.model_validate(top_level)
        # The top-level settings are the root of every inheritance chain.
        values: dict[str, Any] = top_level
        for name in reversed(self.inheritance_chain(profile_name)):
            current = self.profile[name]
            current_values = current.model_dump(exclude={"inherit"}, exclude_unset=True)
            for key, value in current_values.items():
                # Sub-configs merge field by field; everything else is replaced.
                if isinstance(value, dict):
                    values[key] = {**values.get(key, {}), **value}
                else:
                    values[key] = value

        return ResolvedProfileConfig.model_validate(values)

    @property
    def profiles(self) -> Mapping[str, Profile]:
        """The profiles, by name, each with its inheritance applied when read."""
        return {name: Profile(self, name) for name in self.profile}

    def resolve_login(
        self, profile_name: str | None = None, **changes: object
    ) -> Login:
        """
        The login node of a profile, or of the top-level settings alone when
        `None`, with `changes` to its fields (those of `Login`) on top.

        Raises `ConfigError`, such as `MissingSetting` for a host or user that
        neither gives, or `StoredPasswordError`.
        """
        self.reject_stored_password(profile_name)
        connection = self.resolve_profile(profile_name).connection
        values = {
            "host": connection.host,
            "user": connection.user,
            "keepalive_interval": timedelta(
                seconds=connection.ssh_keepalive_interval_seconds
            ),
            "password_fd": unless_disabled(connection.password_fd),
            "password_file": unless_disabled(connection.password_file),
            "password_keyring": connection.password_keyring,
            "password_prompt": connection.password_prompt,
        } | changes
        for name in ("user", "host"):
            if values[name] is None:
                raise self._missing(profile_name, name, f"connection.{name}")
        return validated(Login, values)

    def resolve_job(
        self,
        profile_name: str | None = None,
        *,
        scheduler: Mapping[str, object] | None = None,
        resources: Mapping[str, object] | None = None,
        timings: Mapping[str, object] | None = None,
        auto_provision: bool | None = None,
    ) -> JobConfig:
        """
        The worker job of a profile, or of the top-level settings alone when
        `None`, with changes to its `scheduler`, `resources` and `timings` on top,
        and `auto_provision` if given, else the top-level setting.

        With an interactive submission command, the submission options (see
        `JobConfig`) that the profile only inherits are logged and dropped:
        profiles for a site's wrapper typically inherit from a base profile that
        sets them. Those set in the profile itself, or in the changes, are an
        error.

        Raises `ConfigError`, such as `MissingSetting` for a scheduler type or
        submission mode that neither gives, or `SubmissionCommandError`.
        """
        profile = self.resolve_profile(profile_name)
        changes = {
            "scheduler": scheduler or {},
            "resources": resources or {},
            "timings": timings or {},
        }
        values: dict[str, Any] = {
            section: {
                **getattr(profile, section).model_dump(exclude_unset=True),
                **changes.get(section, {}),
            }
            for section in ("scheduler", "resources", "timings", "lsf", "pbs")
        }
        values["auto_provision"] = (
            self.auto_provision if auto_provision is None else auto_provision
        )
        if values["scheduler"].get("interactive_submission_command") is not None:
            self._drop_inherited_submission_options(
                profile_name, profile, values, changes
            )
        try:
            return validated(JobConfig, values)
        except ConfigError as error:
            for name in ("type", "submission_mode"):
                if values["scheduler"].get(name) is None:
                    setting = f"scheduler.{name}"
                    raise self._missing(profile_name, setting, setting) from error
            raise

    def _drop_inherited_submission_options(
        self,
        profile_name: str | None,
        profile: ResolvedProfileConfig,
        values: dict[str, Any],
        changes: Mapping[str, Mapping[str, object]],
    ) -> None:
        """
        Drop from `values` the submission options that `profile_name` only
        inherits, for its interactive submission command (see `resolve_job`):
        not those `changes` give, which stay to be refused.
        """
        own = self.profile[profile_name] if profile_name is not None else None
        try:
            scheduler_type = validated(
                type(profile.scheduler), values["scheduler"]
            ).type
        except ConfigError:
            scheduler_type = None
        if scheduler_type is None:
            # Validating the job reports it.
            return
        inherited: list[str] = []
        for section in submission_sections(scheduler_type):
            configured = getattr(profile, section).model_fields_set
            directly = getattr(own, section).model_fields_set if own else set()
            for name in configured - directly - set(changes.get(section, {})):
                values[section].pop(name, None)
                inherited.append(f"{section}.{name}")
        if inherited:
            get_logger(__name__).info(
                t"Ignoring inherited scheduler submission options for "
                t"{'scheduler.interactive_submission_command':name}: "
                t"{sorted(inherited):name}"
            )

    @staticmethod
    def _missing(profile_name: str | None, setting: str, key: str) -> MissingSetting:
        """`setting` is missing; `key` is its path in a profile."""
        if profile_name is not None:
            key = f"profile.{profile_name}.{key}"
        return MissingSetting(setting, key=key)

    def reject_stored_password(self, profile_name: str | None = None) -> None:
        """
        Reject a `password` in the top-level `connection` table, or in the profile
        or a profile it inherits. Not when loading: one profile's mistake mustn't
        keep the others from running.
        """
        tables = {"connection": self.connection}
        if profile_name is not None:
            tables |= {
                f"profile.{name}.connection": self.profile[name].connection
                for name in self.inheritance_chain(profile_name)
            }
        for table, connection in tables.items():
            if connection.password is not None:
                raise StoredPasswordError(
                    t"{f'{table}.password':name} must not be set, because passwords "
                    t"must not be stored in configuration files; use "
                    t"{'password_file':name}, {'password_fd':name}, or "
                    t"{'password_keyring':name} instead"
                )

    def inheritance_chain(self, profile_name: str) -> tuple[str, ...]:
        """Return the profile, then the profile it inherits, and so on."""
        if profile_name not in self.profile:
            raise ValueError(f"unknown profile {profile_name!r}")
        chain: list[str] = []
        name: str | None = profile_name
        while name is not None:
            if name in chain:
                cycle = " -> ".join((*chain, name))
                raise ValueError(f"profile inheritance cycle: {cycle}")
            if name not in self.profile:
                raise ValueError(
                    f"profile {chain[-1]!r} inherits unknown profile {name!r}"
                )
            chain.append(name)
            name = self.profile[name].inherit
        return tuple(chain)


# Settings that `EZHPCY_<NAME>` environment variables can't set: they're tables, and
# `EZHPCY_PROFILE` is the CLI's PROFILE argument.
_NOT_FROM_ENV = frozenset({"profile", "local_file", "logging", "connection"})


class _EnvSettingsSource(EnvSettingsSource):
    """Read every setting but `_NOT_FROM_ENV` from the environment."""

    def get_field_value(
        self, field: FieldInfo, field_name: str
    ) -> tuple[Any, str, bool]:
        if field_name in _NOT_FROM_ENV:
            return None, field_name, False
        return super().get_field_value(field, field_name)


class Config(BaseSettings, _ConfigValues):
    """Configuration loaded from the local workstation settings sources."""

    model_config = SettingsConfigDict(
        toml_file=_default_config_file(),
        env_prefix="EZHPCY_",
    )
    _apps: dict[str, dict[str, App]] = PrivateAttr(default_factory=dict)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            _EnvSettingsSource(settings_cls),
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )

    @model_validator(mode="before")
    @classmethod
    def reject_local_file(cls, values: object) -> object:
        # The config file location is resolved before any settings source is read,
        # so `local_file` is derived from the platform directories, not configured.
        if isinstance(values, dict) and "local_file" in values:
            raise ValueError(
                "local_file cannot be configured; set EZHPCY_CONFIG_FILE to use a "
                "different configuration file"
            )
        return values

    @classmethod
    def from_mapping(cls, values: object) -> Self:
        """Validate explicit values without loading workstation settings sources."""
        validated = _ConfigValues.model_validate(values)
        return cls.model_construct(
            _fields_set=validated.model_fields_set, **validated.__dict__
        )

    @classmethod
    def load(
        cls,
        path: Path | None = None,
        *,
        manifests: Sequence[Path] = (),
        default_manifests: Sequence[Path] = (),
    ) -> Self:
        """
        Read the configuration file `path`, by default the one the CLI reads
        (`default_path`), and the apps of `manifests` (`Manifest.load`). Unlike
        `Config()`, the environment (`EZHPCY_*`) is not read. A value: loading
        one changes nothing global, and several may be used at once.

        `default_manifests` are a front end's own: a version of an app in one of
        `manifests` replaces the same version there. Raises `ConfigError`, and
        `ManifestError` for an invalid manifest, or the same version of an app
        in two of `manifests` (or two of `default_manifests`).
        """
        loaded = cls.from_file(path or cls.default_path())
        loaded._apps = collect_apps(
            [Manifest.load(manifest) for manifest in manifests],
            [Manifest.load(manifest) for manifest in default_manifests],
        )
        return loaded

    @property
    def apps(self) -> Mapping[str, Mapping[str, App]]:
        """
        The apps of the manifests it was loaded with, by normalised name
        (`My_App` is `my-app`), then by version as the manifest spells it.
        """
        return self._apps

    def app(self, name: str, version: str | None = None) -> App:
        """
        The app called `name`, compared normalised, at `version` (`v1.2`, the
        same as `v1.2.0`), or by default its newest version that isn't a
        pre-release. Raises `ManifestError`, listing the choices.
        """
        return choose_app(self._apps, name, version)

    @staticmethod
    def default_path() -> Path:
        """
        The configuration file the CLI reads: `EZHPCY_CONFIG_FILE`, or
        `ezhpcy.toml` in the platform's configuration directory.
        """
        return _default_config_file()

    @classmethod
    def from_file(cls, path: Path) -> Self:
        """
        Read and validate the configuration file `path`, without the environment
        or any other source. Raises `ConfigError`.
        """
        try:
            with path.open("rb") as file:
                values = tomllib.load(file)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ConfigError(
                t"Could not read the configuration file {path:path}: {error}"
            ) from error
        try:
            return cls.from_mapping(values)
        except ValidationError as error:
            raise ConfigError(
                t"Invalid configuration file {path:path}: {config_error(error)}"
            ) from error


class Profile:
    """
    A profile of a configuration: its `login` and its `job`, each built and
    checked when read. A profile without a setting it needs (a host, a
    scheduler type) is still listed; reading `login` or `job` then raises
    `ConfigError` naming it.
    """

    def __init__(self, config: _ConfigValues, name: str) -> None:
        self._config = config
        self.name = name

    @property
    def description(self) -> str | None:
        return self._config.resolve_profile(self.name).description

    @property
    def login(self) -> Login:
        """The login node, and where its password comes from."""
        return self._config.resolve_login(self.name)

    @property
    def job(self) -> JobConfig:
        """The worker job."""
        return self._config.resolve_job(self.name)

    def __repr__(self) -> str:
        return f"Profile({self.name!r})"


config = Config()
