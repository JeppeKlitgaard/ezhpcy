"""
Interprocess communication (IPC) between the EzHPCy tunnel and its proxies.

A long-lived tunnel listens on a loopback TCP port. It publishes the port and a
per-run key in a descriptor file named after its SSH host alias, which only the
current user can read (`descriptor`). OpenSSH starts a short-lived `ezhpcy proxy`
for each connection; the proxy reads the descriptor, connects, and both sides
prove that they hold the key (`protocol`), so other local users can't use the
tunnel. The tunnel then relays the proxy's bytes to the worker.
"""

LOOPBACK_HOST = "127.0.0.1"


class IPCError(Exception):
    """An actionable tunnel IPC failure."""
