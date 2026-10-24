import pytest
from rich.style import Style
from rich.text import Text

from ezhpcy.messages import MessageError, duration, plain, rich_text


def styled_runs(text: Text) -> list[tuple[str, str | Style]]:
    """Each styled run of `text`, with its style."""
    return [(text.plain[span.start : span.end], span.style) for span in text.spans]


def test_a_format_spec_may_follow_the_kind() -> None:
    seconds, elapsed = 30.0, 0.12345

    message = t"Every {seconds:detail:g} seconds, took {elapsed:detail:.3f}."

    assert plain(message) == "Every 30 seconds, took 0.123."


def test_a_nested_template_s_plain_text_takes_the_enclosing_kind() -> None:
    wait = "8 min"

    text = rich_text(t"Waiting {t'(timeout in {wait:detail})':aside}.")

    assert text.plain == "Waiting (timeout in 8 min)."
    assert styled_runs(text) == [
        ("(timeout in ", "dim"),
        ("8 min", "bold cyan"),
        (")", "dim"),
    ]


def test_an_interpolated_message_error_is_rendered_as_its_template() -> None:
    job_id = "42"
    error = MessageError(t"job {job_id:detail} failed")

    text = rich_text(t"Stopping: {error}")

    assert str(error) == "job 42 failed"
    assert text.plain == "Stopping: job 42 failed"
    assert styled_runs(text) == [("42", "bold cyan")]


def test_a_converted_message_error_is_plain_text() -> None:
    error = MessageError(t"{'gone':detail}")

    assert styled_runs(rich_text(t"{error!r}")) == []


@pytest.mark.parametrize(
    ("path", "runs"),
    [
        (
            "/home/alice/.cache/ezhpcy/logs",
            [("/home/alice/.cache/ezhpcy/", "repr.path"), ("logs", "repr.filename")],
        ),
        (
            r"C:\Users\alice\known_hosts",
            [("C:\\Users\\alice\\", "repr.path"), ("known_hosts", "repr.filename")],
        ),
        ("known_hosts", [("known_hosts", "repr.filename")]),
    ],
)
def test_a_path_s_file_name_is_styled_apart(
    path: str, runs: list[tuple[str, str]]
) -> None:
    assert styled_runs(rich_text(t"{path:path}")) == runs


def test_an_endpoint_s_host_separator_and_port_are_styled_apart() -> None:
    endpoint = "n-62-12-19:58395"

    assert styled_runs(rich_text(t"{endpoint:endpoint}")) == [
        ("n-62-12-19", "bold cyan"),
        (":", "bold white"),
        ("58395", "cyan"),
    ]


def test_a_target_is_styled_as_a_suggestion_without_quotes() -> None:
    alias = "dtu-a100sh"

    text = rich_text(t"Connect with {'ssh':detail} {alias:target}")

    assert text.plain == "Connect with ssh dtu-a100sh"
    assert styled_runs(text) == [("ssh", "bold cyan"), ("dtu-a100sh", "bold green")]


def test_a_hint_is_green_but_not_bold() -> None:
    hint = "Remove-SmbMapping Z:"

    text = rich_text(t"{hint:hint}")

    assert styled_runs(text) == [("Remove-SmbMapping Z:", "green")]


def test_an_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown message part kind: 'g'"):
        plain(t"{30.0:g}")


@pytest.mark.parametrize(
    ("seconds", "shown"),
    [
        (0, "0 s"),
        (45.4, "45 s"),
        (59.6, "1 min 0 s"),
        (150, "2 min 30 s"),
        (3599.6, "1 h 0 min"),
        (3600 + 5 * 60 + 29, "1 h 5 min"),
    ],
)
def test_durations_are_shown_in_hours_minutes_and_seconds(
    seconds: float, shown: str
) -> None:
    assert duration(seconds) == shown
