import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

PACKAGE_NAME = "ezhpcy"
PAYLOAD_DIRECTORY = "_payload"


@contextmanager
def working_directory(path: str | Path) -> Iterator[None]:
    previous = Path.cwd()

    try:
        os.chdir(path)
        yield
    finally:
        os.chdir(previous)


class CustomBuildHook(BuildHookInterface):
    """Embed the project's sdist inside the standard (non-editable) wheel."""

    _temporary_directory: tempfile.TemporaryDirectory[str] | None = None

    def initialize(
        self,
        version: str,
        build_data: dict[str, Any],
    ) -> None:
        # Do not bundle the payload for editable installations.
        if version != "standard":
            return

        from hatchling.build import build_sdist

        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="ezhpcy-embedded-sdist-"
        )
        output_directory = Path(self._temporary_directory.name)

        with working_directory(self.root):
            sdist_filename = build_sdist(str(output_directory))

        sdist_path = output_directory / sdist_filename
        if not sdist_path.is_file():
            raise RuntimeError(f"Expected generated sdist at {sdist_path}")

        destination = f"{PACKAGE_NAME}/{PAYLOAD_DIRECTORY}/{sdist_filename}"
        build_data["force_include"][str(sdist_path)] = destination

    def finalize(
        self,
        version: str,
        build_data: dict[str, Any],
        artifact_path: str,
    ) -> None:
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()
            self._temporary_directory = None
