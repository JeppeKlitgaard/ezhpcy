"""
The CLI's consoles: `console` for stdout, and `error_console` for stderr, which
the log and Cyclopts write through too. Each writes the records logged so far
first (see `ezhpcy.logging`), so that its output comes after them. That covers
Rich's prompts too, which print through `Console.print`.

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


def make_console(**options: Any) -> Console:
    """
    A console like the CLI's, with Rich's `Console` options. Only the parts that
    a message marks are styled (see `ezhpcy.messages`).
    """
    return _Console(highlight=False, **options)


console = make_console()
error_console = make_console(stderr=True)
