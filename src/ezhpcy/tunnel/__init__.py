"""
The tunnel: a worker job running sshd on a compute node (see `ezhpcy.worker`),
reached through the login connection.

- `session` runs one tunnel from connecting to cleaning up.
- `server` relays each `ezhpcy proxy` to the worker
- `ssh_config` writes the Host blocks that point OpenSSH at the proxy.
- `lock` keeps one running tunnel per alias.
"""


class TunnelError(RuntimeError):
    """The tunnel couldn't start, or stopped because something failed."""
