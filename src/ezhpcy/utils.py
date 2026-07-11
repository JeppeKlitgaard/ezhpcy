from importlib.metadata import PackageNotFoundError, version


def ezhpcy_version() -> str:
    try:
        return version("ezhpcy")
    except PackageNotFoundError:
        return "unknown"
