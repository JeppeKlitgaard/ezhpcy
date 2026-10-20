"""
The tunnel's status as a panel, for `ezhpcy.console.live_display`: what the
tunnel is doing, and once it serves, how to connect to it.
"""

lazy import shlex
lazy import time
lazy from string.templatelib import Template

lazy from rich.console import (
    Console,
    ConsoleOptions,
    Group,
    RenderableType,
    RenderResult,
)
lazy from rich.measure import Measurement
lazy from rich.panel import Panel
lazy from rich.spinner import Spinner
lazy from rich.table import Table
lazy from rich.text import Text

lazy from ezhpcy.messages import duration, rich_text
lazy from ezhpcy.tunnel.session import TunnelSession
lazy from ezhpcy.tunnel.status import Phase, TunnelStatus


class StatusPanel:
    """
    Draws the session's status, afresh on each call. A spinner turns while the
    tunnel starts or stops: kept from one call to the next, as it turns by the
    time since it was first drawn.
    """

    def __init__(self, session: TunnelSession) -> None:
        self._session = session
        self._spinner = Spinner("dots", style="cyan")

    def __call__(self) -> RenderableType:
        return status_panel(
            self._session.status, now=time.monotonic(), spinner=self._spinner
        )


def status_panel(
    status: TunnelStatus, *, now: float, spinner: Spinner | None = None
) -> Panel:
    """`status` at `now` (a `time.monotonic()` reading), as a panel."""
    serving = status.phase is Phase.SERVING
    headline = rich_text(_headline(status))
    if not serving:
        headline += rich_text(t" {f'({duration(now - status.since)})':aside}")
    if spinner is not None and not serving:
        spinner.update(text=headline)
        top: RenderableType = spinner
    else:
        top = headline
    parts: list[RenderableType] = [top]
    if details := _details(status, now):
        parts.extend([Text(), details])
    hint = (
        "(press Ctrl+C again to force quit)"
        if status.phase is Phase.STOPPING
        else "Press Ctrl+C to stop."
    )
    parts.extend([Text(), rich_text(t"{hint:aside}")])
    return Panel(
        Group(*parts),
        title=rich_text(t"Tunnel {status.alias:name}"),
        title_align="left",
        border_style=_border_style(status),
        expand=False,
        padding=(0, 1),
    )


def _headline(status: TunnelStatus) -> Template:
    alias = status.alias
    match status.phase:
        case Phase.CONNECTING:
            return t"Connecting to {status.login:detail}"
        case Phase.PREPARING:
            return t"Preparing the worker infrastructure"
        case Phase.SUBMITTING:
            return t"Submitting the worker job"
        case Phase.QUEUED:
            return t"Waiting for the worker job to start"
        case Phase.STARTING:
            return t"Starting the worker's SSH server"
        case Phase.SERVING:
            return t"Connect with {'ssh':detail} {alias:target}"
        case Phase.STOPPING:
            return t"Stopping"


type _Rows = list[tuple[str, Template]]


def _details(status: TunnelStatus, now: float) -> _Details | None:
    columns = [rows for rows in (_progress(status, now), _requested(status)) if rows]
    return _Details([_grid(rows) for rows in columns]) if columns else None


class _Details:
    """
    What is known about the job and the worker, beside what the job requests, or
    above it where the terminal is too narrow for both side by side.
    """

    _GAP = 4

    def __init__(self, grids: list[Table]) -> None:
        self._grids = grids

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        yield self._laid_out(console, options)

    def __rich_measure__(
        self, console: Console, options: ConsoleOptions
    ) -> Measurement:
        return Measurement.get(console, options, self._laid_out(console, options))

    def _laid_out(self, console: Console, options: ConsoleOptions) -> RenderableType:
        widths = [
            Measurement.get(console, options, grid).maximum for grid in self._grids
        ]
        if sum(widths) + self._GAP * (len(widths) - 1) <= options.max_width:
            side_by_side = Table.grid(padding=(0, self._GAP))
            for _ in self._grids:
                side_by_side.add_column(vertical="top")
            side_by_side.add_row(*self._grids)
            return side_by_side
        stacked: list[RenderableType] = []
        for grid in self._grids:
            stacked.extend([Text(), grid] if stacked else [grid])
        return Group(*stacked)


def _progress(status: TunnelStatus, now: float) -> _Rows:
    """The job, and once it runs, the worker, a row each."""
    rows: _Rows = []
    if status.job_id is not None:
        job = t"{status.job_id:detail}"
        if (info := status.job) is not None:
            job += t" {f'({info.raw_state})':aside}"
        rows.append(("Job", job))
        if info is not None and info.pending_reason and status.running_since is None:
            rows.append(("Waiting on", t"{_waiting_on(info.pending_reason)}"))
    if status.worker is not None:
        rows.append(("Worker", t"{status.worker:endpoint}"))
    if status.running_since is not None:
        running = t"{duration(now - status.running_since):detail}"
        if (limit := _time_limit(status)) is not None:
            running += t" of {limit:detail}"
        rows.append(("Running for", running))
    if status.last_heartbeat is not None:
        ago = duration(now - status.last_heartbeat)
        rows.append(("Heartbeat", t"{ago:detail} ago"))
    if status.phase is Phase.SERVING:
        rows.append(("Connections", t"{status.connections:detail}"))
    return rows


def _waiting_on(pending_reason: str) -> str:
    """
    What the scheduler says the job waits on, after "Waiting on": tersely if it's
    a reason we know, else as it says it, without LSF's closing semicolon.
    """
    reason = pending_reason.strip().removesuffix(";")
    return _TERSE_PENDING_REASONS.get(reason, reason)


# Pending reasons, as LSF or PBS give them, that a few words say as well.
_TERSE_PENDING_REASONS = {
    "New job is waiting for scheduling": "job scheduling",
}


def _requested(status: TunnelStatus) -> _Rows:
    """
    What the job requests, a row each, or the interactive submission command,
    which decides instead.
    """
    if status.submission_command is not None:
        return [("Command", t"{shlex.join(status.submission_command):detail}")]
    if (resources := status.resources) is None:
        return []
    rows: _Rows = []
    if resources.queue is not None:
        rows.append(("Queue", t"{resources.queue:detail}"))
    rows.append(("Cores", t"{resources.cores:detail}"))
    if resources.gpus:
        rows.append(("GPUs", t"{resources.gpus:detail}"))
    if resources.memory is not None:
        memory = resources.memory.human_readable(decimal=True, separator=" ")
        rows.append(("Memory", t"{memory:detail}"))
    if resources.exclusive:
        rows.append(("Exclusive", t"{'yes':detail}"))
    if (limit := _time_limit(status)) is not None:
        rows.append(("Time limit", t"{limit:detail}"))
    return rows


def _time_limit(status: TunnelStatus) -> str | None:
    """The job's time limit, if it requests one."""
    if status.submission_command is not None or status.resources is None:
        return None
    limit = status.resources.time_limit
    return duration(limit.total_seconds()) if limit is not None else None


def _grid(rows: _Rows) -> Table:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim")
    grid.add_column()
    for label, value in rows:
        grid.add_row(label, rich_text(value))
    return grid


def _border_style(status: TunnelStatus) -> str:
    match status.phase:
        case Phase.SERVING:
            return "green"
        case Phase.STOPPING:
            return "red"
        case _:
            return "yellow"
