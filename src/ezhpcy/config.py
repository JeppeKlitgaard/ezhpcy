import os
import re
import tempfile
import typing
from pathlib import Path, PurePath, PurePosixPath
from typing import Generic, TypeVar

from pydantic import BaseModel, Field
from pydantic_extra_types.domain import DomainStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


def _get_default_config_dir() -> Path:
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    config_home = Path(xdg_config_home) if xdg_config_home else Path.home() / ".config"
    return config_home / "ezhpcy"


def _get_default_cache_dir() -> Path:
    xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
    cache_home = Path(xdg_cache_home) if xdg_cache_home else Path.home() / ".cache"
    return cache_home / "ezhpcy"


def _get_default_data_dir() -> Path:
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    data_home = (
        Path(xdg_data_home) if xdg_data_home else Path.home() / ".local" / "share"
    )
    return data_home / "ezhpcy"


def _get_default_runtime_dir() -> Path:
    xdg_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime_dir:
        return Path(xdg_runtime_dir) / "ezhpcy"

    # Unlike XDG_RUNTIME_DIR, the system temporary directory is commonly shared.
    # Include the numeric uid so another user cannot reserve our predictable
    # fallback directory first. Windows temporary directories are already scoped
    # to the user and os.getuid() is not available there.
    getuid = getattr(os, "getuid", None)
    directory_name = f"ezhpcy-{getuid()}" if getuid is not None else "ezhpcy"
    return Path(tempfile.gettempdir()) / directory_name


def _default_config_file() -> Path:
    if config_file := os.environ.get("EZHPCY_CONFIG_FILE"):
        return Path(config_file).expanduser()

    return _get_default_config_dir() / "ezhpcy.toml"


PathT = TypeVar("PathT", bound=PurePath)


class FileConfig(BaseModel, Generic[PathT]):
    """
    The file locations required for ezhpcy.

    Generic over the path type so local (Path) and remote (PurePosixPath)
    configs can share structure without violating Liskov substitution.
    """

    cache_dir: PathT
    config_dir: PathT
    data_dir: PathT
    runtime_dir: PathT
    config_file: PathT


class LocalFileConfig(FileConfig[Path]):
    """File locations for ezhpcy on the local system."""

    cache_dir: Path = Field(default_factory=_get_default_cache_dir)
    config_dir: Path = Field(default_factory=_get_default_config_dir)
    data_dir: Path = Field(default_factory=_get_default_data_dir)
    runtime_dir: Path = Field(default_factory=_get_default_runtime_dir)
    config_file: Path = Field(default_factory=_default_config_file)


class RemoteFileConfig(FileConfig[PurePosixPath]):
    """File locations for ezhpcy on a POSIX remote system."""

    cache_dir: PurePosixPath
    config_dir: PurePosixPath
    data_dir: PurePosixPath
    runtime_dir: PurePosixPath
    config_file: PurePosixPath


class HPCConfig(BaseModel):
    # At DTU this can be found by running `ls /lsf/local/bin/` and filtering a bit manually
    # Or using the list at https://www.hpc.dtu.dk/?page_id=2129 and elsewhere
    interactive_shells: list[str] = [
        # GPU
        "a100sh",
        "h100sh",
        "voltash",
        "sxm2sh",
        # CPU
        "qrsh",
        "linuxsh",
    ]

    default_interactive_shell: str = "linuxsh"

    login_node_pattern: typing.Pattern = re.compile(r"^hpclogin\d+$")


class ConnectionInfo(BaseModel):
    user: str | None = None
    password: str | None = None

    host: DomainStr = DomainStr("login2.hpc.dtu.dk")


class Config(BaseSettings):
    local_file: LocalFileConfig = LocalFileConfig()

    hpc: HPCConfig = HPCConfig()
    connection: ConnectionInfo = ConnectionInfo()

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


config = Config()
