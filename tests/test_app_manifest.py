"""
The apps manifest: loading, checking, versions and names, choosing an app from
a configuration's manifests, rendering placeholders, and its keys.
"""

import hashlib
import json
import re
from datetime import timedelta
from pathlib import Path, PurePosixPath

import pytest

from ezhpcy import api
from ezhpcy.app.manifest import (
    App,
    AppResources,
    AssetFile,
    Manifest,
    assets_key,
    environment_key,
    merge_resources,
    placeholders,
    render,
)
from ezhpcy.types import AppsConfig, Resources
from tests.support.apps import MINIMAL_APP, lock_file, write_project

EXAMPLES = Path(__file__).parent.parent / "examples" / "apps"
EXAMPLE_MANIFESTS = sorted(EXAMPLES.glob("*/ezhpcy-apps.toml"))
LAB = 'ezhpcy.app.lab."v1.0"'

# PLAN.md's JupyterLab example, written with sub-tables and with dotted keys.
LAB_SUB_TABLES = """\
[ezhpcy]
schema = 1

[ezhpcy.app.lab."v1.0"]
description = "JupyterLab with the project's environment"

[ezhpcy.app.lab."v1.0".environment]
pixi = "pixi.toml"
environment = "default"

[ezhpcy.app.lab."v1.0".resources]
gpus = 1
time_limit = "4:00"

[ezhpcy.app.lab."v1.0".run]
command = [
    "jupyter", "lab", "--no-browser",
    "--ServerApp.sock={socket}",
    "--KernelManager.transport=ipc",
]
env = { JUPYTER_TOKEN = "{token}", JUPYTER_RUNTIME_DIR = "{run_dir}/jupyter" }

[ezhpcy.app.lab."v1.0".endpoint]
transport = "socket"
authenticates = true

[ezhpcy.app.lab."v1.0".ready]
check = "http"
path = "/api"
timeout_seconds = 180

[ezhpcy.app.lab."v1.0".access]
url = "http://127.0.0.1:{local_port}/lab?token={token}"
"""
LAB_DOTTED_KEYS = """\
[ezhpcy]
schema = 1

[ezhpcy.app.lab."v1.0"]
description = "JupyterLab with the project's environment"
environment.pixi = "pixi.toml"
environment.environment = "default"
resources = { gpus = 1, time_limit = "4:00" }
run.command = [
    "jupyter", "lab", "--no-browser",
    "--ServerApp.sock={socket}",
    "--KernelManager.transport=ipc",
]
run.env.JUPYTER_TOKEN = "{token}"
run.env.JUPYTER_RUNTIME_DIR = "{run_dir}/jupyter"
endpoint = { transport = "socket", authenticates = true }
ready.check = "http"
ready.path = "/api"
ready.timeout_seconds = 180
access.url = "http://127.0.0.1:{local_port}/lab?token={token}"
"""


def load(tmp_path: Path, manifest: str, **project: object) -> Manifest:
    return Manifest.load(write_project(tmp_path, manifest, **project))


def only(manifest: Manifest) -> App:
    """The manifest's one app version."""
    [versions] = manifest.apps.values()
    [app] = versions.values()
    return app


def problems(tmp_path: Path, manifest: str, **project: object) -> str:
    """The message `Manifest.load` fails with, as plain text."""
    with pytest.raises(api.ManifestError) as error:
        load(tmp_path, manifest, **project)
    return str(error.value)


def versions(*tables: tuple[str, str]) -> str:
    """A manifest with MINIMAL_APP's app under these (name, version) keys."""
    body = MINIMAL_APP.split("\n\n", 1)[1].split("\n", 1)[1]
    return "[ezhpcy]\nschema = 1\n" + "".join(
        f'\n[ezhpcy.app.{name}."{version}"]\n{body}' for name, version in tables
    )


# Loading


@pytest.mark.parametrize("path", EXAMPLE_MANIFESTS, ids=lambda path: path.parent.name)
def test_the_examples_load(path: Path) -> None:
    manifest = Manifest.load(path)

    assert manifest.apps
    for name, by_version in manifest.apps.items():
        for version, app in by_version.items():
            assert (app.normalised_name, app.version) == (name, version)
            assert app.environment.pixi == path.parent / "pixi.toml"
            assert app.environment.lock.is_file()


