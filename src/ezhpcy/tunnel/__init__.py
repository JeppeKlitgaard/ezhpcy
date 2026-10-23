"""
The tunnel: a worker job running sshd on a compute node (see `ezhpcy.worker`),
reached through the login connection.

- `ssh_config` writes the Host blocks that point OpenSSH at a tunnel's
  forwarded port; `ezhpcy.job.publish` publishes a job under one.
- `lock` keeps one running tunnel per alias, and reserves an alias for one.
"""

from ezhpcy.messages import MessageError


class TunnelError(MessageError, RuntimeError):
    """The tunnel couldn't start, or stopped because something failed."""
