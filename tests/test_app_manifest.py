"""
The apps manifest: loading, checking, versions and names, choosing an app from
a configuration's manifests, rendering placeholders, and its keys.
"""

import hashlib
import importlib.util
import json
import re
from datetime import timedelta
from pathlib import Path, PurePosixPath
from types import ModuleType

import pytest

import ezhpcy.app
from ezhpcy import api
from ezhpcy.app.manifest import (
    FIELD_PLACEHOLDERS,
    MANIFEST_FILE_NAME,
    App,
    AssetFile,
    Link,
    Manifest,
    assets_key,
    builtin_manifests,
    environment_key,
    find_manifests,
    placeholders,
    render,
    render_command,
)
from ezhpcy.types import AppsConfig
from tests.support.apps import MINIMAL_APP, lock_file, write_project

BUILTIN_MANIFESTS = builtin_manifests()
BUILTINS = Path(ezhpcy.app.__file__).parent / "builtins"
LAB = 'ezhpcy.app.lab."v1.0"'

# JupyterLab, written with sub-tables and with dotted keys.
LAB_SUB_TABLES = """\
[ezhpcy]
schema = 1

[ezhpcy.app.lab."v1.0"]
short_description = "JupyterLab with the project's environment"

[ezhpcy.app.lab."v1.0".environment]
pixi = "pixi.toml"
environment = "default"

[ezhpcy.app.lab."v1.0".run]
command = [
    "jupyter", "lab", "--no-browser",
    "--ServerApp.sock={{ socket }}",
    "--KernelManager.transport=ipc",
]
env = { JUPYTER_TOKEN = "{{ token }}", JUPYTER_RUNTIME_DIR = "{{ run_dir }}/jupyter" }

[ezhpcy.app.lab."v1.0".endpoint]
transport = "socket"
authenticates = true

[ezhpcy.app.lab."v1.0".ready]
check = "http"
path = "/api"
timeout = 180

[ezhpcy.app.lab."v1.0".access]
url = "http://127.0.0.1:{{ local_port }}/lab?token={{ token }}"
"""
LAB_DOTTED_KEYS = """\
[ezhpcy]
schema = 1

[ezhpcy.app.lab."v1.0"]
short_description = "JupyterLab with the project's environment"
environment.pixi = "pixi.toml"
environment.environment = "default"
run.command = [
    "jupyter", "lab", "--no-browser",
    "--ServerApp.sock={{ socket }}",
    "--KernelManager.transport=ipc",
]
run.env.JUPYTER_TOKEN = "{{ token }}"
run.env.JUPYTER_RUNTIME_DIR = "{{ run_dir }}/jupyter"
endpoint = { transport = "socket", authenticates = true }
ready.check = "http"
ready.path = "/api"
ready.timeout = 180
access.url = "http://127.0.0.1:{{ local_port }}/lab?token={{ token }}"
"""


def load(tmp_path: Path, manifest: str, **project: object) -> Manifest:
    return Manifest.load(write_project(tmp_path, manifest, **project))


def only(manifest: Manifest) -> App:
    """The manifest's one app version."""
    [versions] = manifest.apps.values()
    [app] = versions.values()
    assert isinstance(app, App)
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


@pytest.mark.parametrize(
    "path", BUILTIN_MANIFESTS, ids=lambda path: path.relative_to(BUILTINS).as_posix()
)
def test_the_builtins_load(path: Path) -> None:
    manifest = Manifest.load(path)

    assert manifest.apps
    for name, by_version in manifest.apps.items():
        for version, app in by_version.items():
            assert (app.normalised_id, app.version) == (name, version)
            if isinstance(app, Link):
                continue
            assert app.environment.pixi == path / "pixi.toml"
            assert app.environment.lock.is_file()


@pytest.mark.parametrize(
    "path", BUILTIN_MANIFESTS, ids=lambda path: path.relative_to(BUILTINS).as_posix()
)
def test_the_builtins_keys_hash_lf_text(path: Path) -> None:
    # A CRLF checkout would give a Windows client keys of its own, which find no
    # install made from Linux. Text is told from binary as Git's `text=auto` does.
    hashed: set[Path] = set()
    for by_version in Manifest.load(path).apps.values():
        for app in by_version.values():
            if isinstance(app, Link):
                continue
            hashed |= {app.environment.pixi, app.environment.lock}
            if app.assets is not None:
                hashed |= {file.path for file in app.assets.files}

    crlf = []
    for file in sorted(hashed):
        content = file.read_bytes()
        if b"\0" not in content[:8000] and b"\r\n" in content:
            crlf.append(file.relative_to(BUILTINS).as_posix())
    assert not crlf, "check out again with the .gitattributes' line endings"