def test_the_notebook_apps_share_one_environment() -> None:
    manifest = Manifest.load(EXAMPLES / "notebooks")

    lab = manifest.apps["lab"]["v1.0"]
    tensorboard = manifest.apps["tensorboard"]["v2.18"]
    assert environment_key(lab.environment) == environment_key(tensorboard.environment)


def test_sub_tables_and_dotted_keys_load_alike(tmp_path: Path) -> None:
    sub_tables = only(load(tmp_path / "a", LAB_SUB_TABLES))
    dotted = only(load(tmp_path / "b", LAB_DOTTED_KEYS))

    assert sub_tables.model_dump(exclude={"environment"}) == dotted.model_dump(
        exclude={"environment"}
    )
    assert sub_tables.resources == AppResources(gpus=1, time_limit=timedelta(hours=4))
    assert sub_tables.run.env == {
        "JUPYTER_TOKEN": "{token}",
        "JUPYTER_RUNTIME_DIR": "{run_dir}/jupyter",
    }
    assert sub_tables.ready.accepts(200)
    assert sub_tables.ready.accepts(399)
    assert not sub_tables.ready.accepts(401)


def test_an_app_is_frozen_and_shown_as_name_and_version(tmp_path: Path) -> None:
    app = only(load(tmp_path, MINIMAL_APP))

    assert (app.name, app.version, str(app)) == ("lab", "v1.0", "lab v1.0")
    with pytest.raises(ValueError, match="frozen"):
        app.description = "changed"


def test_a_directory_names_the_manifest_in_it(tmp_path: Path) -> None:
    write_project(tmp_path, MINIMAL_APP)

    assert Manifest.load(tmp_path).path == tmp_path / "ezhpcy-apps.toml"


def test_a_manifest_in_a_directory_with_spaces_loads(tmp_path: Path) -> None:
    directory = tmp_path / "my project" / "with spaces"
    manifest = load(
        directory,
        MINIMAL_APP + 'assets.include = ["app files/*.py"]\n',
        files={"app files/main.py": "print('hi')\n"},
    )

    app = only(manifest)
    assert app.environment.pixi == directory / "pixi.toml"
    assert app.assets is not None
    assert [file.relative for file in app.assets.files] == [
        PurePosixPath("app files/main.py")
    ]


def test_other_tools_tables_are_ignored(tmp_path: Path) -> None:
    manifest = load(
        tmp_path,
        """\
[mytool.deploy]
target = "staging"

[mytool.app.lab]
rnu = "looks like EzHPCy's, but isn't"
schema = 2

[tool.ezhpcy.app.other]
anything = true

"""
        + MINIMAL_APP,
    )

    assert list(manifest.apps) == ["lab"]


@pytest.mark.parametrize(
    ("manifest", "message"),
    [
        ("[mytool]\nx = 1\n", "has no [ezhpcy] table"),
        ("ezhpcy = 1\n", "has no [ezhpcy] table"),
        ("[ezhpcy]\nschema = 1\n", "has no apps"),
        ("[ezhpcy]\nschema = 1\n[ezhpcy.app]\n", "has no apps"),
        ("[ezhpcy]\nschema = 1\n[ezhpcy.app.lab]\n", "has no apps"),
    ],
)
def test_a_file_without_apps_says_what_ezhpcy_looks_for(
    tmp_path: Path, manifest: str, message: str
) -> None:
    text = problems(tmp_path, manifest)

    assert message in text
    assert '[ezhpcy.app.<name>."<version>"]' in text


@pytest.mark.parametrize("schema", ["2", "0", '"1"'])
def test_a_schema_other_than_1_names_the_supported_version(
    tmp_path: Path, schema: str
) -> None:
    message = problems(
        tmp_path, MINIMAL_APP.replace("schema = 1", f"schema = {schema}")
    )

    assert "ezhpcy.schema: unsupported schema" in message
    assert "reads schema = 1" in message


