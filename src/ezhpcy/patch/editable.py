import json
from importlib import metadata
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from ezhpcy.constants import PACKAGE_NAME


def _direct_url(package: str = PACKAGE_NAME) -> dict[str, object] | None:
    try:
        distribution = metadata.distribution(package)
    except metadata.PackageNotFoundError:
        return None

    direct_url = distribution.read_text("direct_url.json")
    if direct_url is None:
        return None

    return json.loads(direct_url)


def is_editable_install(package: str = PACKAGE_NAME) -> bool:
    direct_url = _direct_url(package)
    if direct_url is None:
        return False

    dir_info = direct_url.get("dir_info")
    if not isinstance(dir_info, dict):
        return False

    return dir_info.get("editable") is True


def editable_project_root(package: str = PACKAGE_NAME) -> Path:
    direct_url = _direct_url(package)
    if direct_url is None:
        raise RuntimeError(f"Could not locate installation metadata for {package}.")

    url = direct_url.get("url")
    if not isinstance(url, str):
        raise RuntimeError(f"Could not locate editable project root for {package}.")

    parsed = urlparse(url)
    if parsed.scheme != "file":
        raise RuntimeError(f"Editable project root for {package} is not a file URL.")

    return Path(url2pathname(parsed.path)).resolve()
