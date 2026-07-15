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

from ezhpcy.constants import PACKAGE_NAME, SSH_DIRECTORY_NAME
from ezhpcy.logging import LogLevel, configure_logging
from ezhpcy.types import ProfileConfig, ResolvedProfileConfig
from ezhpcy.utils import ssh_connection_id

_DIRS = PlatformDirs(PACKAGE_NAME, appauthor=False)
_PROFILE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _default_config_file() -> Path:
    if config_file := os.environ.get("EZHPCY_CONFIG_FILE"):
        return Path(config_file).expanduser()

    return _DIRS.user_config_path / "ezhpcy.toml"


class LocalFileConfig(BaseModel):
    """File locations for ezhpcy on the local system."""

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


class _ConfigValues(BaseModel):
    local_file: LocalFileConfig = LocalFileConfig()
    log_level: LogLevel = LogLevel.INFO
    auto_provision: bool = True
    default_profile: str | None = None
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
        if (
            self.default_profile is not None
            and self.default_profile not in self.profile
        ):
            raise ValueError(
                f"default_profile {self.default_profile!r} does not name a configured profile"
            )
        for name in self.profile:
            self.resolve_profile(name)
        return self

    def resolve_profile(self, name: str | None = None) -> ResolvedProfileConfig:
        selected = name or self.default_profile
        if selected is None:
            raise ValueError(
                "no profile was selected and default_profile is not configured"
            )
        if selected not in self.profile:
            raise ValueError(f"unknown profile {selected!r}")

        def merged(profile_name: str, chain: tuple[str, ...]) -> dict[str, object]:
            if profile_name in chain:
                cycle = " -> ".join((*chain, profile_name))
                raise ValueError(f"profile inheritance cycle: {cycle}")
            try:
                current = self.profile[profile_name]
            except KeyError:
                parent = chain[-1] if chain else selected
                raise ValueError(
                    f"profile {parent!r} inherits unknown profile {profile_name!r}"
                ) from None

            values: dict[str, object] = {}
            if current.inherit is not None:
                values.update(merged(current.inherit, (*chain, profile_name)))
            values.update(current.model_dump(exclude={"inherit"}, exclude_unset=True))
            return values

        return ResolvedProfileConfig.model_validate(merged(selected, ()))


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
        return cls.model_construct(
            local_file=validated.local_file,
            log_level=validated.log_level,
            auto_provision=validated.auto_provision,
            default_profile=validated.default_profile,
            profile=validated.profile,
        )


config = Config()
configure_logging(config.log_level)
