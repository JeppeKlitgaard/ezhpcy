import zipfile
from pathlib import Path

from jinja2 import StrictUndefined, Template

from ezhpcy.constants import OPENSSH_MATCHSPEC

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

    assert "ezhpcy/static/data/provision.sh.j2" in names
    assert "ezhpcy/static/data/prune-all.sh.j2" in names
    assert "ezhpcy/static/data/ssh-serve.sh.j2" in names
    assert "ezhpcy/static/config/ssh_remote/sshd_config" in names
    assert "ezhpcy/static/config/presets/DTU.toml.j2" in names
    assert not any("_payload" in Path(name).parts for name in names)
    assert "ezhpcy/patch/sdist.py" not in names
    assert "ezhpcy/cli/compute/ssh_serve.py" not in names
    assert "hatch_build.py" not in names


def test_provision_script_installs_only_pixi_and_openssh() -> None:
    with zipfile.ZipFile(built_wheel()) as wheel:
        provision_template = wheel.read("ezhpcy/static/data/provision.sh.j2").decode()

    provision_script = Template(provision_template, undefined=StrictUndefined).render(
        openssh_matchspec=OPENSSH_MATCHSPEC,
    )

    assert "\r" not in provision_template
    assert "XDG_DATA_HOME" in provision_script
    assert "PIXI_HOME" in provision_script
    assert "PIXI_CACHE_DIR" in provision_script
    assert "xdg_cache_home/ezhpcy/pixi_cache" in provision_script
    assert "PIXI_NO_PATH_UPDATE" in provision_script
    assert "openssh_matchspec={{ openssh_matchspec }}" in provision_template
    assert 'run_pixi exec --spec="$openssh_matchspec" sh -c' in provision_script
    assert 'exec "$sshd_path" -V' in provision_script
    assert f"openssh_matchspec={OPENSSH_MATCHSPEC}" in provision_script
    assert "uv tool" not in provision_script
    assert "sdist" not in provision_script
    assert "ezhpcy version" not in provision_script


def test_prune_all_script_removes_the_complete_remote_footprint() -> None:
    with zipfile.ZipFile(built_wheel()) as wheel:
        prune_template = wheel.read("ezhpcy/static/data/prune-all.sh.j2").decode()

    prune_script = Template(prune_template, undefined=StrictUndefined).render()

    assert "\r" not in prune_template
    assert "XDG_CONFIG_HOME" in prune_script
    assert "XDG_CACHE_HOME" in prune_script
    assert 'rm -rf -- "$ezhpcy_config_dir"' in prune_script
    assert 'rm -rf -- "$ezhpcy_cache_dir"' in prune_script
    assert 'exec rm -rf -- "$remote_root"' in prune_script
    assert "uv tool" not in prune_script
