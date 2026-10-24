"""
An app's command through Pixi (`ezhpcy.app.run`), run for real: under bash,
with the launch script, and a real `pixi` where there is one (in WSL on
Windows). Its arguments must reach the app as the manifest gives them.
"""

import shutil
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

import pytest

from ezhpcy.app import run as app_run
from ezhpcy.app.manifest import Manifest
from ezhpcy.app.provision import AppsLayout, ProvisionedApp
from ezhpcy.process import launch_script
from ezhpcy.types import RemoteState
from tests.support.apps import MINIMAL_APP, write_project

WAIT_SECONDS = 30
# Pixi as its installer leaves it, if a login shell's PATH doesn't have it.
_INSTALLED_PIXI = Path.home() / ".pixi" / "bin" / "pixi"
PIXI = shutil.which("pixi") or (
    str(_INSTALLED_PIXI) if _INSTALLED_PIXI.exists() else None
)
# What would need quoting for a shell, or for Pixi's own deno_task_shell.
AWKWARD = [
    "a b",
    "\"q\" 's'",
    "$HOME",
    "*",
    "",
    "line\nbreak",
    ";",
    "|",
    "&&",
    "~",
    "${X:-y}",
    "`x`",
    "$(y)",
    "\\",
    "#c",
    "<in",
    "2>&1",
    "--",
    "-e",
]


def _escaped(part: str) -> str:
    """`part` as a manifest writes it: braces doubled, as they aren't placeholders."""
    return part.replace("{", "{{").replace("}", "}}")


pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or PIXI is None, reason="bash and a real pixi"
)


def test_awkward_arguments_reach_the_app_through_pixi_unchanged(
    tmp_path: Path,
) -> None:
    """
    The environment isn't installed, so `pixi run --as-is -x` runs `sh` from
    `PATH`: what matters is how Pixi passes the arguments on.
    """
    remote_state = RemoteState(
        cache_dir=PurePosixPath(tmp_path / "cache"),
        state_dir=PurePosixPath(tmp_path / "state"),
    )
    executable = Path(remote_state.pixi_executable())
    executable.parent.mkdir(parents=True)
    assert PIXI is not None
    executable.symlink_to(PIXI)
    command = ["sh", "-c", 'printf "%s\\0" "$@"', "sh", *AWKWARD]
    manifest = write_project(
        tmp_path / "project",
        MINIMAL_APP.replace(
            'run.command = ["jupyter", "lab", "--ServerApp.sock={socket}"]',
            "run.command = ["
            + ", ".join(f"'''{_escaped(part)}'''" for part in command)
            + "]",
        ),
    )
    app = Manifest.load(manifest).apps["lab"]["v1.0"]
    # Loading needs it; Pixi would read it, and it is a stand-in's.
    (manifest.parent / "pixi.lock").unlink()
    layout = AppsLayout(PurePosixPath(tmp_path / "apps"))
    provisioned = ProvisionedApp(PurePosixPath(manifest.parent / "pixi.toml"), None)
    workdir = tmp_path / "work"
    workdir.mkdir()

    argv, env = app_run._command(
        app, remote_state, layout, provisioned, PurePosixPath(workdir), "0" * 32
    )
    run_dir = tmp_path / "ezhpcy-run"
    run_dir.mkdir(mode=0o700)
    (run_dir / "launch.sh").write_text(
        launch_script(
            PurePosixPath(run_dir), argv, env=env, cwd=PurePosixPath(workdir)
        ),
        encoding="utf-8",
    )
    subprocess.run(
        ["setsid", "-f", "bash", str(run_dir / "launch.sh")],
        capture_output=True,
        check=True,
        timeout=WAIT_SECONDS,
    )

    deadline = time.monotonic() + WAIT_SECONDS
    while not (run_dir / "status").exists():
        assert time.monotonic() < deadline, "no status file"
        time.sleep(0.05)
    assert (run_dir / "status").read_text(encoding="utf-8") == "0\n", (
        run_dir / "err"
    ).read_text(encoding="utf-8")
    printed = (run_dir / "out").read_text(encoding="utf-8").split("\0")[:-1]
    assert printed == AWKWARD