def test_the_builtins_are_found_as_before() -> None:
    assert BUILTIN_MANIFESTS == sorted(
        path.parent for path in BUILTINS.rglob(MANIFEST_FILE_NAME)
    )


def app_directories(root: Path, *relatives: str) -> None:
    for relative in relatives:
        (root / relative).mkdir(parents=True, exist_ok=True)
        (root / relative / MANIFEST_FILE_NAME).write_text("", encoding="utf-8")


def link(path: Path, target: Path) -> None:
    try:
        path.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("this account can't create symbolic links")


def test_manifests_are_found_at_any_depth(tmp_path: Path) -> None:
    app_directories(tmp_path, "b", "a/one", "a/deep/er/two", "c/d/three")
    (tmp_path / "empty" / "dir").mkdir(parents=True)

    assert find_manifests(tmp_path) == [
        tmp_path / "a" / "deep" / "er" / "two",
        tmp_path / "a" / "one",
        tmp_path / "b",
        tmp_path / "c" / "d" / "three",
    ]


def test_an_apps_directory_is_not_looked_in(tmp_path: Path) -> None:
    app_directories(tmp_path, "app", "app/assets/nested")

    assert find_manifests(tmp_path) == [tmp_path / "app"]


def test_dot_directories_are_not_looked_in(tmp_path: Path) -> None:
    app_directories(tmp_path, ".pixi/envs/default/app", ".git/app", "app/v1")

    assert find_manifests(tmp_path) == [tmp_path / "app" / "v1"]


def test_an_empty_root_has_no_manifests(tmp_path: Path) -> None:
    assert find_manifests(tmp_path) == []
    assert find_manifests(tmp_path / "missing") == []


def test_the_root_may_be_an_apps_directory(tmp_path: Path) -> None:
    app_directories(tmp_path, ".", "nested")

    assert find_manifests(tmp_path) == [tmp_path]


def test_a_linked_apps_directory_is_found(tmp_path: Path) -> None:
    app_directories(tmp_path, "elsewhere/app")
    (tmp_path / "root").mkdir()
    link(tmp_path / "root" / "app", tmp_path / "elsewhere" / "app")

    assert find_manifests(tmp_path / "root") == [tmp_path / "root" / "app"]


def test_a_link_to_an_ancestor_does_not_loop(tmp_path: Path) -> None:
    app_directories(tmp_path, "apps/app")
    link(tmp_path / "apps" / "up", tmp_path)

    assert find_manifests(tmp_path) == [tmp_path / "apps" / "app"]


def test_a_directory_reached_twice_is_found_at_its_first_path(tmp_path: Path) -> None:
    # "a/x" sorts before "b", though it is deeper.
    app_directories(tmp_path, "b")
    (tmp_path / "a").mkdir()
    link(tmp_path / "a" / "x", tmp_path / "b")
    link(tmp_path / "c", tmp_path / "b")

    assert find_manifests(tmp_path) == [tmp_path / "a" / "x"]


def test_a_broken_link_is_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app_directories(tmp_path, "app")
    link(tmp_path / "broken", tmp_path / "nowhere")
    caplog.set_level("DEBUG", logger="ezhpcy.app.manifest")

    assert find_manifests(tmp_path) == [tmp_path / "app"]
    assert "Skipped a broken link" in caplog.text


def test_a_broken_link_for_a_manifest_still_counts(tmp_path: Path) -> None:
    # So that loading it reports the link, rather than the app going missing.
    (tmp_path / "app").mkdir()
    link(tmp_path / "app" / MANIFEST_FILE_NAME, tmp_path / "nowhere")

    assert find_manifests(tmp_path) == [tmp_path / "app"]


def test_the_notebook_apps_share_one_environment() -> None:
    manifest = Manifest.load(BUILTINS / "notebooks")

    lab = manifest.apps["lab"]["v1.0"]
    tensorboard = manifest.apps["tensorboard"]["v2.18"]
    assert environment_key(lab.environment) == environment_key(tensorboard.environment)


