"""
The CLI is a client of `ezhpcy.api` and `ezhpcy.api.frontend`: it imports nothing
else of the package, and nothing below it imports the CLI, or what only a terminal
needs.
"""

import ast
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

import ezhpcy
from ezhpcy import api
from ezhpcy.api import frontend

PACKAGE = Path(ezhpcy.__file__).parent
CLI = PACKAGE / "cli"
BELOW_THE_CLI = [
    path
    for path in PACKAGE.rglob("*.py")
    if CLI not in path.parents and "builtins" not in path.parts
]


def imported_modules(path: Path) -> Iterator[str]:
    """Each module `path` imports, `lazy` or in a function, with what it imports."""
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{path} has a relative import"
            assert node.module is not None
            yield node.module
            yield from (f"{node.module}.{alias.name}" for alias in node.names)


def within(module: str, package: str) -> bool:
    return module == package or module.startswith(f"{package}.")


@pytest.mark.parametrize("path", sorted(CLI.rglob("*.py")), ids=lambda path: path.stem)
def test_the_cli_imports_only_the_api_and_itself(path: Path) -> None:
    modules = [module for module in imported_modules(path) if within(module, "ezhpcy")]

    assert not [
        module
        for module in modules
        if module != "ezhpcy"
        and not within(module, "ezhpcy.api")
        and not within(module, "ezhpcy.cli")
    ]


@pytest.mark.parametrize("path", BELOW_THE_CLI, ids=lambda path: path.stem)
def test_nothing_below_the_cli_imports_it(path: Path) -> None:
    assert not [
        module for module in imported_modules(path) if within(module, "ezhpcy.cli")
    ]


PUBLIC_MODULES = [api, frontend]


@pytest.mark.parametrize(
    ("module", "name"),
    [(module, name) for module in PUBLIC_MODULES for name in module.__all__],
    ids=lambda value: value if isinstance(value, str) else value.__name__,
)
def test_every_public_name_resolves(module: ModuleType, name: str) -> None:
    # The names are lazy, so one that is broken would otherwise fail only on use.
    assert getattr(module, name) is not None


def test_no_name_is_public_twice() -> None:
    assert not set(api.__all__) & set(frontend.__all__)


def test_the_api_loads_nothing_only_a_terminal_needs() -> None:
    script = (
        "import sys\n"
        "from ezhpcy import api\n"
        "from ezhpcy.api import frontend\n"
        "for module in (api, frontend):\n"
        "    for name in module.__all__:\n"
        "        getattr(module, name)\n"
        "print(*[m for m in ('cyclopts', 'rich', 'ezhpcy.cli') if m in sys.modules])\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True
    )

    assert result.stdout.strip() == ""
