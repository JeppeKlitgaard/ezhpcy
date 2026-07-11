import io
import tarfile
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIST_DIRECTORY = PROJECT_ROOT / "dist"


def test_wheel_contains_one_non_recursive_sdist() -> None:
    wheels = list(DIST_DIRECTORY.glob("*.whl"))
    assert len(wheels) == 1, (
        f"expected exactly one wheel in {DIST_DIRECTORY}, found {wheels}"
    )

    with zipfile.ZipFile(wheels[0]) as wheel:
        payloads = [
            name
            for name in wheel.namelist()
            if name.startswith("ezhpcy/_payload/") and name.endswith(".tar.gz")
        ]
        assert len(payloads) == 1, (
            f"expected exactly one embedded sdist, found {payloads}"
        )
        payload = wheel.read(payloads[0])

    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        names = archive.getnames()

    assert any(name.endswith("/pyproject.toml") for name in names), (
        "embedded sdist does not contain pyproject.toml"
    )
    assert any(name.endswith("/hatch_build.py") for name in names), (
        "embedded sdist does not contain hatch_build.py"
    )
    assert not any("_payload" in Path(name).parts for name in names), (
        "embedded sdist recursively contains an _payload directory"
    )


def test_wheel_contains_install_script() -> None:
    wheels = list(DIST_DIRECTORY.glob("*.whl"))
    assert len(wheels) == 1, (
        f"expected exactly one wheel in {DIST_DIRECTORY}, found {wheels}"
    )

    with zipfile.ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()

    assert "ezhpcy/static/data/install.sh" in names
    assert "ezhpcy/static/data/uninstall.sh" in names
    assert "ezhpcy/static/config/ssh_remote/sshd_config" in names
    assert "ezhpcy/patch/editable.py" in names
    assert "ezhpcy/patch/sdist.py" in names


def test_install_script_uses_xdg_private_pixi_home() -> None:
    wheels = list(DIST_DIRECTORY.glob("*.whl"))
    assert len(wheels) == 1, (
        f"expected exactly one wheel in {DIST_DIRECTORY}, found {wheels}"
    )

    with zipfile.ZipFile(wheels[0]) as wheel:
        install_script = wheel.read("ezhpcy/static/data/install.sh").decode()

    assert "XDG_DATA_HOME" in install_script
    assert "PIXI_HOME" in install_script
    assert "PIXI_CACHE_DIR" in install_script
    assert "xdg_cache_home/ezhpcy/pixi_cache" in install_script
    assert "PIXI_NO_PATH_UPDATE" in install_script
    assert 'run_pixi exec --spec="$OPENSSH_MATCHSPEC" sshd -V' in install_script


def test_uninstall_script_removes_xdg_pixi_cache() -> None:
    wheels = list(DIST_DIRECTORY.glob("*.whl"))
    assert len(wheels) == 1, (
        f"expected exactly one wheel in {DIST_DIRECTORY}, found {wheels}"
    )

    with zipfile.ZipFile(wheels[0]) as wheel:
        uninstall_script = wheel.read("ezhpcy/static/data/uninstall.sh").decode()

    assert "XDG_CACHE_HOME" in uninstall_script
    assert "PIXI_CACHE_DIR" in uninstall_script
    assert 'rm -rf -- "$pixi_cache_dir"' in uninstall_script
