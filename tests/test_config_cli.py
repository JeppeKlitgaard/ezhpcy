import tomllib
from pathlib import Path
from types import ModuleType

import pytest
from typer.testing import CliRunner

from ezhpcy.cli import app, list_profiles as config_list_profiles
from ezhpcy.cli.config import (
    edit as config_edit,
    load as config_load,
)
from ezhpcy.config import Config

runner = CliRunner()


def use_config_file(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, config_file: Path
) -> None:
    config = Config.from_mapping({"local_file": {"config_file": config_file}})
    monkeypatch.setattr(module, "config", config)


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
    assert "edit" in result.output
    assert "load" in result.output


def test_list_profiles_shows_local_profile_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config.from_mapping(
        {
            "profile": {
                "gpu": {
                    "description": "GPU jobs",
                    "inherit": "default",
                },
                "default": {"description": "General login"},
            },
        }
    )
    monkeypatch.setattr(config_list_profiles, "config", config)

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
    assert contents.startswith("### DTU HPC configuration for EzHPCy.\n")
    loaded = tomllib.loads(contents)
    assert loaded["profile"]["dtu-base"]["user"] == "alice"
    loaded_config = Config.from_mapping(loaded)
    assert str(loaded_config.resolve_profile("dtu-base").host) == "login.hpc.dtu.dk"
    assert loaded_config.resolve_profile("dtu-gpul40s").cores == 8
    a100sh = loaded_config.resolve_profile("dtu-a100sh")
    assert a100sh.interactive_submission_command == ["/lsf/local/bin/a100sh"]
    assert a100sh.lsf_application_profile == "qrsh"
    assert a100sh.lsf_submission_environment == {}
    assert a100sh.lsf_export_environment == []
    assert {
        "lsf_resource_reserve_per_task",
        "lsf_application_profile",
        "lsf_submission_environment",
        "lsf_export_environment",
    }.isdisjoint(loaded_config.profile["dtu-a100sh"].model_fields_set)
    assert str(loaded_config.resolve_profile("dtu-base-pbs").pbs_command_directory) == (
        "/opt/pbspro/bin"
    )


def test_config_load_existing_file_defaults_to_no(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "ezhpcy.toml"
    config_file.write_text(
        '[profile.old]\nhost = "old.example.com"\n',
        encoding="utf-8",
    )
    use_config_file(monkeypatch, config_load, config_file)

    result = runner.invoke(
        app, ["config", "load", "dtu", "--user", "alice"], input="\n"
    )

    assert result.exit_code == 1
    assert "Warning" in result.output
    assert '-host = "old.example.com"' in result.output
    assert '+host = "login.hpc.dtu.dk"' in result.output
    assert "[y/n] (n)" in result.output
    assert "configuration unchanged" in result.output
    assert config_file.read_text(encoding="utf-8") == (
        '[profile.old]\nhost = "old.example.com"\n'
    )


def test_config_load_yes_overwrites_existing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "ezhpcy.toml"
    config_file.write_text(
        '[profile.old]\nhost = "old.example.com"\n',
        encoding="utf-8",
    )
    use_config_file(monkeypatch, config_load, config_file)

    result = runner.invoke(app, ["config", "load", "DTU", "--user", "alice", "--yes"])

    assert result.exit_code == 0, result.output
    assert "Warning" in result.output
    assert "Overwrite the existing configuration?" not in result.output
    assert 'host = "login.hpc.dtu.dk"' in config_file.read_text(encoding="utf-8")


def test_config_load_rejects_unknown_preset() -> None:
    result = runner.invoke(app, ["config", "load", "unknown"])

    assert result.exit_code == 2
    output = result.output.casefold()
    assert "invalid value for 'preset:{" in output
    assert "'unknown' is not one of" in output
    assert "'dtu'" in output


def test_config_load_help_lists_available_presets() -> None:
    result = runner.invoke(app, ["config", "load", "--help"])

    assert result.exit_code == 0
    output = result.output.casefold()
    assert "preset:{" in output
    assert "dtu" in output


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
