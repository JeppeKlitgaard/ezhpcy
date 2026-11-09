"""
`ezhpcy.api` for setting EzHPCy up: provisioning and pruning a host, whether this
machine is set up (`diagnose`), whether an alias could be used, and what a
configuration's manifests give. Uses `ezhpcy.api` alone, with the in-process SSH
servers of `tests.support`.
"""

from pathlib import Path, PurePosixPath

import pytest

from ezhpcy import api
from ezhpcy.api import frontend
from tests.support.api import LOGIN
from tests.support.apps import MINIMAL_APP, write_project
from tests.support.login import CACHE, STATE
from tests.support.sshserver import SSHTestServer, reply

pytestmark = pytest.mark.usefixtures("quiet_api")


def commands(server: SSHTestServer, name: str) -> list[list[str]]:
    return [argv for argv in server.commands_run if argv[0] == name]


async def test_provision_installs_what_a_worker_needs(login: SSHTestServer) -> None:
    login.command("bash -c", reply())
    login.command("env", reply())

    async with api.connect(LOGIN) as host:
        await host.provision()

    assert commands(login, "bash")
    assert commands(login, "env")


async def test_prune_stale_removes_other_versions_and_says_which(
    login: SSHTestServer,
) -> None:
    login.command("rm", reply())
    stale = CACHE / "ezhpcy" / "0.0.9"
    login.local_path(stale).mkdir(parents=True)

    async with api.connect(LOGIN) as host:
        removed = await host.prune_stale()

    assert removed == (stale,)
    assert commands(login, "rm") == [["rm", "-rf", "--", str(stale)]]


async def test_prune_all_removes_everything_under_the_apps_root_given(
    login: SSHTestServer,
) -> None:
    login.command("sh -c", reply())
    apps_root = PurePosixPath("/work3/alice/apps")

    async with api.connect(LOGIN) as host:
        await host.prune_all(apps_root=apps_root)

    [argv] = commands(login, "sh")
    assert argv[-3:] == [f"{CACHE}/ezhpcy", f"{STATE}/ezhpcy", str(apps_root)]


def test_diagnose_runs_only_as_far_as_it_is_read(
    isolated_local_files: object,
) -> None:
    checks = api.diagnose(api.Config.from_mapping({}))

    first = next(checks)

    assert first.status is api.CheckStatus.FAIL
    assert first.section == "configuration"
    assert "Config file not found" in str(first.message.strings)


def test_diagnose_says_how_each_check_went_by_section(
    isolated_local_files: api.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = api.Config.from_mapping({})
    config.local_file.config_file.parent.mkdir(parents=True, exist_ok=True)
    config.local_file.config_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("PATH", "")

    checks = list(api.diagnose(config))

    assert [(check.section, check.status) for check in checks] == [
        ("configuration", api.CheckStatus.OK),
        ("configuration", api.CheckStatus.WARN),
        ("ssh", api.CheckStatus.WARN),
    ]


def test_check_alias_refuses_what_a_host_block_cannot_name() -> None:
    frontend.check_alias("gpu", LOGIN)

    with pytest.raises(ValueError, match="invalid host alias"):
        frontend.check_alias("not an alias", LOGIN)
    with pytest.raises(ValueError, match="non-whitespace"):
        frontend.check_alias("gpu", LOGIN.model_copy(update={"user": "al ice"}))


def test_with_manifests_is_a_copy_with_their_apps(tmp_path: Path) -> None:
    manifest = write_project(tmp_path, MINIMAL_APP)
    config = api.Config.from_mapping({})

    with_apps = config.with_manifests([manifest], load_builtin_manifests=False)

    assert list(with_apps.available_apps) == ["lab"]
    assert str(with_apps.app("lab")) == "lab v1.0"
    assert config.available_apps == {}


def test_with_manifests_reads_the_builtins_as_the_configuration_says() -> None:
    settings = {"apps": {"load_builtin_manifests": False}}

    assert not api.Config.from_mapping(settings).with_manifests().available_apps
    assert api.Config.from_mapping({}).with_manifests().available_apps
