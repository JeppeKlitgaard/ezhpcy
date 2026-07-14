from pathlib import Path

import pytest
from typer.testing import CliRunner

from ezhpcy.cli import app
from ezhpcy.cli.config import edit as config_edit

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
        assert startfile_calls == [
            (config_edit.DEFAULT_EDITOR, f'"{config_file}"')
        ]
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
    monkeypatch.setattr(
        config_edit.shutil, "which", lambda _editor: resolved_editor
    )
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
