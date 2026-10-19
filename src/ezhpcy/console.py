"""
The CLI's consoles. Each writes the records logged so far first (see
`ezhpcy.logging`), so that its output comes after them. That covers Rich's
prompts too, which print through `Console.print`.

They write to the terminal directly, so a terminal that doesn't take output
blocks them. Don't print from the event loop while the tunnel serves: log
instead, or print from a thread (`asyncio.to_thread`).
"""

from typing import Any

from rich.console import Console

lazy from ezhpcy.logging import flush_logs


class _Console(Console):
    def print(self, *objects: Any, **kwargs: Any) -> None:
        flush_logs()
        super().print(*objects, **kwargs)


console = _Console(highlight=False)
error_console = _Console(stderr=True, highlight=False)
