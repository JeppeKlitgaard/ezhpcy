import os
import re
from pathlib import Path
from typing import Self

from platformdirs import PlatformDirs
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_extra_types.domain import DomainStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)
from rich.text import Text

from ezhpcy.constants import PACKAGE_NAME, SSH_DIRECTORY_NAME
from ezhpcy.logging import LogLevel, configure_logging
from ezhpcy.types import PASSWORD_SOURCE_FIELDS, ProfileConfig, ResolvedProfileConfig
from ezhpcy.utils import ssh_connection_id

_DIRS = PlatformDirs(PACKAGE_NAME, appauthor=False)
_PROFILE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class ProfilePasswordSourceError(ValueError):
    """A profile's configured password sources are invalid."""

    def __init__(self, profile_name: str, message: str) -> None:
        self._rich_message = f"profile [bold blue]{profile_name}[/bold blue] {message}"
        super().__init__(Text.from_markup(self._rich_message).plain)

    def rich_message(self) -> str:
        return self._rich_message


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
    config_file: Path = Field(default_factory=_default_config_file)

    def ssh_dir(self, machine_id: str, *, user: str, host: str) -> Path:
        """Return the SSH credential directory for one machine and endpoint."""
        return (
            self.config_dir
            / SSH_DIRECTORY_NAME
            / machine_id
            / ssh_connection_id(user, host)
        )


class ConnectionInfo(BaseModel):
    user: str | None = None
    password: str | None = None
    host: DomainStr
    ssh_keepalive_interval_seconds: int = Field(default=30, gt=0)


class _ConfigValues(BaseModel):
    local_file: LocalFileConfig = LocalFileConfig()
    log_level: LogLevel = LogLevel.INFO
    auto_provision: bool = True
    profile: dict[str, ProfileConfig] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_profiles(self) -> Self:
        invalid_names = [
            name
            for name in self.profile
            if _PROFILE_NAME_PATTERN.fullmatch(name) is None
        ]
        if invalid_names:
            raise ValueError(
                "profile names may contain only letters, digits, '.', '_', and '-': "
                + ", ".join(repr(name) for name in invalid_names)
            )

        for name in self.profile:
            self.resolve_profile(name, validate_password_source=False)
        return self

    def resolve_profile(
        self,
        name: str | None = None,
        *,
        validate_password_source: bool = True,
    ) -> ResolvedProfileConfig:
        if name is None:
            raise ValueError("no profile was selected")
        if name not in self.profile:
            raise ValueError(f"unknown profile {name!r}")

        def merged(profile_name: str, chain: tuple[str, ...]) -> dict[str, object]:
            if profile_name in chain:
                cycle = " -> ".join((*chain, profile_name))
                raise ValueError(f"profile inheritance cycle: {cycle}")
            try:
                current = self.profile[profile_name]
            except KeyError:
                parent = chain[-1] if chain else name
                raise ValueError(
                    f"profile {parent!r} inherits unknown profile {profile_name!r}"
                ) from None

            values: dict[str, object] = {}
            if current.inherit is not None:
                values.update(merged(current.inherit, (*chain, profile_name)))
            current_values = current.model_dump(exclude={"inherit"}, exclude_unset=True)
            if current.model_fields_set & PASSWORD_SOURCE_FIELDS:
                for field in PASSWORD_SOURCE_FIELDS:
                    values.pop(field, None)
            values.update(current_values)
            return values

        resolved = ResolvedProfileConfig.model_validate(merged(name, ()))
        if validate_password_source:
            _validate_profile_password_source(name, resolved)
        return resolved


def _validate_profile_password_source(
    profile_name: str, profile: ResolvedProfileConfig
) -> None:
    if profile.password is not None:
        raise ProfilePasswordSourceError(
            profile_name,
            "must not set [bold red]password[/bold red], because passwords must "
            "not be stored in configuration files; use "
            "[bold blue]password_file[/bold blue], "
            "[bold blue]password_fd[/bold blue], or "
            "[bold blue]password_keyring[/bold blue] instead",
        )

    selected_sources = [
        name
        for name, selected in (
            ("password_file", profile.password_file is not None),
            ("password_fd", profile.password_fd is not None),
            ("password_keyring", profile.password_keyring),
        )
        if selected
    ]
    if len(selected_sources) > 1:
        rich_sources = ", ".join(
            f"[bold red]{source}[/bold red]" for source in selected_sources
        )
        raise ProfilePasswordSourceError(
            profile_name,
            f"has mutually exclusive password source settings: {rich_sources}",
        )


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
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )

    @classmethod
    def from_mapping(cls, values: object) -> Self:
        """Validate explicit values without loading workstation settings sources."""
        validated = _ConfigValues.model_validate(values)
        return cls.model_construct(**validated.__dict__)


config = Config()
configure_logging(config.log_level)
