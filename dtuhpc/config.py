import os
from pathlib import Path

from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

def _get_default_config_dir() -> Path:
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    config_home = Path(xdg_config_home) if xdg_config_home else Path.home() / ".config"
    return config_home / "dtuhpc"


def _default_config_file() -> Path:
    if config_file := os.environ.get("DTUHPC_CONFIG_FILE"):
        return Path(config_file).expanduser()

    return _get_default_config_dir() / "dtuhpc.toml"

class Config(BaseSettings):
    # define your fields here
    config_dir: Path = _get_default_config_dir()

    model_config = SettingsConfigDict(
        toml_file=_default_config_file(),
        env_prefix="DTUHPC_",
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
