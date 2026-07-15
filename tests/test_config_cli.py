import tomllib
from pathlib import Path
from types import ModuleType

import pytest
from typer.testing import CliRunner

from ezhpcy.cli import app, list_profiles as config_list_profiles
from ezhpcy.cli.config import (
    dir as config_dir,
    edit as config_edit,
    load as config_load,
)
from ezhpcy.config import LocalConfig

runner = CliRunner()


def use_config_file(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, config_file: Path
) -> None:
    config = LocalConfig.from_mapping({"local_file": {"config_file": config_file}})
    monkeypatch.setattr(module, "get_config", lambda: config)


@pytest.mark.parametrize("editor", ["code", "C:/Program Files/Editor/editor.exe", None])
def test_config_edit_creates_and_opens_config_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, editor: str | None
) -> None:
    config_file = tmp_path / "missing" / "ezhpcy.toml"
    process_calls: list[list[str]] = []
    startfile_calls: list[tuple[str, str]] = []
    use_config_file(monkeypatch, config_edit, config_file)
    monkeypatch.setattr(config_edit.shutil, "which", lambda _editor: None)
    monkeypatch.setattr(
        config_edit.subprocess,
        "Popen",
        lambda command: process_calls.append(command),
    )
    monkeypatch.setattr(
        config_edit.os,
        "startfile",
        lambda executable, *, arguments: startfile_calls.append(
            (executable, arguments)
        ),
        raising=False,
    )
    monkeypatch.setenv("EDITOR", "default-editor")

    arguments = ["config", "edit"]
    if editor is not None:
        arguments.append(editor)
    result = runner.invoke(app, arguments)

    assert result.exit_code == 0, result.output
    assert config_file.is_file()
    selected_editor = editor or "default-editor"
    if config_edit.IS_WINDOWS:
        assert startfile_calls == [(selected_editor, f'"{config_file}"')]
        assert process_calls == []
    else:
        assert process_calls == [[selected_editor, str(config_file)]]
        assert startfile_calls == []


def test_config_edit_is_listed_in_help() -> None:
    result = runner.invoke(app, ["config", "--help"])

    assert result.exit_code == 0, result.output
    assert "dir" in result.output
    assert "edit" in result.output
    assert "load" in result.output


def test_config_dir_prints_config_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_directory = tmp_path / "custom config"
    config = LocalConfig.from_mapping({"local_file": {"config_dir": config_directory}})
    monkeypatch.setattr(config_dir, "get_config", lambda: config)

    result = runner.invoke(app, ["config", "dir"])

    assert result.exit_code == 0, result.output
    assert result.output == f"{config_directory}\n"


def test_list_profiles_shows_local_profile_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = LocalConfig.from_mapping(
        {
            "default_profile": "default",
            "profile": {
                "gpu": {
                    "description": "GPU jobs",
                    "inherit": "default",
                },
                "default": {"description": "General login"},
            },
        }
    )
    monkeypatch.setattr(config_list_profiles, "get_config", lambda: config)

    result = runner.invoke(app, ["list-profiles"])

    assert result.exit_code == 0, result.output
    lines = [" ".join(line.split()) for line in result.output.splitlines()]
    assert lines == [
        "Profile Description",
        "default General login",
        "gpu GPU jobs",
    ]


@pytest.mark.parametrize("preset", ["dtu", "Dtu", "DTU"])
def test_config_load_creates_dtu_config_case_insensitively(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, preset: str
) -> None:
    config_file = tmp_path / "missing" / "ezhpcy.toml"
    use_config_file(monkeypatch, config_load, config_file)

    result = runner.invoke(app, ["config", "load", preset, "--user", "alice"])

    assert result.exit_code == 0, result.output
    assert "loaded the DTU preset" in result.output
    contents = config_file.read_text(encoding="utf-8")
    assert contents == (
        "# DTU HPC configuration for ezhpcy.\n"
        "\n"
        'default_profile = "default"\n'
        'log_level = "INFO"\n'
        "auto_provision = true\n"
        "\n"
        "[profile.default]\n"
        'description = "DTU LSF interactive queue"\n'
        'host = "login2.hpc.dtu.dk"\n'
        'user = "alice"\n'
        "\n"
        'scheduler = "LSF"\n'
        'queue = "hpcint"\n'
        "\n"
        "lsf_resource_reserve_per_task = true\n"
        'lsf_application_profile = "qrsh"\n'
        'lsf_submission_environment = { ESUB_BYPASS = "1", ESUB_QUIET = "1", LSF_QRSH = "true" }\n'
        'lsf_export_environment = ["TERM", "LSF_QRSH"]\n'
        "\n"
        "queue_timeout_seconds = 900\n"
        "worker_startup_timeout_seconds = 60\n"
        "\n"
        "[profile.pbs]\n"
        'description = "DTU PBS work queue"\n'
        'inherit = "default"\n'
        'scheduler = "PBS"\n'
        'queue = "workq"\n'
        'pbs_command_directory = "/opt/pbspro/bin"\n'
        "\n"
        "[profile.gpul40s]\n"
        'description = "DTU L40S GPU queue"\n'
        'inherit = "default"\n'
        "\n"
        'queue = "gpul40s"\n'
        "cores = 8\n"
        'time_limit = "1:00"\n'
        'memory = "32GB"\n'
    )
    loaded = tomllib.loads(contents)
    loaded_config = LocalConfig.from_mapping(loaded)
    assert str(loaded_config.resolve_profile().host) == "login2.hpc.dtu.dk"
    assert loaded_config.resolve_profile("gpul40s").cores == 8
    assert str(loaded_config.resolve_profile("pbs").pbs_command_directory) == (
        "/opt/pbspro/bin"
    )


