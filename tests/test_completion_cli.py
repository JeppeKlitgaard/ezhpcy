from pathlib import Path

import pytest

from ezhpcy.cli import _options, app
from ezhpcy.config import Config
from tests.support.cli import invoke


def test_install_completion_is_listed_with_the_global_options() -> None:
    result = invoke(["--help"])

    assert result.exit_code == 0, result
    global_options = result.stdout.split("Global Options")[1].split("Commands")[0]
    assert "--install-completion" in global_options


def test_install_completion_writes_the_script(tmp_path: Path) -> None:
    # Fish needs no startup file changes, so nothing outside `tmp_path` is touched.
    script = tmp_path / "ezhpcy.fish"

    result = invoke(["--install-completion", "--shell", "fish", "-o", str(script)])

    assert result.exit_code == 0, result
    assert str(script) in result.stdout
    contents = script.read_text(encoding="utf-8")
    assert contents == app.generate_completion(shell="fish")
    # Lazily registered commands are included.
    assert "tunnel" in contents
    assert "list-profiles" in contents


@pytest.mark.parametrize("command", ["provision", "prune", "tunnel"])
def test_profiles_complete_with_their_descriptions(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    config = Config.from_mapping(
        {"profile": {"gpu": {"description": "GPU jobs"}, "default": {}}}
    )
    monkeypatch.setattr(_options, "config", config)

    result = invoke(["__complete", command, ""])

    assert result.exit_code == 0, result
    candidates = result.stdout.split("\x1fbegin\n")[1].splitlines()
    assert candidates == ["gpu\tGPU jobs", "default"]
