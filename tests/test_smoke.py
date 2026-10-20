"""
Smoke test of a built distribution, installed on its own. The publish workflow
runs it before publishing:

    uv run --isolated --no-project --with dist/*.whl tests/test_smoke.py

It checks that the `ezhpcy` command is installed and runs, that it reports the
distribution's version, and that every preset in the source tree is packaged and
loads into a valid configuration.
"""

import os
import shutil
import subprocess
import sysconfig
import tempfile
from importlib.metadata import version
from pathlib import Path

PRESETS_SOURCE = Path(__file__).parents[1] / "src/ezhpcy/static/config/presets"
PRESET_SUFFIX = ".toml.j2"


def ezhpcy(*args: str, env: dict[str, str] | None = None) -> str:
    """Run the installed `ezhpcy` command, and return what it printed."""
    command = shutil.which("ezhpcy", path=sysconfig.get_path("scripts"))
    assert command is not None, "The ezhpcy command is not installed."
    result = subprocess.run(
        [command, *args],
        capture_output=True,
        encoding="utf-8",
        env=(env or dict(os.environ)) | {"PYTHONUTF8": "1"},
        check=False,
    )
    assert result.returncode == 0, (
        f"`ezhpcy {' '.join(args)}` exited with {result.returncode}:\n"
        f"{result.stdout}{result.stderr}"
    )
    return result.stdout


def main() -> None:
    assert ezhpcy("version").strip() == version("ezhpcy")
    assert "tunnel" in ezhpcy("--help")

    presets = sorted(
        path.name.removesuffix(PRESET_SUFFIX)
        for path in PRESETS_SOURCE.glob(f"*{PRESET_SUFFIX}")
    )
    assert presets, f"No presets found in {PRESETS_SOURCE}."
    with tempfile.TemporaryDirectory() as directory:
        env = os.environ | {"EZHPCY_CONFIG_FILE": str(Path(directory) / "ezhpcy.toml")}
        for preset in presets:
            ezhpcy("config", "load", preset, "--user", "smoke-user", "--yes", env=env)
            assert "smoke-user" in ezhpcy("config", "show", env=env), preset

    print(f"Smoke test passed: ezhpcy {version('ezhpcy')}, presets {presets}.")


if __name__ == "__main__":
    main()
