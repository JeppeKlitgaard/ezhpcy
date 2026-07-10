import os
from pathlib import Path
from pydantic import BaseModel
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)
from pydantic_extra_types.domain import DomainStr
import typing
import re

def _get_default_config_dir() -> Path:
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    config_home = Path(xdg_config_home) if xdg_config_home else Path.home() / ".config"
    return config_home / "ezhpcy"

def _get_default_cache_dir() -> Path:
    xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
    cache_home = Path(xdg_cache_home) if xdg_cache_home else Path.home() / ".cache"
    return cache_home / "ezhpcy"


def _default_config_file() -> Path:
    if config_file := os.environ.get("EZHPCY_CONFIG_FILE"):
        return Path(config_file).expanduser()

    return _get_default_config_dir() / "ezhpcy.toml"


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
    # define your fields here
    config_dir: Path = _get_default_config_dir()
    cache_dir: Path = _get_default_cache_dir()

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
