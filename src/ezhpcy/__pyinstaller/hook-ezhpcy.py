"""
The files EzHPCy reads from beside its modules, which PyInstaller doesn't find
by following imports: the built-in apps' manifests and files
(`app/builtins/`), and the configuration presets (`static/`).

We don't use PyInstaller, but downstream users may.
"""

from PyInstaller.utils.hooks import collect_data_files  # type: ignore

datas = [
    *collect_data_files("ezhpcy"),
    *collect_data_files("ezhpcy", subdir="app/builtins", include_py_files=True),
]
