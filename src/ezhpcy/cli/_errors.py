lazy from string.templatelib import Interpolation, Template

lazy from rich.text import Text

lazy from ezhpcy.cli._output import rich_text
lazy from ezhpcy.messages import plain

_SENTENCE_ENDINGS = (".", "?", "!")


def _capitalised(text: str) -> str:
    first_word = text.partition(" ")[0]
    if first_word.isalpha() and first_word.islower():
        return text[0].upper() + text[1:]
    return text


def sentence[MessageT: (str, Template)](message: MessageT) -> MessageT:
    """Make a lower-case message, e.g. from an exception, a sentence.

    A first word that is an identifier, such as `time_limit`, keeps its case.
    """
    if isinstance(message, str):
        message = _capitalised(message)
        return message if message.endswith(_SENTENCE_ENDINGS) else message + "."
    pieces: list[str | Interpolation] = list(message)
    if pieces and isinstance(pieces[0], str):
        pieces[0] = _capitalised(pieces[0])
    if not (pieces and isinstance(pieces[-1], str)) or not pieces[-1].endswith(
        _SENTENCE_ENDINGS
    ):
        pieces.append(".")
    return Template(*pieces)


class CliUsageError(Exception):
    """A usage error found in a command body.

    `main()` renders it like Cyclopts' own parse errors and exits with code 2.
    With `param_hint`, the option, argument or setting at fault, the message
    starts like Cyclopts' validation errors: `Invalid value "0" for --cores.`
    """

    def __init__(
        self,
        message: Template,
        *,
        param_hint: str | None = None,
        value: object = None,
    ) -> None:
        if param_hint is not None:
            prefix = (
                t"Invalid value for {param_hint:name}. "
                if value is None
                else t"Invalid value {value:value} for {param_hint:name}. "
            )
            message = prefix + message
        self.message = message
        super().__init__(plain(message))

    def __rich__(self) -> Text:
        text = rich_text(self.message)
        # Like Cyclopts' own errors: the panel's border is red, the message isn't.
        text.style = "default"
        return text
