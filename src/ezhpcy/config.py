import logging
import re
from typing import Annotated
lazy import os
lazy from pathlib import Path
lazy from string.templatelib import Template
lazy from typing import Any, Self

from platformdirs import PlatformDirs
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)
lazy from pydantic.fields import FieldInfo

from ezhpcy.constants import LOGIN_KNOWN_HOSTS_NAME, PACKAGE_NAME, SSH_DIRECTORY_NAME
from ezhpcy.types import PasswordConfig
lazy from ezhpcy.messages import plain
lazy from ezhpcy.types import (
    PASSWORD_SOURCE_FIELDS,
    ProfileConfig,
    ResolvedProfileConfig,
)
lazy from ezhpcy.utils import ssh_connection_id

_DIRS = PlatformDirs(PACKAGE_NAME, appauthor=False)
# Profile names double as OpenSSH Host aliases, so they exclude whitespace and the
# pattern characters `*`, `?`, `!` and `,`.
_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def validate_alias(alias: str) -> str:
    """Check a profile name or OpenSSH Host alias is safe as both and as a file name."""
    if _ALIAS_PATTERN.fullmatch(alias) is None:
        raise ValueError(
            f"invalid host alias {alias!r}: profile names and host aliases may "
            "contain only letters, digits, '.', '_', and '-', and must start "
            "with a letter or digit"
        )
    return alias


class ProfilePasswordSourceError(ValueError):
    """A profile's configured password sources are invalid.

    `message` is a template, so the CLI can style the names in it (see
    `ezhpcy.messages`).
    """

    def __init__(self, message: Template) -> None:
        self.message = message
        super().__init__(plain(message))


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
            self.resolve_profile(name, validate_password_source=False)
        return self

    def resolve_profile(
        self,
        profile_name: str | None = None,
        *,
        validate_password_source: bool = True,
    ) -> ResolvedProfileConfig:
        """Resolve a profile, or the top-level settings alone when `None`."""
        if validate_password_source:
            _validate_password_source(
                self.connection, subject=t"the top-level {'connection':name} table"
            )
        top_level = {"connection": self.connection.model_dump(exclude_unset=True)}
        if profile_name is None:
            return ResolvedProfileConfig.model_validate(top_level)
        # The top-level settings are the root of every inheritance chain.
        values: dict[str, Any] = top_level
        for name in reversed(self.inheritance_chain(profile_name)):
            current = self.profile[name]
            current_values = current.model_dump(exclude={"inherit"}, exclude_unset=True)
            if current.connection.model_fields_set & PASSWORD_SOURCE_FIELDS:
                for field in PASSWORD_SOURCE_FIELDS:
                    values.get("connection", {}).pop(field, None)
            for key, value in current_values.items():
                # Sub-configs merge field by field; everything else is replaced.
                if isinstance(value, dict):
                    values[key] = {**values.get(key, {}), **value}
                else:
                    values[key] = value

        profile_config = ResolvedProfileConfig.model_validate(values)
        if validate_password_source:
            # The top-level table is valid, so any fault is the profile's.
            _validate_password_source(
                profile_config.connection,
                subject=t"profile {profile_name:name}",
                prefix="connection.",
            )
        return profile_config

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


def _validate_password_source(
    connection: PasswordConfig, *, subject: Template, prefix: str = ""
) -> None:
    """Check the password settings of `subject`, whose keys start with `prefix`."""
    if connection.password is not None:
        raise ProfilePasswordSourceError(
            subject
            + t" must not set {f'{prefix}password':name}, because passwords must "
            t"not be stored in configuration files; use "
            t"{f'{prefix}password_file':name}, {f'{prefix}password_fd':name}, or "
            t"{f'{prefix}password_keyring':name} instead"
        )

    selected_sources = [
        f"{prefix}{name}"
        for name, selected in (
            ("password_file", connection.password_file is not None),
            ("password_fd", connection.password_fd is not None),
            ("password_keyring", connection.password_keyring),
        )
        if selected
    ]
    if len(selected_sources) > 1:
        raise ProfilePasswordSourceError(
            subject + t" has mutually exclusive password source settings: "
            t"{selected_sources:name}"
        )


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


config = Config()