def test_sub_tables_and_dotted_keys_load_alike(tmp_path: Path) -> None:
    sub_tables = only(load(tmp_path / "a", LAB_SUB_TABLES))
    dotted = only(load(tmp_path / "b", LAB_DOTTED_KEYS))

    assert sub_tables.model_dump(exclude={"environment"}) == dotted.model_dump(
        exclude={"environment"}
    )
    assert sub_tables.run.env == {
        "JUPYTER_TOKEN": "{{ token }}",
        "JUPYTER_RUNTIME_DIR": "{{ run_dir }}/jupyter",
    }
    assert sub_tables.ready.accepts(200)
    assert sub_tables.ready.accepts(399)
    assert not sub_tables.ready.accepts(401)


def test_an_app_is_frozen_and_shown_as_id_and_version(tmp_path: Path) -> None:
    app = only(load(tmp_path, MINIMAL_APP))

    assert (app.id_, app.version, str(app)) == ("lab", "v1.0", "lab v1.0")
    with pytest.raises(ValueError, match="frozen"):
        app.short_description = "changed"


def test_an_app_is_hashable_with_its_tables_of_values() -> None:
    # Its arguments and actions are dicts.
    app = only(Manifest.load(BUILTINS / "zhome-quota"))
    again = only(Manifest.load(BUILTINS / "zhome-quota"))

    assert app.argument and app.action
    assert app == again and hash(app) == hash(again)
    assert {app: 1}[again] == 1


def test_an_app_has_a_name_and_descriptions_to_show(tmp_path: Path) -> None:
    app = only(
        load(
            tmp_path,
            MINIMAL_APP
            + 'name = "JupyterLab"\n'
            + 'short_description = "Notebooks, *in the browser*"\n'
            + 'long_description = """\nLine one.\n\n    ezhpcy tunnel\n"""\n',
        )
    )

    assert (app.id_, app.name) == ("lab", "JupyterLab")
    assert app.short_description == "Notebooks, *in the browser*"
    assert app.long_description == "Line one.\n\n    ezhpcy tunnel\n"


def test_an_app_needs_no_name_or_descriptions(tmp_path: Path) -> None:
    app = only(load(tmp_path, MINIMAL_APP))

    assert (app.name, app.short_description, app.long_description) == (
        None,
        None,
        None,
    )


@pytest.mark.parametrize("key", ["name", "short_description", "long_description"])
def test_an_empty_name_or_description_is_an_error(tmp_path: Path, key: str) -> None:
    message = problems(tmp_path, MINIMAL_APP + f'{key} = ""\n')

    assert f"{LAB}.{key}: String should have at least 1 character" in message


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
        ("ready.timeout_seconds = 3", f"{LAB}.ready.timeout_seconds"),
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
    """An app's fields straight in `[ezhpcy.app.lab]`, with no version."""
    manifest = MINIMAL_APP.replace('[ezhpcy.app.lab."v1.0"]', "[ezhpcy.app.lab]")

    message = problems(tmp_path, manifest)

    assert "app lab has no version" in message
    assert '[ezhpcy.app.lab."v1.0"]' in message


def test_a_version_key_that_isnt_one_names_its_app(tmp_path: Path) -> None:
    message = problems(tmp_path, versions(("lab", "latest")))

    assert "ezhpcy.app.lab.latest: invalid version 'latest'" in message


def test_every_problem_is_listed(tmp_path: Path) -> None:
    manifest = MINIMAL_APP + 'rnu = 1\nrun.workdir = "work"\n'

    message = problems(tmp_path, manifest)

    assert f"{LAB}.rnu: unknown key" in message
    assert f"{LAB}.run.workdir:" in message


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
    assert app.id_ == name.strip('"')
    assert app.normalised_id == normalised


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
        config_file,
        manifests=list(manifests),
        default_manifests=list(defaults),
        load_builtin_manifests=False,
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
    config = api.Config.load(config_file, load_builtin_manifests=False)

    assert config.available_apps == {}
    with pytest.raises(api.ManifestError, match="no manifest with apps was given"):
        config.app("lab")


def test_the_builtin_apps_are_loaded_by_default(tmp_path: Path) -> None:
    config_file = tmp_path / "ezhpcy.toml"
    config_file.write_text("", encoding="utf-8")
    config = api.Config.load(config_file)

    assert config.apps.load_builtin_manifests is True
    assert config.app("lab").environment.pixi.parent == BUILTINS / "notebooks"
    assert set(config.available_apps) == {
        name for path in BUILTIN_MANIFESTS for name in Manifest.load(path).apps
    }


