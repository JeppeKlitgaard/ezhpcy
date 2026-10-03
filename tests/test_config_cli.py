import subprocess
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

from ezhpcy.cli import list_profiles as config_list_profiles
from ezhpcy.cli.config import (
    edit as config_edit,
    load as config_load,
)
from ezhpcy.config import Config
from ezhpcy.types import SubmissionMode
from tests.support.cli import invoke


def use_config_file(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, config_file: Path
) -> None:
    config = Config.from_mapping({"local_file": {"config_file": config_file}})
    monkeypatch.setattr(module, "config", config)


def record_editor_runs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    windows: bool,
    executables: dict[str, str] | None = None,
    returncode: int = 0,
) -> list[list[str]]:
    """Replace the editor process and PATH lookup, returning the commands run."""
    runs: list[list[str]] = []

    def run(command: list[str], *, check: bool) -> subprocess.CompletedProcess:
        assert not check
        runs.append(command)
        return subprocess.CompletedProcess(command, returncode)

    monkeypatch.setattr(config_edit, "IS_WINDOWS", windows)
    monkeypatch.setattr(config_edit.shutil, "which", (executables or {}).get)
    monkeypatch.setattr(config_edit.subprocess, "run", run)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    return runs


@pytest.mark.parametrize("windows", [False, True])
@pytest.mark.parametrize(
    ("editor_argument", "environment", "expected_editor"),
    [
        ("code", {"EDITOR": "default-editor"}, "code"),
        (None, {"EDITOR": "default-editor"}, "default-editor"),
        (None, {"VISUAL": "visual", "EDITOR": "default-editor"}, "visual"),
        (None, {}, config_edit.DEFAULT_EDITOR),
    ],
)
def test_config_edit_creates_config_file_and_waits_for_editor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    windows: bool,
    editor_argument: str | None,
    environment: dict[str, str],
    expected_editor: str,
) -> None:
    config_file = tmp_path / "missing" / "ezhpcy.toml"
    use_config_file(monkeypatch, config_edit, config_file)
    runs = record_editor_runs(monkeypatch, windows=windows)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    arguments = ["config", "edit"]
    if editor_argument is not None:
        arguments.append(editor_argument)
    result = invoke(arguments)

    assert result.exit_code == 0, result
    assert config_file.is_file()
    assert runs == [[expected_editor, str(config_file)]]


@pytest.mark.parametrize(
    ("windows", "editor", "executables", "expected_arguments"),
    [
        (False, "code --wait", {"code": "/usr/bin/code"}, ["/usr/bin/code", "--wait"]),
        (False, '"/opt/My Editor/edit" -w', {}, ["/opt/My Editor/edit", "-w"]),
        (
            True,
            r'"C:\Program Files\Editor\editor.exe" --wait',
            {},
            [r"C:\Program Files\Editor\editor.exe", "--wait"],
        ),
        (
            True,
            "C:/Program Files/Editor/editor.exe",
            {
                "C:/Program Files/Editor/editor.exe": (
                    "C:/Program Files/Editor/editor.exe"
                )
            },
            ["C:/Program Files/Editor/editor.exe"],
        ),
        (
            True,
            "code --wait",
            {"code": r"C:\VS Code\bin\code.CMD"},
            [
                r"C:\VS Code\bin\code.CMD",
                "--wait",
            ],
        ),
    ],
)
def test_config_edit_splits_editor_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    windows: bool,
    editor: str,
    executables: dict[str, str],
    expected_arguments: list[str],
) -> None:
    config_file = tmp_path / "ezhpcy.toml"
    use_config_file(monkeypatch, config_edit, config_file)
    runs = record_editor_runs(monkeypatch, windows=windows, executables=executables)

    result = invoke(["config", "edit", editor])

    assert result.exit_code == 0, result
    assert runs == [[*expected_arguments, str(config_file)]]


def test_config_edit_does_not_interpret_editor_metacharacters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "ezhpcy.toml"
    use_config_file(monkeypatch, config_edit, config_file)
    runs = record_editor_runs(monkeypatch, windows=False)

    result = invoke(["config", "edit", "code;echo INJECTED"])

    assert result.exit_code == 0, result
    assert runs == [["code;echo", "INJECTED", str(config_file)]]


def test_config_edit_refuses_metacharacters_for_batch_file_editor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "config&echo INJECTED.toml"
    use_config_file(monkeypatch, config_edit, config_file)
    runs = record_editor_runs(
        monkeypatch, windows=True, executables={"code": r"C:\VS Code\bin\code.cmd"}
    )

    result = invoke(["config", "edit", "code"])

    assert result.exit_code == 2
    assert "cannot safely be given" in result.stderr
    assert runs == []


@pytest.mark.parametrize("editor", ['"unterminated', "   "])
def test_config_edit_rejects_invalid_editor_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, editor: str
) -> None:
    use_config_file(monkeypatch, config_edit, tmp_path / "ezhpcy.toml")
    runs = record_editor_runs(monkeypatch, windows=False)

    result = invoke(["config", "edit", editor])

    assert result.exit_code == 2
    assert "Invalid editor command" in result.stderr
    assert runs == []


