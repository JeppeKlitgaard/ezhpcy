"""
What a frontend of `ezhpcy.api` needs to talk to its user as EzHPCy's CLI does,
which a script that only drives the API doesn't: messages in parts to style
(`message_parts`), EzHPCy's logger and log file (`get_logger`, `LogFile`), a
password read from where a `Login` says (`read_password`), the keyring's service
(`KEYRING_SERVICE_NAME`), this machine's ID (`local_machine_id`), and checks of
an alias a user typed before it is reserved (`check_alias`, `probe_alias`,
`alias_digest`).

    from ezhpcy.api import frontend

    logging.getLogger("ezhpcy").addHandler(frontend.LogFile(config.local_file.log_dir))
"""

lazy from ezhpcy.constants import KEYRING_SERVICE_NAME
lazy from ezhpcy.logging import LogFile, get_logger
lazy from ezhpcy.login import PASSWORD_ATTEMPTS, read_password
lazy from ezhpcy.messages import message_parts
lazy from ezhpcy.tunnel.ssh_config import AliasProblem, check_alias, probe_alias
lazy from ezhpcy.types import alias_digest
lazy from ezhpcy.utils import local_machine_id

__all__ = [
    "KEYRING_SERVICE_NAME",
    "PASSWORD_ATTEMPTS",
    "AliasProblem",
    "LogFile",
    "alias_digest",
    "check_alias",
    "get_logger",
    "local_machine_id",
    "message_parts",
    "probe_alias",
    "read_password",
]
