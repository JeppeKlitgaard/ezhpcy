from ezhpcy.cli.utils.bad_parameter import RichBadParameter


def resolve_forbidden_none[T](
    *,
    cli_value: T | None,
    config_value: T | None,
    name: str,
    cli_param: str,
    config_param: str,
) -> T:
    value = cli_value if cli_value is not None else config_value
    if value is None:
        raise RichBadParameter(
            f"[bold purple]{name}[/bold purple] must be set via CLI ([bold green]{cli_param}[/bold green]) or config ([bold green]{config_param}[/bold green])"
        )
    return value
