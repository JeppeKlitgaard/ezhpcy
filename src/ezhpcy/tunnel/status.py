"""
What a tunnel is doing, for a display to show while it runs.

`TunnelSession` replaces its `status` with a new `TunnelStatus` at each step, from
the event loop, and a display reads it from a thread of its own (see
`ezhpcy.console.live_display`). Replaced whole, a status is never seen half
updated, and the event loop never waits for the display, which waits for the
terminal.

Times are `time.monotonic()` readings, so that a display can say how long ago.
"""

import time
from dataclasses import dataclass, field
from enum import Enum, auto

lazy from ezhpcy.scheduler.base import JobInfo
lazy from ezhpcy.types import ResourcesConfig


class Phase(Enum):
    """Where the tunnel is, from connecting until it has cleaned up."""

    CONNECTING = auto()
    # Provisioning or checking the worker infrastructure, and the control directory.
    PREPARING = auto()
    SUBMITTING = auto()
    # Until the scheduler says the job runs.
    QUEUED = auto()
    # Until the worker's sshd answers.
    STARTING = auto()
    SERVING = auto()
    STOPPING = auto()


@dataclass(frozen=True, slots=True)
class TunnelStatus:
    alias: str
    # `user@host` of the login node.
    login: str
    phase: Phase = Phase.CONNECTING
    # When the phase began.
    since: float = field(default_factory=time.monotonic)
    job_id: str | None = None
    # The scheduler's latest word on the job.
    job: JobInfo | None = None
    # When the job was first seen running.
    running_since: float | None = None
    # What the job requests, or the interactive submission command that decides
    # instead (see `SchedulerConfig.interactive_submission_command`).
    resources: ResourcesConfig | None = None
    submission_command: tuple[str, ...] | None = None
    # The worker's sshd, as `host:port`, once it has said which port.
    worker: str | None = None
    last_heartbeat: float | None = None
    # Connections open through the tunnel.
    connections: int = 0