def test_config_edit_reports_editor_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use_config_file(monkeypatch, config_edit, tmp_path / "ezhpcy.toml")
    record_editor_runs(monkeypatch, windows=False, returncode=3)

    result = invoke(["config", "edit", "vi"])

    assert result.exit_code == 1
    assert "editor exited with status 3" in result.stdout


def test_config_edit_reports_missing_editor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use_config_file(monkeypatch, config_edit, tmp_path / "ezhpcy.toml")
    record_editor_runs(monkeypatch, windows=False)

    def missing(command: list[str], *, check: bool) -> None:
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(config_edit.subprocess, "run", missing)

    result = invoke(["config", "edit", "no-such-editor"])

    assert result.exit_code == 2
    assert "Could not open configuration file" in result.stderr


def test_config_edit_is_listed_in_help() -> None:
    result = invoke(["config", "--help"])

    assert result.exit_code == 0, result
    assert "edit" in result.stdout
    assert "load" in result.stdout


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

    result = invoke(["list-profiles"])

    assert result.exit_code == 0, result
    lines = [" ".join(line.split()) for line in result.stdout.splitlines()]
    assert lines == [
        "Profile Description",
        "gpu GPU jobs",
        "default General login",
    ]


@pytest.mark.parametrize("preset", ["dtu", "Dtu", "DTU"])
def test_config_load_creates_dtu_config_case_insensitively(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, preset: str
) -> None:
    config_file = tmp_path / "missing" / "ezhpcy.toml"
    use_config_file(monkeypatch, config_load, config_file)

    result = invoke(["config", "load", preset, "--user", "alice"])

    assert result.exit_code == 0, result
    assert "loaded the DTU preset" in result.stdout
    contents = config_file.read_text(encoding="utf-8")
    assert contents.startswith("### DTU HPC configuration for EzHPCy.\n")
    loaded = tomllib.loads(contents)
    assert loaded["profile"]["dtu-base"]["connection"]["user"] == "alice"
    loaded_config = Config.from_mapping(loaded)
    dtu_base = loaded_config.resolve_profile("dtu-base")
    assert str(dtu_base.connection.host) == "login.hpc.dtu.dk"
    gpul40s = loaded_config.resolve_profile("dtu-gpul40s")
    assert gpul40s.resources.cores == 8
    assert gpul40s.scheduler.submission_mode is SubmissionMode.BATCH
    a100sh = loaded_config.resolve_profile("dtu-a100sh")
    assert a100sh.scheduler.submission_mode is SubmissionMode.INTERACTIVE
    assert a100sh.scheduler.interactive_submission_command == ["/lsf/local/bin/a100sh"]
    assert a100sh.lsf.application_profile == "qrsh"
    assert a100sh.lsf.submission_environment == {}
    assert a100sh.lsf.export_environment == []
    assert not loaded_config.profile["dtu-a100sh"].lsf.model_fields_set
    dtu_base_pbs = loaded_config.resolve_profile("dtu-base-pbs")
    assert str(dtu_base_pbs.pbs.command_directory) == "/opt/pbspro/bin"


def test_config_load_existing_file_defaults_to_no(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "ezhpcy.toml"
    config_file.write_text(
        '[profile.old]\nconnection.host = "old.example.com"\n',
        encoding="utf-8",
    )
    use_config_file(monkeypatch, config_load, config_file)

    result = invoke(["config", "load", "dtu", "--user", "alice"], input="\n")

    assert result.exit_code == 1
    assert "Warning" in result.stdout
    assert '-connection.host = "old.example.com"' in result.stdout
    assert '+connection.host = "login.hpc.dtu.dk"' in result.stdout
    assert "[y/n] (n)" in result.stdout
    assert "configuration unchanged" in result.stdout
    assert config_file.read_text(encoding="utf-8") == (
        '[profile.old]\nconnection.host = "old.example.com"\n'
    )


def test_config_load_yes_overwrites_existing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_file = tmp_path / "ezhpcy.toml"
    config_file.write_text(
        '[profile.old]\nconnection.host = "old.example.com"\n',
        encoding="utf-8",
    )
    use_config_file(monkeypatch, config_load, config_file)

    result = invoke(["config", "load", "DTU", "--user", "alice", "--yes"])

    assert result.exit_code == 0, result
    assert "Warning" in result.stdout
    assert "Overwrite the existing configuration?" not in result.stdout
    assert "Overwrite the existing configuration?" not in result.stderr
    assert 'host = "login.hpc.dtu.dk"' in config_file.read_text(encoding="utf-8")


def test_config_load_rejects_unknown_preset() -> None:
    result = invoke(["config", "load", "unknown"])

    assert result.exit_code == 2
    output = result.stderr.casefold()
    assert 'invalid value "unknown" for preset' in output
    assert '"dtu"' in output


def test_config_load_without_a_preset_lists_the_presets() -> None:
    result = invoke(["config", "load"])

    assert result.exit_code == 2
    output = result.stderr.casefold()
    assert "preset requires an argument" in output
    assert 'choose from: "dtu", "generic"' in output


def test_config_load_help_lists_available_presets() -> None:
    result = invoke(["config", "load", "--help"])

    assert result.exit_code == 0
    output = result.stdout.casefold()
    assert "[choices: dtu, generic]" in output
    assert "dtu" in output
