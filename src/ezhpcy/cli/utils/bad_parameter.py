from rich.text import Text
from typer._click.exceptions import BadParameter


class _ManualMarkupText(Text):
    def copy(self) -> _ManualMarkupText:
        text = type(self)(
            self.plain,
            style=self.style,
            justify=self.justify,
            overflow=self.overflow,
            no_wrap=self.no_wrap,
            end=self.end,
            tab_size=self.tab_size,
        )
        text._spans[:] = self._spans
        return text

    def highlight_regex(self, *args, **kwargs) -> int:
        return 0


def _render_markup(text: str) -> Text:
    rich_text = Text.from_markup(text, end="")
    manual_text = _ManualMarkupText(
        rich_text.plain,
        style=rich_text.style,
        justify=rich_text.justify,
        overflow=rich_text.overflow,
        no_wrap=rich_text.no_wrap,
        end=rich_text.end,
        tab_size=rich_text.tab_size,
    )
    manual_text._spans[:] = rich_text.spans
    return manual_text


class RichBadParameter(BadParameter):
    def format_message(self) -> Text:
        return _render_markup(super().format_message())