def test_config_load_existing_file_defaults_to_no(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "ezhpcy.toml"
    config_file.write_text(
        'default_profile = "old"\n[profile.old]\nhost = "old.example.com"\n',
        encoding="utf-8",
    )
    use_config_file(monkeypatch, config_load, config_file)

    result = runner.invoke(
        app, ["config", "load", "dtu", "--user", "alice"], input="\n"
    )

    assert result.exit_code == 1
    assert "Warning" in result.output
    assert '-host = "old.example.com"' in result.output
    assert '+host = "login2.hpc.dtu.dk"' in result.output
    assert "[y/n] (n)" in result.output
    assert "configuration unchanged" in result.output
    assert config_file.read_text(encoding="utf-8") == (
        'default_profile = "old"\n[profile.old]\nhost = "old.example.com"\n'
    )


def test_config_load_yes_overwrites_existing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "ezhpcy.toml"
    config_file.write_text(
        'default_profile = "old"\n[profile.old]\nhost = "old.example.com"\n',
        encoding="utf-8",
    )
    use_config_file(monkeypatch, config_load, config_file)

    result = runner.invoke(app, ["config", "load", "DTU", "--user", "alice", "--yes"])

    assert result.exit_code == 0, result.output
    assert "Warning" in result.output
    assert "Overwrite the existing configuration?" not in result.output
    assert 'host = "login2.hpc.dtu.dk"' in config_file.read_text(encoding="utf-8")


def test_config_load_rejects_unknown_preset() -> None:
    result = runner.invoke(app, ["config", "load", "unknown"])

    assert result.exit_code == 2
    assert "Unknown preset 'unknown'" in result.output
    assert "DTU" in result.output


def test_config_edit_creates_file_and_uses_platform_editor_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "missing" / "ezhpcy.toml"
    process_calls: list[list[str]] = []
    startfile_calls: list[tuple[str, str]] = []
    use_config_file(monkeypatch, config_edit, config_file)
    monkeypatch.setattr(config_edit.shutil, "which", lambda _editor: None)
    monkeypatch.setattr(
        config_edit.subprocess,
        "Popen",
        lambda command: process_calls.append(command),
    )
    monkeypatch.setattr(
        config_edit.os,
        "startfile",
        lambda executable, *, arguments: startfile_calls.append(
            (executable, arguments)
        ),
        raising=False,
    )
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)

    result = runner.invoke(app, ["config", "edit"])

    assert result.exit_code == 0, result.output
    assert config_file.is_file()
    if config_edit.IS_WINDOWS:
        assert startfile_calls == [(config_edit.DEFAULT_EDITOR, f'"{config_file}"')]
        assert process_calls == []
    else:
        assert process_calls == [[config_edit.DEFAULT_EDITOR, str(config_file)]]
        assert startfile_calls == []


def test_config_edit_uses_non_shell_windows_launcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "config&echo INJECTED.toml"
    resolved_editor = "C:/Program Files/Microsoft VS Code/bin/code"
    startfile_calls: list[tuple[str, str]] = []
    use_config_file(monkeypatch, config_edit, config_file)
    monkeypatch.setattr(config_edit, "IS_WINDOWS", True)
    monkeypatch.setattr(config_edit.shutil, "which", lambda _editor: resolved_editor)
    monkeypatch.setattr(
        config_edit.os,
        "startfile",
        lambda executable, *, arguments: startfile_calls.append(
            (executable, arguments)
        ),
        raising=False,
    )

    result = runner.invoke(app, ["config", "edit", "code"])

    assert result.exit_code == 0, result.output
    assert startfile_calls == [(resolved_editor, f'"{config_file}"')]


def test_config_edit_does_not_interpret_editor_metacharacters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "ezhpcy.toml"
    startfile_calls: list[tuple[str, str]] = []
    use_config_file(monkeypatch, config_edit, config_file)
    monkeypatch.setattr(config_edit, "IS_WINDOWS", True)
    monkeypatch.setattr(config_edit.shutil, "which", lambda _editor: None)
    monkeypatch.setattr(
        config_edit.os,
        "startfile",
        lambda executable, *, arguments: startfile_calls.append(
            (executable, arguments)
        ),
        raising=False,
    )

    result = runner.invoke(app, ["config", "edit", "code&echo INJECTED"])

    assert result.exit_code == 0, result.output
    assert startfile_calls == [("code&echo INJECTED", f'"{config_file}"')]
