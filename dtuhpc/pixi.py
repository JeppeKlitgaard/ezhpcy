
from pathlib import Path
import os

import subprocess

from dtuhpc.config import config
import requests

_PIXI_INSTALL_SCRIPT_URL = "https://pixi.sh/install.sh"

def _get_config_pixi_dir() -> Path:
    return config.config_dir / "pixi"


def is_pixi_installed() -> bool:
    return _get_config_pixi_dir().is_dir()


def install_pixi():
    """
    Installs the Pixi CLI tool if it is not already installed.
    """
    if is_pixi_installed():
        print("Pixi is already installed.")
        return

    install_script_path = _get_config_pixi_dir() / "install.sh"

    # Download the installation script

    response = requests.get(_PIXI_INSTALL_SCRIPT_URL)
    response.raise_for_status()

    # Save the installation script to the config directory
    _get_config_pixi_dir().mkdir(parents=True, exist_ok=True)
    with open(install_script_path, "wb") as f:
        f.write(response.content)

    # Make the script executable
    os.chmod(install_script_path, 0o755)

    # Execute the installation script
    subprocess.run([str(install_script_path)], check=True)
