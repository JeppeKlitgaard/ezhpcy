"""
The tunnel: a worker job running sshd on a compute node (see `ezhpcy.worker`),
reached through the login connection.

- `session` runs one tunnel from connecting to cleaning up, forwarding a
  loopback port to the worker's sshd.
- `ssh_config` writes the Host blocks that point OpenSSH at a tunnel's
  forwarded port.
- `lock` keeps one running tunnel per alias.
"""

from ezhpcy.messages import MessageError


class TunnelError(MessageError, RuntimeError):
    """The tunnel couldn't start, or stopped because something failed."""
