"""
The worker: a scheduler job running sshd on a compute node, which the tunnel
reaches through the login connection.

- `sshd` configures sshd, and runs it by its absolute path from the Pixi
    environment.
- `job` builds the worker job's command: a shell program that starts sshd on the
    first free candidate port, and a watchdog that cancels the job once sshd
    has exited or the tunnel's leases stop.
- `control` is the tunnel's side of the control directory the two share.

The tunnel and the worker talk through files in a directory on the cluster's
shared filesystem, which the tunnel reaches over SFTP:

- The tunnel renews a lease (`lease.<sequence>`) every heartbeat interval. The
  worker's watchdog cancels the job through the scheduler once no newer lease
  has appeared within the heartbeat timeout, so a worker never outlives its
  tunnel by long, even when the tunnel can no longer cancel it.
- The worker writes its status to `status`, replacing it whole each time:
  `ready` with the port sshd listens on, or `failed` with the reason, and
  `stopped` with its exit status when it ends.

Every file names the run's token, so one from another run is never taken for
this run's.
"""

from ezhpcy.messages import MessageError


class WorkerError(MessageError, RuntimeError):
    """The worker wrote something the tunnel can't use."""
