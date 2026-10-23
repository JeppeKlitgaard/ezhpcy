"""
What is attached to a host's connection (a process, a forwarded port) or to a
job (a Host block), and closed with it.

Each is an `Attachment`, made by its owner's `Attachments.attach`. As it
acquires things, it pushes the cleanup step for each, so that its cleanup covers
exactly what it acquired, in reverse order, each step with a timeout. It is
closed once: early, by `close`, or when its owner closes them all, newest first,
whichever comes first; the other then finds it closed, or waits for it.

Its `status`, if it reports one, is in its owner's `statuses`, until it closes.
"""

lazy import asyncio
lazy from collections.abc import Awaitable, Callable
lazy from contextlib import AsyncExitStack

lazy from ezhpcy.cleanup import cleanup
lazy from ezhpcy.job.status import AttachmentStatus


class Attachment:
    """
    Something attached to a host or a job, with its cleanup steps. `on_change`
    is called when its status changes, and when it closes.
    """

    def __init__(self, on_change: Callable[[], object]) -> None:
        self._stack = AsyncExitStack()
        self._closing: asyncio.Task[None] | None = None
        self._on_change = on_change
        self.status: AttachmentStatus | None = None

    def report(self, status: AttachmentStatus) -> None:
        """Replace the attachment's status."""
        self.status = status
        self._on_change()

    def push(
        self,
        description: str,
        step: Callable[[], Awaitable[object]],
        *,
        seconds: float | None = None,
    ) -> None:
        """
        Run `step` when the attachment is closed, before the steps pushed before
        it, as a cleanup step (see `cleanup`).
        """
        if self._closing is not None:
            raise RuntimeError("the attachment is closed")
        self._stack.push_async_callback(cleanup, description, step, seconds=seconds)

    @property
    def closed(self) -> bool:
        """Whether the attachment is closed, or closing."""
        return self._closing is not None

    async def close(self) -> None:
        """
        Run the cleanup steps, the newest first, unless they ran already; then
        return once they have. Cancelled, the steps still run to the end.
        """
        if self._closing is None:
            self._closing = asyncio.ensure_future(self._stack.aclose())
            self._on_change()
        await asyncio.shield(self._closing)


class Attachments:
    """
    What is attached to one owner, oldest first. `close` closes them all, newest
    first, and waits for those closing already, and no more can be attached
    after. Listeners added with `on_change` are called whenever `statuses` may
    have changed.
    """

    def __init__(self) -> None:
        self._attachments: list[Attachment] = []
        self._closing: asyncio.Task[None] | None = None
        self._listeners: list[Callable[[], object]] = []

    def attach(self) -> Attachment:
        """
        Register a new attachment, closed with the others unless it was closed
        before; then push its cleanup steps onto it as it acquires things.
        """
        if self._closing is not None:
            raise RuntimeError("its connection or job is being closed")
        attachment = Attachment(on_change=self._changed)
        self._attachments.append(attachment)
        return attachment

    @property
    def statuses(self) -> tuple[AttachmentStatus, ...]:
        """The open attachments' statuses, oldest first."""
        return tuple(
            attachment.status
            for attachment in self._attachments
            if attachment.status is not None and not attachment.closed
        )

    def on_change(self, listener: Callable[[], object]) -> None:
        """Call `listener` whenever `statuses` may have changed."""
        self._listeners.append(listener)

    async def close(self) -> None:
        """
        Close every attachment, the newest first, once, waiting for those that
        are closing already; then return once they are. Cancelled, they are
        still closed to the end.
        """
        if self._closing is None:
            self._closing = asyncio.ensure_future(self._close_all())
        await asyncio.shield(self._closing)

    async def _close_all(self) -> None:
        for attachment in reversed(self._attachments):
            await attachment.close()

    def _changed(self) -> None:
        for listener in self._listeners:
            listener()
