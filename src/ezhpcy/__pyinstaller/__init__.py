"""
PyInstaller's hooks for EzHPCy, found through the `pyinstaller40` entry point,
so that an app frozen with EzHPCy has the files EzHPCy reads beside its modules.
"""

from pathlib import Path


def get_hook_dirs() -> list[str]:
    return [str(Path(__file__).parent)]
