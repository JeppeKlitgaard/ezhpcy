"""
The actions of a tunnel's apps, as keys in its status panel. A key runs its
action, once it has asked for the action's inputs, one at a time, in the panel
itself, so that nothing blocks the event loop and the log keeps scrolling above.
Running them, and checking what they are given, is `ezhpcy.api`'s.

An action failing is a warning, not an error, in the log: the tunnel serves on,
and an error would bring out everything kept to show with the next one, such as
the whole of the job's output.
"""

from dataclasses import dataclass, field
lazy import asyncio
lazy import time
lazy from collections.abc import Callable
lazy from string.templatelib import Template

from ezhpcy.api import get_logger
lazy from ezhpcy import api
lazy from ezhpcy.cli._errors import sentence
lazy from ezhpcy.cli._messages import duration
lazy from ezhpcy.cli.utils.keys import BACKSPACE, ENTER, ESCAPE

logger = get_logger(__name__)

# In the order they are given to actions, across the apps.
_KEYS = "123456789abcdefghijklmnopqrstuvwxyz"


@dataclass(frozen=True, slots=True)
class MenuEntry:
    """An action of a running app, and the key that runs it, if a key may."""

    running: api.RunningApp
    name: str
    action: api.Action
    key: str | None


@dataclass(slots=True)
class _Asking:
    """An action being asked for its inputs: those left, and the values so far."""

    entry: MenuEntry
    left: list[tuple[str, api.Parameter]]
    values: dict[str, object] = field(default_factory=dict)
    text: str = ""
    problem: str | None = None


class ActionMenu:
    """
    What the status panel offers. `on_change` is called when what it shows has
    changed because of a key.
    """

    def __init__(self, on_change: Callable[[], object] = lambda: None) -> None:
        self.entries: list[MenuEntry] = []
        self.on_change = on_change
        self._asking: _Asking | None = None
        self._closed = False
        self._runs: set[asyncio.Task[None]] = set()

    def add(self, running: api.RunningApp) -> None:
        """Offer the actions of `running`."""
        for name, action in running.actions.items():
            keys_used = sum(entry.key is not None for entry in self.entries)
            key = None
            if action.trigger.manual and keys_used < len(_KEYS):
                key = _KEYS[keys_used]
            self.entries.append(MenuEntry(running, name, action, key))

    @property
    def has_keys(self) -> bool:
        """Whether any key runs anything."""
        return any(entry.key is not None for entry in self.entries)

    def of(self, run_id: str | None) -> list[MenuEntry]:
        """The actions of the app whose status has `run_id`."""
        return [
            entry for entry in self.entries if entry.running.status.run_id == run_id
        ]

    @property
    def prompt(self) -> tuple[Template, ...]:
        """The lines that ask for an input, if one is being asked for."""
        if (asking := self._asking) is None:
            return ()
        input_name, parameter = asking.left[0]
        label = parameter.label or input_name
        about = describe(parameter)
        lines = [
            t"{asking.entry.name:name}: {label} {f'({about})':aside}: {asking.text}█"
        ]
        if asking.problem is not None:
            lines.append(t"{asking.problem:emphasis}")
        lines.append(t"{'Enter accepts, Esc cancels.':aside}")
        return tuple(lines)

    def on_key(self, key: str) -> None:
        """Take a key, as `ezhpcy.cli.utils.keys.read_keys` gives it."""
        if self._closed:
            return
        if self._asking is not None:
            self._edit(self._asking, key)
        else:
            entry = next((e for e in self.entries if e.key == key), None)
            if entry is not None:
                self._begin(entry)
        self.on_change()

    def _begin(self, entry: MenuEntry) -> None:
        latest = {run.action: run for run in entry.running.status.actions}
        run = latest.get(entry.name)
        if run is not None and run.state is api.ActionState.RUNNING:
            logger.warning(t"Action {entry.name:name} is running already.")
            return
        if entry.running.status.state is not api.AppState.READY:
            logger.warning(
                t"Action {entry.name:name} needs app "
                t"{entry.running.app!s:name} running, and it isn't."
            )
            return
        asking = _Asking(entry, list(entry.action.input.items()))
        if asking.left:
            self._asking = asking
        else:
            self._launch(asking)

    def _edit(self, asking: _Asking, key: str) -> None:
        if key == ESCAPE:
            self._asking = None
        elif key == ENTER:
            self._accept(asking)
        else:
            asking.text = asking.text[:-1] if key == BACKSPACE else asking.text + key
            asking.problem = None

    def _accept(self, asking: _Asking) -> None:
        input_name, parameter = asking.left[0]
        try:
            if asking.text:
                value = parameter.parse(asking.text)
            elif parameter.default is not None:
                value = parameter.default
            elif parameter.required:
                raise ValueError("it needs a value")
            else:
                value = None
            if value is not None:
                parameter.check(value)
        except ValueError as error:
            asking.problem = sentence(str(error))
            return
        if value is not None:
            asking.values[input_name] = value
        asking.left.pop(0)
        asking.text, asking.problem = "", None
        if not asking.left:
            self._asking = None
            self._launch(asking)

    def _launch(self, asking: _Asking) -> None:
        task = asyncio.create_task(_run(asking.entry, asking.values))
        self._runs.add(task)
        task.add_done_callback(self._runs.discard)

    def close(self) -> None:
        """Take no more keys, and stop asking, as the tunnel stops."""
        self._closed = True
        self._asking = None

    async def stop(self) -> None:
        """Close it, and stop the runs its keys began."""
        self.close()
        runs = list(self._runs)
        for run in runs:
            run.cancel()
        if runs:
            await asyncio.wait(runs)


def describe(parameter: api.Parameter) -> str:
    """What an input takes, in a few words, and its default."""
    match parameter:
        case api.IntegerParameter() | api.NumberParameter():
            words = parameter.type
            if parameter.min is not None and parameter.max is not None:
                words += f", {parameter.min} to {parameter.max}"
            elif parameter.min is not None:
                words += f", at least {parameter.min}"
            elif parameter.max is not None:
                words += f", at most {parameter.max}"
        case api.BooleanParameter():
            words = "yes or no"
        case api.ChoiceParameter():
            words = " or ".join(parameter.choices)
        case api.PathParameter():
            what = parameter.kind or "path"
            words = f"{what} on the host"
            if parameter.must_exist:
                words += ", which must exist"
        case api.StringParameter():
            words = "text"
    if parameter.default is not None:
        words += f", default {parameter.default}"
    if not parameter.required:
        words += ", optional"
    return words


async def _run(entry: MenuEntry, values: dict[str, object]) -> None:
    """Run an action, and say how it ended, with its output, in the log."""
    what = t"Action {entry.name:name} of app {entry.running.app!s:name}"
    logger.info(what + t" is running.")
    try:
        result = await entry.running.run_action(entry.name, **values)
    except (api.MessageError, RuntimeError, OSError) as error:
        logger.warning(what + t" could not run: {error}")
        return
    seconds = (result.finished or time.monotonic()) - result.started
    if result.state is api.ActionState.SUCCEEDED:
        logger.info(what + t" succeeded in {duration(seconds):detail}.")
    else:
        logger.warning(
            what + t" exited with status {result.exit_status!s:detail} "
            t"after {duration(seconds):detail}."
        )
    for line in result.stdout.splitlines():
        logger.info(t"  {line}")
    for line in result.stderr.splitlines():
        logger.warning(t"  {line}")