def test_a_missing_schema_is_an_error(tmp_path: Path) -> None:
    message = problems(tmp_path, MINIMAL_APP.replace("schema = 1\n", ""))

    assert "ezhpcy.schema: Field required" in message


def test_an_unknown_key_in_ezhpcy_names_its_path(tmp_path: Path) -> None:
    manifest = MINIMAL_APP.replace("schema = 1\n", "schema = 1\nschmea = 1\n")

    assert "ezhpcy.schmea: unknown key" in problems(tmp_path, manifest)


@pytest.mark.parametrize(
    ("addition", "path"),
    [
        ("rnu = 1", f"{LAB}.rnu"),
        ("run.workdri = '/x'", f"{LAB}.run.workdri"),
        ("ready.timeout = 3", f"{LAB}.ready.timeout"),
        ("environment.lock = 'x'", f"{LAB}.environment.lock"),
        ("assets = { include = ['*.toml'], files = [] }", f"{LAB}.assets.files"),
    ],
)
def test_an_unknown_key_in_an_app_names_its_path(
    tmp_path: Path, addition: str, path: str
) -> None:
    message = problems(tmp_path, MINIMAL_APP + addition + "\n")

    assert f"{path}: unknown key" in message


def test_an_unversioned_app_table_says_to_add_a_version(tmp_path: Path) -> None:
    """The draft's layout, `[ezhpcy.app.lab]` with the app's fields."""
    manifest = MINIMAL_APP.replace('[ezhpcy.app.lab."v1.0"]', "[ezhpcy.app.lab]")

    message = problems(tmp_path, manifest)

    assert "app lab has no version" in message
    assert '[ezhpcy.app.lab."v1.0"]' in message


def test_a_version_key_that_isnt_one_names_its_app(tmp_path: Path) -> None:
    message = problems(tmp_path, versions(("lab", "latest")))

    assert "ezhpcy.app.lab.latest: invalid version 'latest'" in message


def test_every_problem_is_listed(tmp_path: Path) -> None:
    manifest = MINIMAL_APP + "rnu = 1\nresources.cores = 0\n"

    message = problems(tmp_path, manifest)

    assert f"{LAB}.rnu: unknown key" in message
    assert f"{LAB}.resources.cores:" in message


def test_invalid_toml_is_reported(tmp_path: Path) -> None:
    assert "is not valid TOML" in problems(tmp_path, "[ezhpcy\n")


def test_a_missing_manifest_is_reported(tmp_path: Path) -> None:
    with pytest.raises(api.ManifestError, match="Could not read manifest"):
        Manifest.load(tmp_path / "nope.toml")


def test_a_manifest_error_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(api.ConfigError):
        Manifest.load(tmp_path / "nope.toml")


# Names


@pytest.mark.parametrize("name", ["-x", "x-", "_x", '"a@b"', '"a b"', '"é"'])
def test_an_invalid_app_name_is_an_error(tmp_path: Path, name: str) -> None:
    message = problems(tmp_path, versions((name, "v1.0")))

    assert "invalid app name" in message
    assert "Python packages" in message


@pytest.mark.parametrize(
    ("name", "normalised"),
    [
        ("lab", "lab"),
        ("0", "0"),
        ("Jupyter_Lab", "jupyter-lab"),
        ('"my.app--2"', "my-app-2"),
        ("a" * 80, "a" * 80),
    ],
)
def test_a_valid_app_name_loads_under_its_normalised_name(
    tmp_path: Path, name: str, normalised: str
) -> None:
    manifest = load(tmp_path, versions((name, "v1.0")))

    app = manifest.apps[normalised]["v1.0"]
    assert app.name == name.strip('"')
    assert app.normalised_name == normalised


def test_names_that_normalise_alike_are_one_app(tmp_path: Path) -> None:
    message = problems(tmp_path, versions(("My_App", "v1.0"), ("my-app", "v2.0")))

    assert "ezhpcy.app: apps 'My_App' and 'my-app' are one app" in message
    assert "'my-app'" in message


# Versions


