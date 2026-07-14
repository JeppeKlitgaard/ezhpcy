import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ezhpcy.cli import app
from ezhpcy.cli.config import edit as config_edit, load as config_load
from ezhpcy.config import ConnectionInfo, HPCConfig

runner = CliRunner()


@pytest.mark.parametrize("editor", ["code", "C:/Program Files/Editor/editor.exe", None])
def test_config_edit_creates_and_opens_config_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, editor: str | None
) -> None:
    config_file = tmp_path / "missing" / "ezhpcy.toml"
    process_calls: list[list[str]] = []
    startfile_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(config_edit.config.local_file, "config_file", config_file)
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


@pytest.mark.parametrize("preset", ["dtu", "Dtu", "DTU"])
def test_config_load_creates_dtu_config_case_insensitively(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, preset: str
) -> None:
    config_file = tmp_path / "missing" / "ezhpcy.toml"
    monkeypatch.setattr(config_load.config.local_file, "config_file", config_file)

    result = runner.invoke(app, ["config", "load", preset])

    assert result.exit_code == 0, result.output
    assert "loaded the DTU preset" in result.output
    contents = config_file.read_text(encoding="utf-8")
    assert contents == (
        "# DTU HPC configuration for ezhpcy.\n"
        "\n"
        "[connection]\n"
        'host = "login2.hpc.dtu.dk"\n'
        "\n"
        "[hpc]\n"
        'login_node_pattern = "^hpclogin\\\\d+$"\n'
        "\n"
        'lsf_queue = "hpcint"\n'
        "lsf_resource_reserve_per_task = true\n"
        'lsf_application_profile = "qrsh"\n'
        'lsf_submission_environment = { ESUB_BYPASS = "1", ESUB_QUIET = "1", LSF_QRSH = "true" }\n'
        'lsf_export_environment = ["TERM", "LSF_QRSH"]\n'
        "\n"
        'pbs_queue = "workq"\n'
        'pbs_command_directory = "/opt/pbspro/bin"\n'
        "\n"
        "queue_timeout_seconds = 900\n"
        "worker_startup_timeout_seconds = 60\n"
    )
    loaded = tomllib.loads(contents)
    connection = ConnectionInfo.model_validate(loaded["connection"])
    hpc = HPCConfig.model_validate(loaded["hpc"])
    assert str(connection.host) == "login2.hpc.dtu.dk"
    assert hpc.login_node_pattern.pattern == r"^hpclogin\d+$"


def test_config_load_existing_file_defaults_to_no(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "ezhpcy.toml"
    config_file.write_text('[connection]\nhost = "old.example.com"\n', encoding="utf-8")
    monkeypatch.setattr(config_load.config.local_file, "config_file", config_file)

    result = runner.invoke(app, ["config", "load", "dtu"], input="\n")

    assert result.exit_code == 1
    assert "Warning" in result.output
    assert '-host = "old.example.com"' in result.output
    assert '+host = "login2.hpc.dtu.dk"' in result.output
    assert "[y/n] (n)" in result.output
    assert "configuration unchanged" in result.output
    assert config_file.read_text(encoding="utf-8") == (
        '[connection]\nhost = "old.example.com"\n'
    )


def test_config_load_yes_overwrites_existing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "ezhpcy.toml"
    config_file.write_text('[connection]\nhost = "old.example.com"\n', encoding="utf-8")
    monkeypatch.setattr(config_load.config.local_file, "config_file", config_file)

    result = runner.invoke(app, ["config", "load", "DTU", "--yes"])

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
    monkeypatch.setattr(config_edit.config.local_file, "config_file", config_file)
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
    monkeypatch.setattr(config_edit.config.local_file, "config_file", config_file)
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
    monkeypatch.setattr(config_edit.config.local_file, "config_file", config_file)
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