def test_the_setting_or_the_argument_leaves_the_builtin_apps_out(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "ezhpcy.toml"
    config_file.write_text("[apps]\nload_builtin_manifests = false\n", encoding="utf-8")

    assert api.Config.load(config_file).available_apps == {}
    assert api.Config.load(config_file, load_builtin_manifests=True).app("lab")


def test_a_users_manifest_replaces_a_builtin_version(tmp_path: Path) -> None:
    user = write_project(tmp_path / "user", versions(("lab", "v1.0")))
    config_file = tmp_path / "ezhpcy.toml"
    config_file.write_text("", encoding="utf-8")

    config = api.Config.load(config_file, manifests=[user])

    assert config.app("lab").environment.pixi.parent == tmp_path / "user"
    assert config.app("tensorboard").environment.pixi.parent == BUILTINS / "notebooks"


def test_the_apps_of_several_manifests_are_merged(tmp_path: Path) -> None:
    config = config_with(
        write_project(tmp_path / "a", versions(("lab", "v1.0"))),
        write_project(tmp_path / "b", versions(("Lab", "v2.0"), ("board", "v0.1"))),
    )

    assert {
        name: list(by_version) for name, by_version in config.available_apps.items()
    } == {
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
    files = {
        "pyproject.toml": '[tool.pixi.workspace]\nchannels = []\nversion = "0.1.0"\n'
    }
    manifest = MINIMAL_APP.replace('"pixi.toml"', '"pyproject.toml"')

    app = only(load(tmp_path, manifest, files=files))

    assert app.environment.pixi.name == "pyproject.toml"


def test_a_workspace_needs_a_version(tmp_path: Path) -> None:
    message = problems(
        tmp_path, MINIMAL_APP, pixi_toml='[workspace]\nname = "project"\n'
    )

    assert "gives its workspace no version" in message
    assert 'such as version = "0.1.0"' in message


def test_a_workspace_version_is_one_a_directory_may_be_named_by(
    tmp_path: Path,
) -> None:
    message = problems(
        tmp_path, MINIMAL_APP, pixi_toml='[workspace]\nversion = "1/0"\n'
    )

    assert "gives its workspace version '1/0'" in message


@pytest.mark.parametrize(
    ("name", "content", "workspace"),
    [
        (
            "pixi.toml",
            '[workspace]\nname = "My_Tools"\nversion = "1.2"\n',
            ("my-tools", "1.2"),
        ),
        ("pixi.toml", '[project]\nname = "old"\nversion = "0.3"\n', ("old", "0.3")),
        ("pixi.toml", '[workspace]\nversion = "2.0"\n', ("project", "2.0")),
        (
            "pyproject.toml",
            '[project]\nname = "pkg"\nversion = "0.5"\n[tool.pixi.workspace]\n',
            ("pkg", "0.5"),
        ),
    ],
    ids=["workspace", "project", "unnamed", "pyproject"],
)
def test_the_workspace_is_named_and_versioned_as_pixi_has_it(
    tmp_path: Path, name: str, content: str, workspace: tuple[str, str]
) -> None:
    manifest = MINIMAL_APP.replace('"pixi.toml"', f'"{name}"')
    files = {name: content}

    app = only(load(tmp_path / "project", manifest, files=files))

    # Normalised, and without a name, its directory's.
    assert app.environment.workspace == workspace


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
    message = problems(tmp_path, MINIMAL_APP, lock=lock_file(version=6))

    assert "pixi.lock has version 6" in message


def test_the_builtin_lock_files_are_read() -> None:
    """Locked by Pixi itself (version 7), not written by `lock_file`."""
    text = (BUILTINS / "dashboard" / "pixi.lock").read_text(encoding="utf-8")

    assert text.startswith("version: 7")
    assert only(Manifest.load(BUILTINS / "dashboard"))


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


# Other fields


@pytest.mark.parametrize(
    ("addition", "message"),
    [
        ("resources.cores = 2", f"{LAB}.resources: unknown key"),
        ("requires.cuda = true", f"{LAB}.requires.cuda: unknown key"),
        ('run.workdir = "/work"', f"{LAB}.run.workdir: unknown key"),
        ('run.env.EZHPCY_TOKEN = "x"', "EZHPCY_TOKEN is reserved"),
        ('run.env."A-B" = "x"', "'A-B' is not an environment variable name"),
        ("run.command = []", f"{LAB}.run.command:"),
        ('ready.check = "banner"', "check = 'banner' needs a prefix"),
        ('ready.prefix = "SSH-"', "prefix only applies to check = 'banner'"),
        ('ready.path = "/api"', "path only apply to check = 'http'"),
        ('ready = { check = "http", path = "api" }', f"{LAB}.ready.path:"),
        ('ready = { check = "http", status = [300, 200] }', "status must be"),
        ("ready.timeout = 0", f"{LAB}.ready.timeout:"),
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


def test_the_ready_timeout_is_a_duration_in_seconds(tmp_path: Path) -> None:
    manifest = load(tmp_path, MINIMAL_APP + "ready.timeout = 2.5\n")

    assert only(manifest).ready.timeout == timedelta(seconds=2.5)


def test_an_app_requires_a_gpu_only_if_it_says_so(tmp_path: Path) -> None:
    plain = load(tmp_path / "plain", MINIMAL_APP)
    gpu = load(tmp_path / "gpu", MINIMAL_APP + "requires.gpu = true\n")

    assert not only(plain).requires.gpu
    assert only(gpu).requires.gpu


# Placeholders


def test_render_fills_placeholders_and_keeps_literal_braces() -> None:
    values = {"socket": "/tmp/ezhpcy-1/app.sock", "token": "abc"}

    assert (
        render("run.env", "--sock={{ socket }}", values)
        == "--sock=/tmp/ezhpcy-1/app.sock"
    )
    assert render("run.env", '{"token": "{{ token }}"}', values) == '{"token": "abc"}'
    assert render("run.env", "{not a placeholder}", values) == "{not a placeholder}"
    assert (
        render("run.env", "{% raw %}{{ .Names }}{% endraw %}", values) == "{{ .Names }}"
    )
    assert render("run.env", "plain", values) == "plain"


@pytest.mark.parametrize(
    ("template", "values", "rendered"),
    [
        ("{{ v }}", {"v": "a&b<c>'\""}, "a&b<c>'\""),
        ("{{ v }}", {"v": True}, "true"),
        ("{{ v }}", {"v": False}, "false"),
        ("{{ v }}", {"v": 3}, "3"),
        ("{{ v }}", {"v": 0.5}, "0.5"),
        ("{{ v * 60 }}", {"v": 3}, "180"),
        ("{{ {'fast': '-O3'}[v] }}", {"v": "fast"}, "-O3"),
        ("{% if v %}yes{% endif %}", {"v": False}, ""),
        ("{% if v is defined %}={{ v }}{% endif %}", {"v": 0}, "=0"),
        ("{% if v is defined %}={{ v }}{% endif %}", {}, ""),
        # What it is given isn't a template in turn.
        ("{{ v }}", {"v": "{{ 7 * 7 }} $(rm -rf /)"}, "{{ 7 * 7 }} $(rm -rf /)"),
        ("{{ v }}", {"name": "n", "source": "s", "v": "\0run_dir\0"}, "\0run_dir\0"),
    ],
)
def test_what_templates_can_do(
    template: str, values: dict[str, object], rendered: str
) -> None:
    assert render("run.env", template, values) == rendered


def test_a_value_named_as_the_renderers_parameters_is_rendered() -> None:
    assert (
        render("run.env", "{{ name }} {{ source }}", {"name": "n", "source": "s"})
        == "n s"
    )


@pytest.mark.parametrize(
    "template",
    [
        "{{ ''.__class__.__mro__ }}",
        "{{ cycler.__init__.__globals__ }}",
        "{{ range.__class__ }}",
        "{{ self }}",
    ],
)
def test_a_template_cant_reach_python(template: str) -> None:
    with pytest.raises(ValueError, match="undefined"):
        render("run.env", template, {})


def test_a_template_that_would_never_end_runs_out_of_fuel() -> None:
    forever = (
        "{% for i in range(100000) %}{% for j in range(100000) %}x{% endfor %}"
        "{% endfor %}"
    )

    with pytest.raises(ValueError, match="fuel"):
        render("run.env", forever, {})


def test_a_template_with_an_undefined_value_fails() -> None:
    with pytest.raises(ValueError, match="undefined"):
        render("run.env", "{{ nope }}", {})


def test_a_command_leaves_out_arguments_whose_blocks_render_empty() -> None:
    command = ["a", "{% if flag %}--flag{% endif %}", "{{ empty }}", "{{ value }}"]

    assert render_command(
        "run.command", command, {"flag": False, "empty": "", "value": "v"}
    ) == [
        "a",
        "",
        "v",
    ]
    assert render_command(
        "run.command", command, {"flag": True, "empty": "", "value": "v"}
    ) == [
        "a",
        "--flag",
        "",
        "v",
    ]


def test_render_gives_a_field_only_the_built_in_placeholders_it_takes() -> None:
    template = "{% if token is defined %}{{ token }}{% endif %}-{{ name }}"
    values = {"token": "secret", "name": "n"}

    assert render("run.command", template, values) == "-n"
    assert render("run.env", template, values) == "secret-n"


def test_placeholders_are_found_in_blocks_too() -> None:
    assert placeholders(
        "{% if a %}{{ b | upper }}{% endif %}{% for i in c %}{{ i }}{% endfor %}"
    ) == {"a", "b", "c"}
    assert placeholders("{not one} plain") == set()


@pytest.mark.parametrize("template", ["{{", "{% if a %}", "{{ a b }}"])
def test_a_broken_template_is_said_to_be(template: str) -> None:
    with pytest.raises(ValueError, match="syntax error"):
        placeholders(template)


@pytest.mark.parametrize(
    ("addition", "message"),
    [
        ('run.env.X = "{{ sockt }}"', "run.env.X: unknown placeholder {{ sockt }}"),
        (
            'run.command = ["app", "--token={{ token }}"]',
            "run.command[1]: run.command does not take {{ token }}",
        ),
        (
            'run.command = ["app", "{{ local_port }}"]',
            "run.command[1]: run.command does not take {{ local_port }}",
        ),
        (
            'access.url = "http://x/{{ run_dir }}"',
            "access.url: access.url does not take {{ run_dir }}",
        ),
        (
            'access.status_hints = ["{{ token }}", "{{ workdir }}"]',
            "access.status_hints[1]: access.status_hints does not take {{ workdir }}",
        ),
        (
            'ready = { check = "http", path = "/?port={{ local_port }}" }',
            "ready.path: ready.path does not take {{ local_port }}",
        ),
        (
            'run.env.X = "{{ port }}"',
            "run.env.X: {{ port }} is not set for endpoint.transport = 'socket'",
        ),
        (
            'run.env.X = "{{ assets }}"',
            "run.env.X: {{ assets }} is not set without [assets]",
        ),
        ('run.env.X = "{{"', "run.env.X: syntax error"),
    ],
)
def test_placeholders_are_checked_for_every_field(
    tmp_path: Path, addition: str, message: str
) -> None:
    manifest = MINIMAL_APP
    if addition.startswith("run.command"):
        manifest = manifest.replace(
            'run.command = ["jupyter", "lab", "--ServerApp.sock={{ socket }}"]\n', ""
        )

    assert f"{LAB}.{message}" in problems(tmp_path, manifest + addition + "\n")


ACTION_TABLE = '[ezhpcy.app.lab."v1.0".action.go]\n'
# A manifest with `{{ nope }}` in each field that takes placeholders.
UNKNOWN_PLACEHOLDER_IN = {
    "run.command": MINIMAL_APP.replace("{{ socket }}", "{{ nope }}"),
    "run.env": MINIMAL_APP + 'run.env.X = "{{ nope }}"\n',
    "ready.path": MINIMAL_APP + 'ready = { check = "http", path = "/{{ nope }}" }\n',
    "access.url": MINIMAL_APP + 'access.url = "http://x/{{ nope }}"\n',
    "access.status_hints": MINIMAL_APP + 'access.status_hints = ["{{ nope }}"]\n',
    "argument.default": (
        MINIMAL_APP
        + 'run.env.A = "{{ a }}"\n'
        + '[ezhpcy.app.lab."v1.0".argument.a]\ntype = "path"\ndefault = "/{{ nope }}"\n'
    ),
    "action.run.command": MINIMAL_APP + ACTION_TABLE + 'run.command = ["{{ nope }}"]\n',
    "action.run.env": (
        MINIMAL_APP + ACTION_TABLE + 'run.command = ["x"]\nrun.env.X = "{{ nope }}"\n'
    ),
    "action.trigger.every": (
        MINIMAL_APP
        + ACTION_TABLE
        + 'run.command = ["x"]\ntrigger.every = "{{ nope }}"\n'
    ),
    "action.input.default": (
        MINIMAL_APP
        + ACTION_TABLE
        + 'run.command = ["{{ a }}"]\n'
        + 'input.a = { type = "path", default = "/{{ nope }}" }\n'
    ),
}


@pytest.mark.parametrize("field", FIELD_PLACEHOLDERS)
def test_every_field_render_takes_is_checked_as_the_manifest_loads(
    tmp_path: Path, field: str
) -> None:
    assert "unknown placeholder {{ nope }}" in problems(
        tmp_path, UNKNOWN_PLACEHOLDER_IN[field]
    )


def test_socket_is_not_set_for_tcp(tmp_path: Path) -> None:
    manifest = MINIMAL_APP.replace('"socket"', '"tcp"')

    assert "{{ socket }} is not set for endpoint.transport = 'tcp'" in problems(
        tmp_path, manifest
    )


def test_every_placeholder_problem_is_listed(tmp_path: Path) -> None:
    manifest = MINIMAL_APP + 'run.env.A = "{{ nope }}"\naccess.url = "{{ port }}"\n'

    message = problems(tmp_path, manifest)

    assert f"{LAB}.run.env.A: unknown placeholder {{{{ nope }}}}" in message
    assert f"{LAB}.access.url: access.url does not take {{{{ port }}}}" in message


# Links

LINK_TABLE = '[ezhpcy.app.code."v1.0"]\n'
# A link, beside `MINIMAL_APP`'s lab.
LINK_AND_LAB = (
    MINIMAL_APP
    + "\n"
    + LINK_TABLE
    + 'access.url = "vscode://vscode-remote/ssh-remote+{{ alias }}/"\n'
    + 'access.status_hints = ["ssh {{ alias }}", "code --remote ssh-remote+{{ alias }}"]\n'
)


def test_a_table_with_nothing_to_run_is_a_link_to_the_alias(tmp_path: Path) -> None:
    manifest = load(tmp_path, LINK_AND_LAB)

    link = manifest.apps["code"]["v1.0"]
    assert isinstance(link, Link)
    assert str(link) == "code v1.0"
    assert link.url("node-1") == "vscode://vscode-remote/ssh-remote+node-1/"
    assert link.status_hints("node-1") == (
        "ssh node-1",
        "code --remote ssh-remote+node-1",
    )
    assert isinstance(manifest.apps["lab"]["v1.0"], App)


@pytest.mark.parametrize(
    ("addition", "message"),
    [
        ('short_description = "x"', '"v1.0".access: Field required'),
        ('access.url = "x"\naccess.local_port = 1', "access.local_port: unknown key"),
        ('access.url = "x"\nassets.include = ["x"]', "assets: unknown key"),
        (
            'access.url = "{{ token }}"',
            "access.url: {{ token }} is not set for a link, which runs nothing",
        ),
        (
            'access.url = "{{ local_port }}"',
            "access.url: {{ local_port }} is not set for a link, which runs nothing",
        ),
        (
            'access.url = "x"\naccess.status_hints = ["{{ alias }}", "{{ token }}"]',
            "access.status_hints[1]: {{ token }} is not set for a link, which runs nothing",
        ),
    ],
)
def test_a_link_has_only_a_url_to_the_alias(
    tmp_path: Path, addition: str, message: str
) -> None:
    manifest = "[ezhpcy]\nschema = 1\n" + LINK_TABLE + addition + "\n"

    assert message in problems(tmp_path, manifest)


def test_an_app_has_no_alias(tmp_path: Path) -> None:
    manifest = MINIMAL_APP + 'access.url = "http://{{ alias }}/"\n'

    assert (
        f"{LAB}.access.url: {{{{ alias }}}} is set only for links, which have no [run]"
    ) in problems(tmp_path, manifest)


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


def _quota_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "quota", BUILTINS / "zhome-quota" / "quota.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("usage", "shown"),
    [
        ("19.38G of 30.00G", "19.4/30.0G [18:04]"),
        ("512.00M of 30.00G", "512.0M/30.0G [18:04]"),
    ],
)
def test_the_quota_is_said_tersely(usage: str, shown: str) -> None:
    output = (
        f"You are using {usage} quota\n\n(Information updated ca. every 240 "
        "minutes. Last Update: 2026-11-01 18:04:17)\n"
    )

    assert _quota_script().terse(output) == shown


def test_quota_output_that_cant_be_read_is_not_said_tersely() -> None:
    assert _quota_script().terse("Something else entirely\n") is None