@pytest.mark.parametrize(
    "version", ["1.0", "V1.0", "vv1.0", "v", "v1.0 ", "v 1.0", "v1.x", "version1"]
)
def test_an_invalid_version_is_an_error_naming_the_app(
    tmp_path: Path, version: str
) -> None:
    message = problems(tmp_path, versions(("lab", version)))

    # At the version's key: `ezhpcy.app.lab."v1.x"`.
    assert re.search(
        rf"ezhpcy\.app\.lab\..+: invalid version {re.escape(repr(version))}", message
    )
    assert "PEP 440" in message


@pytest.mark.parametrize(
    "version", ["v1", "v1.2.3", "v2.0rc1", "v1.0.post1", "v1.0.dev3", "v1!2.0"]
)
def test_a_valid_version_loads(tmp_path: Path, version: str) -> None:
    app = only(load(tmp_path, versions(("lab", version))))

    assert app.version == version


def test_versions_that_are_equal_as_pep_440_are_an_error(tmp_path: Path) -> None:
    message = problems(tmp_path, versions(("lab", "v1.2"), ("lab", "v1.2.0")))

    assert "app 'lab' has versions 'v1.2' and 'v1.2.0', which are one version" in (
        message
    )


def test_several_versions_of_one_app_load(tmp_path: Path) -> None:
    manifest = load(tmp_path, versions(("lab", "v1.0"), ("lab", "v2.0")))

    assert list(manifest.apps["lab"]) == ["v1.0", "v2.0"]


# Choosing from a configuration's manifests


def config_with(*manifests: Path, defaults: tuple[Path, ...] = ()) -> api.Config:
    config_file = manifests[0].parent.parent / "ezhpcy.toml"
    config_file.write_text("", encoding="utf-8")
    return api.Config.load(
        config_file, manifests=list(manifests), default_manifests=list(defaults)
    )


def test_the_newest_version_is_chosen_without_a_version(tmp_path: Path) -> None:
    config = config_with(
        write_project(
            tmp_path / "a",
            versions(("lab", "v1.10"), ("lab", "v1.9"), ("lab", "v2.0rc1")),
        )
    )

    # By PEP 440, not by text; and not a pre-release.
    assert config.app("lab").version == "v1.10"
    assert config.app("Lab").version == "v1.10"
    assert config.app("lab", "v1.9").version == "v1.9"
    # A pre-release only by its version, which is compared as PEP 440.
    assert config.app("lab", "v2.0rc1").version == "v2.0rc1"
    assert config.app("lab", "v2.0.0rc1").version == "v2.0rc1"


def test_only_pre_releases_are_chosen_only_by_version(tmp_path: Path) -> None:
    config = config_with(write_project(tmp_path / "a", versions(("lab", "v2.0rc1"))))

    with pytest.raises(api.ManifestError, match=re.escape("lab@v2.0rc1")):
        config.app("lab")


def test_an_unknown_app_or_version_lists_the_choices(tmp_path: Path) -> None:
    config = config_with(
        write_project(tmp_path / "a", versions(("lab", "v1.0"), ("board", "v0.1")))
    )

    with pytest.raises(
        api.ManifestError, match='there is no app "nope"; choose from: "board", "lab"'
    ):
        config.app("nope")
    with pytest.raises(
        api.ManifestError,
        match=re.escape('app "lab" has no version "v3"; choose from: "v1.0"'),
    ):
        config.app("lab", "v3")
    with pytest.raises(api.ManifestError, match="invalid version '3'"):
        config.app("lab", "3")


def test_without_manifests_there_are_no_apps(tmp_path: Path) -> None:
    config_file = tmp_path / "ezhpcy.toml"
    config_file.write_text("", encoding="utf-8")
    config = api.Config.load(config_file)

    assert config.apps == {}
    with pytest.raises(api.ManifestError, match="no manifest with apps was given"):
        config.app("lab")


def test_the_apps_of_several_manifests_are_merged(tmp_path: Path) -> None:
    config = config_with(
        write_project(tmp_path / "a", versions(("lab", "v1.0"))),
        write_project(tmp_path / "b", versions(("Lab", "v2.0"), ("board", "v0.1"))),
    )

    assert {name: list(by_version) for name, by_version in config.apps.items()} == {
        "lab": ["v1.0", "v2.0"],
        "board": ["v0.1"],
    }
    assert config.app("lab").environment.pixi.parent == tmp_path / "b"


