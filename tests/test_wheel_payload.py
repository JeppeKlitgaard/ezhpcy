import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIST_DIRECTORY = PROJECT_ROOT / "dist"


def built_wheel() -> Path:
    wheels = list(DIST_DIRECTORY.glob("*.whl"))
    assert len(wheels) == 1, (
        f"expected exactly one wheel in {DIST_DIRECTORY}, found {wheels}"
    )
    return wheels[0]


def test_wheel_contains_local_only_runtime() -> None:
    with zipfile.ZipFile(built_wheel()) as wheel:
        names = wheel.namelist()

    assert "ezhpcy/static/data/provision.sh.j2" not in names
    assert "ezhpcy/static/data/prune-all.sh.j2" not in names
    assert "ezhpcy/static/data/ssh-serve.sh.j2" in names
    assert "ezhpcy/static/config/ssh_remote/sshd_config" in names
    assert "ezhpcy/static/config/presets/DTU.toml.j2" in names
    assert not any("_payload" in Path(name).parts for name in names)
    assert "ezhpcy/patch/sdist.py" not in names
    assert "ezhpcy/cli/compute/ssh_serve.py" not in names
    assert "ezhpcy/cli/utils/ssh_serve.py" not in names
    assert "hatch_build.py" not in names
