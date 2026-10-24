"""An SMB server, run by EzHPCy as an app on a host, serving the node's files.

impacket's `SimpleSMBServer` listens on TCP. Here it listens on the Unix socket
EzHPCy chose instead, by way of `UnixSMBServer`, so the server is reachable only
through that socket's owner and the connection EzHPCy forwards to this machine --
never on a port other users on the node could reach.

A credential is registered so clients authenticate rather than log in as guests.
That is not the security boundary (the socket and the forward are); it is what
lets impacket derive a session key and sign its SMB2/3 responses. Windows 11
24H2 and later require SMB signing and reject unsigned guest shares by default.
"""

import contextlib
import os
import pathlib
import socket

from impacket.ntlm import compute_lmhash, compute_nthash
from impacket.smbserver import SMBSERVER, SimpleSMBServer, SMBSERVERHandler

# EzHPCy's Unix socket, and the tree to share: the node's filesystem root, seen
# with the permissions of the account the server runs as (no root access).
SOCKET_PATH = os.environ["EZHPCY_SOCKET"]
SHARE_PATH = os.environ.get("SMB_SHARE", "/")
SHARE_NAME = "files"
# The login clients use; its password is EzHPCy's per-run token.
USERNAME = os.environ.get("SMB_USER", "ezhpcy")
PASSWORD = os.environ["SMB_PASSWORD"]


class UnixSMBServer(SMBSERVER):
    """An `SMBSERVER` that listens on a Unix socket rather than a TCP port."""

    address_family = socket.AF_UNIX

    def __init__(self, _server_address, handler_class=SMBSERVERHandler, **kwargs):
        # `SimpleSMBServer` passes the (host, port) it was told to listen on;
        # ignore it and listen on the socket instead.
        super().__init__(SOCKET_PATH, handler_class, **kwargs)

    def server_bind(self):
        with contextlib.suppress(FileNotFoundError):
            pathlib.Path(self.server_address).unlink()
        self.socket.bind(self.server_address)
        pathlib.Path(self.server_address).chmod(0o600)  # the owner's alone
        self.server_address = self.socket.getsockname()

    def get_request(self):
        connection, _ = self.socket.accept()
        # A Unix socket has no peer address; impacket logs and keys connections
        # by one, so give each a distinct synthetic address.
        return connection, ("127.0.0.1", connection.fileno())


def main() -> None:
    server = SimpleSMBServer(smbserverclass=UnixSMBServer)
    server.setSMB2Support(True)
    server.addCredential(
        USERNAME, 0, compute_lmhash(PASSWORD), compute_nthash(PASSWORD)
    )
    server.addShare(SHARE_NAME, SHARE_PATH, "Files on this node", readOnly="no")
    server.start()


if __name__ == "__main__":
    main()
