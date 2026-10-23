"""
Apps: servers such as JupyterLab or TensorBoard, described by a manifest on this
machine, and run on any host through EzHPCy's Pixi.

- `manifest` reads and checks the manifest (`ezhpcy-apps.toml`), and renders its
  placeholders.
- `provision` installs an app's environment and uploads its assets on its host.
- `run` starts an app on its host as a process, checks it is ready, and
  forwards a local port to it.
"""

lazy from string.templatelib import Template

from ezhpcy.messages import MessageError
from ezhpcy.types import ConfigError


class ManifestError(ConfigError):
    """A manifest is invalid, or names no app that was asked for."""


class AppFailed(MessageError, RuntimeError):
    """
    An app didn't become ready, or exited while starting. `stderr` holds the
    last lines of its stderr, if they could be read.
    """

    def __init__(self, message: Template, *, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr
