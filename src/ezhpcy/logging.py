"""
EzHPCy's logger.

EzHPCy's modules log through `get_logger`, which also takes a message as a
template (see `ezhpcy.messages`): `logger.info(t"Run {'ssh gpu':suggestion}.")`.
Records carry it as plain text; a handler that styles it finds the template as
`template` on the record's `msg`.

Nothing here configures logging, or writes anywhere: that is up to the program.
"""

import logging
lazy from collections.abc import MutableMapping
lazy from string.templatelib import Template
lazy from typing import Any

lazy from ezhpcy.messages import plain

# `extra` for a record that is context for a failure, such as a line of an
# interactive job's output: to show before the next error, if it wasn't shown.
REPLAY_ON_ERROR = {"replay_on_error": True}


class _Logger(logging.LoggerAdapter[logging.Logger]):
    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        return (_LogMessage(msg) if isinstance(msg, Template) else msg), kwargs


def get_logger(name: str) -> _Logger:
    """The logger `name`, which also takes a message as a template."""
    return _Logger(logging.getLogger(name))


class _LogMessage:
    """A template message: plain text to every handler, styled by those that can."""

    def __init__(self, template: Template) -> None:
        self.template = template

    def __str__(self) -> str:
        return plain(self.template)