def test_one_version_in_two_manifests_is_an_error_naming_both(tmp_path: Path) -> None:
    first = write_project(tmp_path / "a", versions(("lab", "v1.0")))
    second = write_project(tmp_path / "b", versions(("lab", "v1.0.0")))

    with pytest.raises(api.ManifestError) as error:
        config_with(first, second)

    message = str(error.value)
    assert "lab v1.0 of" in message
    assert "lab v1.0.0 of" in message
    assert str(first) in message
    assert str(second) in message


def test_a_users_manifest_replaces_a_default_version(tmp_path: Path) -> None:
    default = write_project(tmp_path / "default", versions(("lab", "v1.0")))
    user = write_project(tmp_path / "user", versions(("lab", "v1.0")))
    other_default = write_project(tmp_path / "other", versions(("lab", "v0.9")))

    config = config_with(user, defaults=(default, other_default))

    assert config.app("lab", "v1.0").environment.pixi.parent == tmp_path / "user"
    assert config.app("lab", "v0.9").environment.pixi.parent == tmp_path / "other"


def test_one_version_in_two_defaults_is_an_error(tmp_path: Path) -> None:
    first = write_project(tmp_path / "a", versions(("lab", "v1.0")))
    second = write_project(tmp_path / "b", versions(("lab", "v1.0")))
    user = write_project(tmp_path / "user", versions(("board", "v1.0")))

    with pytest.raises(api.ManifestError, match="are one version of one app"):
        config_with(user, defaults=(first, second))


def test_an_invalid_manifest_fails_loading_the_configuration(tmp_path: Path) -> None:
    with pytest.raises(api.ManifestError, match="invalid manifest"):
        config_with(write_project(tmp_path / "a", MINIMAL_APP + "rnu = 1\n"))


# The Pixi files


def test_pixi_lock_must_exist_beside_the_pixi_manifest(tmp_path: Path) -> None:
    message = problems(tmp_path, MINIMAL_APP, lock="")

    assert f"{LAB}.environment:" in message
    assert "pixi.lock does not exist" in message


def test_a_missing_pixi_manifest_is_an_error(tmp_path: Path) -> None:
    message = problems(tmp_path, MINIMAL_APP.replace('"pixi.toml"', '"sub/pixi.toml"'))

    assert f"{LAB}.environment.pixi: no such file" in message


def test_the_pixi_manifest_must_be_pixi_toml_or_pyproject_toml(tmp_path: Path) -> None:
    message = problems(tmp_path, MINIMAL_APP.replace('"pixi.toml"', '"env.toml"'))

    assert "must name a pixi.toml or pyproject.toml" in message


def test_a_pyproject_toml_needs_a_pixi_table(tmp_path: Path) -> None:
    files = {"pyproject.toml": '[project]\nname = "x"\n'}
    manifest = MINIMAL_APP.replace('"pixi.toml"', '"pyproject.toml"')

    assert "has no [tool.pixi] table" in problems(tmp_path, manifest, files=files)


def test_a_pyproject_toml_with_a_pixi_table_loads(tmp_path: Path) -> None:
    files = {"pyproject.toml": "[tool.pixi.workspace]\nchannels = []\n"}
    manifest = MINIMAL_APP.replace('"pixi.toml"', '"pyproject.toml"')

    app = only(load(tmp_path, manifest, files=files))

    assert app.environment.pixi.name == "pyproject.toml"


def test_the_lock_file_must_have_the_environment(tmp_path: Path) -> None:
    manifest = MINIMAL_APP + 'environment.environment = "cuda"\n'

    message = problems(tmp_path, manifest)

    assert "pixi.lock has no environment 'cuda' (it has: default)" in message


def test_the_lock_file_must_have_packages_for_linux(tmp_path: Path) -> None:
    lock = lock_file({"default": {"osx-arm64": ["conda: https://example.org/a.conda"]}})

    assert "no packages for linux-64" in problems(tmp_path, MINIMAL_APP, lock=lock)


@pytest.mark.parametrize("entry", ["pypi: ./", "pypi: ../lib", "conda: ./recipe"])
def test_path_dependencies_are_rejected(tmp_path: Path, entry: str) -> None:
    lock = lock_file(
        {"default": {"linux-64": ["conda: https://example.org/a.conda", entry]}}
    )

    message = problems(tmp_path, MINIMAL_APP, lock=lock)

    assert "has path dependencies" in message
    assert entry.split(": ")[1] in message


def test_path_dependencies_of_other_environments_dont_matter(tmp_path: Path) -> None:
    lock = lock_file(
        {
            "default": {"linux-64": ["conda: https://example.org/a.conda"]},
            "dev": {"linux-64": ["pypi: ./"]},
        }
    )

    assert only(load(tmp_path, MINIMAL_APP, lock=lock))


def test_git_and_url_dependencies_are_not_path_dependencies(tmp_path: Path) -> None:
    lock = lock_file(
        {
            "default": {
                "linux-64": [
                    "pypi: git+https://github.com/example/project.git#abc",
                    "pypi: https://files.pythonhosted.org/a.whl",
                ]
            }
        }
    )

    assert only(load(tmp_path, MINIMAL_APP, lock=lock))


def test_an_old_lock_file_is_rejected(tmp_path: Path) -> None:
    message = problems(tmp_path, MINIMAL_APP, lock=lock_file(version=5))

    assert "pixi.lock has version 5" in message


def test_the_example_lock_files_are_read() -> None:
    """Locked by Pixi itself (version 7), not written by `lock_file`."""
    text = (EXAMPLES / "dashboard" / "pixi.lock").read_text(encoding="utf-8")

    assert text.startswith("version: 7")
    assert only(Manifest.load(EXAMPLES / "dashboard"))


# Assets


def test_assets_are_matched_excluded_and_sorted(tmp_path: Path) -> None:
    files = {
        "main.py": "",
        "pages/a.py": "",
        "pages/sub/b.py": "",
        "pages/sub/notes.txt": "",
        "static/logo.svg": "",
        "static/css/site.css": "",
        "static/css/site.css.map": "",
    }
    manifest = MINIMAL_APP + (
        'assets.include = ["main.py", "pages/**/*.py", "static/**"]\n'
        'assets.exclude = ["**/*.map"]\n'
    )

    app = only(load(tmp_path, manifest, files=files))

    assert app.assets is not None
    assert [str(file.relative) for file in app.assets.files] == [
        "main.py",
        "pages/a.py",
        "pages/sub/b.py",
        "static/css/site.css",
        "static/logo.svg",
    ]
    assert all(file.path.is_file() for file in app.assets.files)


def test_an_asset_pattern_that_matches_nothing_is_an_error(tmp_path: Path) -> None:
    manifest = MINIMAL_APP + 'assets.include = ["main.py", "missing/*.py"]\n'

    message = problems(tmp_path, manifest, files={"main.py": ""})

    assert f"{LAB}.assets: pattern 'missing/*.py' matches no files" in message


@pytest.mark.parametrize("pattern", ["../secret.txt", "/etc/passwd", "a/../../b"])
def test_an_asset_pattern_must_stay_in_the_directory(
    tmp_path: Path, pattern: str
) -> None:
    manifest = MINIMAL_APP + f"assets.include = [{json.dumps(pattern)}]\n"

    assert "must be relative to the manifest's directory" in problems(
        tmp_path / "project", manifest
    )


