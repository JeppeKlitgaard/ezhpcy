"""
Apps: servers such as JupyterLab or TensorBoard, described by a manifest on this
machine, and run on any host through EzHPCy's Pixi.

- `manifest` reads and checks the manifest (`ezhpcy-apps.toml`), and renders its
  placeholders.
- `provision` installs an app's environment and uploads its assets on its host.
"""

from ezhpcy.types import ConfigError


class ManifestError(ConfigError):
    """A manifest is invalid, or names no app that was asked for."""
