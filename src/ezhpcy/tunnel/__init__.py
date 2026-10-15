"""
The tunnel: a worker job running sshd on a compute node, reached through the
login connection.

- `session` runs one tunnel from connecting to cleaning up.
- `worker` builds the worker's command and talks to it through its control directory
    on the shared filesystem.
- `server` relays each `ezhpcy proxy` to the worker
- `ssh_config` writes the Host blocks that point OpenSSH at the proxy.
"""


class TunnelError(RuntimeError):
    """The tunnel couldn't start, or stopped because something failed."""
