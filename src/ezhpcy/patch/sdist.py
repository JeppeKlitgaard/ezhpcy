import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

from ezhpcy.constants import PACKAGE_NAME
from ezhpcy.patch.editable import editable_project_root, is_editable_install

PAYLOAD_RESOURCE_DIR = "_payload"


@contextmanager
def working_directory(path: Path) -> Iterator[None]:
    previous = Path.cwd()

    try:
        os.chdir(path)
        yield
    finally:
        os.chdir(previous)


@contextmanager
def _build_sdist_from_project(project_root: Path) -> Iterator[Path]:
    try:
        from hatchling.build import build_sdist
    except ImportError as exc:
        raise RuntimeError(
            "Editable ezhpcy installs need hatchling available to build an sdist."
        ) from exc

    with tempfile.TemporaryDirectory(prefix="ezhpcy-editable-sdist-") as temp_dir:
        output_directory = Path(temp_dir)

        with working_directory(project_root):
            sdist_filename = build_sdist(str(output_directory))

        sdist_path = output_directory / sdist_filename
        if not sdist_path.is_file():
            raise RuntimeError(f"Expected generated sdist at {sdist_path}")

        yield sdist_path


def _bundled_sdist() -> resources.abc.Traversable:
    payload_dir = resources.files(PACKAGE_NAME).joinpath(PAYLOAD_RESOURCE_DIR)
    if not payload_dir.is_dir():
        raise RuntimeError(
            "No bundled sdist found. Build and install ezhpcy from a standard wheel, "
            "or use an editable install with hatchling available."
        )

    payloads = [
        path
        for path in payload_dir.iterdir()
        if path.is_file() and path.name.endswith(".tar.gz")
    ]
    if len(payloads) != 1:
        raise RuntimeError(
            f"Expected exactly one bundled sdist, found {len(payloads)}."
        )

    return payloads[0]


@contextmanager
def sdist_for_current_installation() -> Iterator[resources.abc.Traversable | Path]:
    if is_editable_install():
        with _build_sdist_from_project(editable_project_root()) as sdist_path:
            yield sdist_path
        return

    yield _bundled_sdist()