def test_an_asset_outside_the_directory_through_a_link_is_an_error(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    write_project(project, MINIMAL_APP)
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    try:
        (project / "linked.txt").symlink_to(secret)
    except OSError:
        pytest.skip("this account can't create symbolic links")
    manifest = MINIMAL_APP + 'assets.include = ["*.txt"]\n'

    assert "linked.txt is outside the manifest's directory" in problems(
        project, manifest
    )


# Resources


def test_resources_cannot_set_the_queue(tmp_path: Path) -> None:
    message = problems(tmp_path, MINIMAL_APP + 'resources.queue = "gpu"\n')

    assert f"{LAB}.resources: queue is not set by an app" in message


@pytest.mark.parametrize(
    ("resources", "field"),
    [
        ("cores = 0", "cores"),
        ("gpus = -1", "gpus"),
        ('time_limit = "4h"', "time_limit"),
        ('memory = "lots"', "memory"),
    ],
)
def test_resources_are_validated_as_the_profiles_are(
    tmp_path: Path, resources: str, field: str
) -> None:
    message = problems(tmp_path, MINIMAL_APP + f"resources.{resources}\n")

    assert f"{LAB}.resources.{field}:" in message


def test_resources_take_the_manifest_over_the_profile_and_the_cli_over_both() -> None:
    profile = Resources(queue="hpc", cores=8, memory="32GB", gpus=0)
    app = AppResources(cores=2, time_limit=timedelta(hours=4))

    merged = merge_resources(profile, app)

    assert (merged.queue, merged.cores, merged.memory_bytes) == ("hpc", 2, 32 * 10**9)
    assert merged.time_limit == timedelta(hours=4)
    assert merged.model_fields_set == {"queue", "cores", "memory", "gpus", "time_limit"}


# Other fields


@pytest.mark.parametrize(
    ("addition", "message"),
    [
        ('run.workdir = "work"', f"{LAB}.run.workdir: must be an absolute path"),
        ('run.env.EZHPCY_TOKEN = "x"', "EZHPCY_TOKEN is reserved"),
        ('run.env."A-B" = "x"', "'A-B' is not an environment variable name"),
        ("run.command = []", f"{LAB}.run.command:"),
        ('ready.check = "banner"', "check = 'banner' needs a prefix"),
        ('ready.prefix = "SSH-"', "prefix only applies to check = 'banner'"),
        ('ready.path = "/api"', "path only apply to check = 'http'"),
        ('ready = { check = "http", path = "api" }', f"{LAB}.ready.path:"),
        ('ready = { check = "http", status = [300, 200] }', "status must be"),
        ("ready.timeout_seconds = 0", f"{LAB}.ready.timeout_seconds:"),
        ("access.local_port = 70000", f"{LAB}.access.local_port:"),
        ('endpoint.transport = "udp"', f"{LAB}.endpoint.transport:"),
    ],
)
def test_fields_are_validated(tmp_path: Path, addition: str, message: str) -> None:
    manifest = MINIMAL_APP
    if addition.startswith(("run.command", "endpoint.transport")):
        key = addition.split(" = ", maxsplit=1)[0]
        manifest = (
            "\n".join(
                line for line in manifest.splitlines() if not line.startswith(key)
            )
            + "\n"
        )
    assert message in problems(tmp_path, manifest + addition + "\n")


def test_a_banner_check_with_a_prefix_loads(tmp_path: Path) -> None:
    manifest = load(
        tmp_path, MINIMAL_APP + 'ready = { check = "banner", prefix = "SSH-" }\n'
    )

    assert only(manifest).ready.prefix == "SSH-"


# Placeholders


def test_render_fills_placeholders_and_keeps_literal_braces() -> None:
    values = {"socket": "/tmp/ezhpcy-1/app.sock", "token": "abc"}

    assert render("--sock={socket}", values) == "--sock=/tmp/ezhpcy-1/app.sock"
    assert render('{{"token": "{token}"}}', values) == '{"token": "abc"}'
    assert render("plain", values) == "plain"


@pytest.mark.parametrize(
    ("template", "message"),
    [
        ("{}", "{} is not a placeholder name"),
        ("{0}", "{0} is not a placeholder name"),
        ("{token.real}", "{token.real} is not a placeholder name"),
        ("{token!r}", "must not have a format"),
        ("{token:>5}", "must not have a format"),
        ("{token", "invalid braces"),
        ("token}", "invalid braces"),
    ],
)
def test_placeholders_are_bare_names(template: str, message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        placeholders(template)


@pytest.mark.parametrize(
    ("addition", "message"),
    [
        ('run.env.X = "{sockt}"', "run.env.X: unknown placeholder {sockt}"),
        (
            'run.command = ["app", "--token={token}"]',
            "run.command[1]: run.command does not take {token}",
        ),
        (
            'run.command = ["app", "{local_port}"]',
            "run.command[1]: run.command does not take {local_port}",
        ),
        (
            'access.url = "http://x/{run_dir}"',
            "access.url: access.url does not take {run_dir}",
        ),
        (
            'run.env.X = "{port}"',
            "run.env.X: {port} is not set for endpoint.transport = 'socket'",
        ),
        ('run.env.X = "{assets}"', "run.env.X: {assets} is not set without [assets]"),
        (
            'access.url = "http://x/{token:x}"',
            "access.url: placeholder {token} must not have a format",
        ),
        ('run.env.X = "{"', "run.env.X: invalid braces"),
    ],
)
def test_placeholders_are_checked_for_every_field(
    tmp_path: Path, addition: str, message: str
) -> None:
    manifest = MINIMAL_APP
    if addition.startswith("run.command"):
        manifest = manifest.replace(
            'run.command = ["jupyter", "lab", "--ServerApp.sock={socket}"]\n', ""
        )

    assert f"{LAB}.{message}" in problems(tmp_path, manifest + addition + "\n")


def test_socket_is_not_set_for_tcp(tmp_path: Path) -> None:
    manifest = MINIMAL_APP.replace('"socket"', '"tcp"')

    assert "{socket} is not set for endpoint.transport = 'tcp'" in problems(
        tmp_path, manifest
    )


def test_every_placeholder_problem_is_listed(tmp_path: Path) -> None:
    manifest = MINIMAL_APP + 'run.env.A = "{nope}"\naccess.url = "{port}"\n'

    message = problems(tmp_path, manifest)

    assert f"{LAB}.run.env.A: unknown placeholder {{nope}}" in message
    assert f"{LAB}.access.url: access.url does not take {{port}}" in message


# Keys


def test_the_environment_key_is_of_the_pixi_files_and_the_environment(
    tmp_path: Path,
) -> None:
    app = only(load(tmp_path, MINIMAL_APP))
    key = environment_key(app.environment)

    expected = hashlib.sha256()
    for field in (
        b"pixi.toml",
        app.environment.pixi.read_bytes(),
        app.environment.lock.read_bytes(),
        b"default",
    ):
        expected.update(len(field).to_bytes(8, "big") + field)
    assert key == expected.hexdigest()

    other = app.environment.model_copy(update={"environment": "cuda"})
    assert environment_key(other) != key
    app.environment.lock.write_text(lock_file() + "# changed\n", encoding="utf-8")
    assert environment_key(app.environment) != key


def test_the_assets_key_is_stable_across_platforms(tmp_path: Path) -> None:
    (tmp_path / "pages").mkdir()
    (tmp_path / "main.py").write_bytes(b"print('hi')\r\n")
    (tmp_path / "pages" / "a.py").write_bytes(b"x = 1\n")
    files = [
        AssetFile(PurePosixPath("pages/a.py"), tmp_path / "pages" / "a.py", False),
        AssetFile(PurePosixPath("main.py"), tmp_path / "main.py", False),
    ]

    key = assets_key(files)

    # The same on every platform: POSIX paths, line endings as they are, sorted.
    assert key == "f341abbe3c33532204880896c1bf7a691cf03bcd98365705598d97177d29019e"
    assert key == assets_key(list(reversed(files)))
    (tmp_path / "main.py").write_bytes(b"print('hi')\n")
    assert assets_key(files) != key
    executable = [files[0], AssetFile(files[1].relative, files[1].path, True)]
    assert assets_key(executable) != assets_key(files)


# The profile's [apps] table


def test_the_apps_root_must_be_absolute() -> None:
    assert AppsConfig(root=PurePosixPath("/work3/me/apps")).root == PurePosixPath(
        "/work3/me/apps"
    )
    with pytest.raises(ValueError, match="root must be an absolute path"):
        AppsConfig(root=PurePosixPath("apps"))
