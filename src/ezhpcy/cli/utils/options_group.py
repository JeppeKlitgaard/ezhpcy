import functools
import inspect
import typing
from collections.abc import Callable
from typing import Any, Concatenate


class BadHookError(TypeError): ...


# Adapted from https://github.com/fastapi/typer/discussions/742
def attach_hook[**ParamsHook, RHook](
    hook_func: Callable[ParamsHook, RHook], hook_output_kwarg: str | None = None
) -> Callable[..., Any]:
    """
    Build a decorator that runs `hook_func` before the decorated function.

    Typer derives CLI options from a function's signature, so sharing a group of
    options between commands requires merging parameter lists. The returned
    decorator does this: the wrapper's signature is the decorated function's
    parameters (minus `hook_output_kwarg`) followed by the hook's parameters as
    keyword-only. On call, the hook receives the keyword arguments it declares,
    and its return value is passed to the decorated function as
    `hook_output_kwarg`. Parameters present in both signatures are passed to
    both functions.

    Example:
        ```
        def logging_from_cli(
            *, log_file: Annotated[Path | None, typer.Option()] = None
        ) -> Logger: ...

        with_logging = attach_hook(logging_from_cli, hook_output_kwarg="logger")

        @app.command()
        @with_logging
        def run(size: int, logger: Logger) -> None: ...
        ```

    Here `run` is exposed with the options `--size` and `--log-file`.

    Args:
        hook_func: The function to run first. All of its parameters must be
            passable as keyword arguments.
        hook_output_kwarg: The parameter of the decorated function that receives
            the hook's return value. Defaults to the hook function's name.

    Raises:
        BadHookError: When decorating, if a hook parameter without a default
            has the same name as a parameter of the decorated function.

    Returns:
        A decorator that chains `hook_func` before the decorated function.
    """
    if hook_output_kwarg is None:
        hook_output_kwarg = hook_func.__name__

    def decorator[**ParamsSource, RSource](
        source_func: Callable[Concatenate[RHook, ParamsSource], RSource],
    ) -> Callable[Concatenate[ParamsSource, ParamsHook], RSource]:
        source_params = inspect.signature(source_func).parameters

        # Raise BadHookError if the hook has non-default argument that collides with the `source_func`.
        dup_params = [
            k
            for k, v in inspect.signature(hook_func).parameters.items()
            if k in source_params and v.default == inspect.Parameter.empty
        ]
        if dup_params:
            raise BadHookError(
                f"The following non-default arguments of the hook function (`{hook_func.__name__}`) collide with the source func (`{source_func.__name__}`): {dup_params}"
            )
        hook_params = {
            k: v.replace(kind=inspect.Parameter.KEYWORD_ONLY)
            for k, v in inspect.signature(hook_func).parameters.items()
            if k not in source_params
        }
        hook_param_names = set(inspect.signature(hook_func).parameters)
        shared_params = hook_param_names & set(source_params)

        @functools.wraps(source_func)
        def wrapper(*args, **kwargs):
            # Filter kwargs for those accepted by the hook function
            hook_kwargs = {k: v for k, v in kwargs.items() if k in hook_param_names}

            # Execute hook function with its specific kwargs
            hook_result = hook_func(**hook_kwargs)

            # Filter in the remaining kwargs for the source function.
            source_kwargs = {
                k: v
                for k, v in kwargs.items()
                if k not in hook_param_names or k in shared_params
            }

            # Execute the source function with original args and pass the hook's output to the source function as
            # the specified keyword argument
            return source_func(
                *args, **source_kwargs, **{hook_output_kwarg: hook_result}
            )

        # Combine signatures, but remove the hook_output_kwarg
        combined_params = [
            param for param in source_params.values() if param.name != hook_output_kwarg
        ] + list(hook_params.values())
        wrapper.__signature__ = inspect.signature(source_func).replace(
            parameters=combined_params
        )

        # Combine annotations, but remove the hook_output_kwarg
        wrapper.__annotations__ = {
            **typing.get_type_hints(source_func),
            **typing.get_type_hints(hook_func),
        }
        wrapper.__annotations__.pop(hook_output_kwarg)

        return wrapper

    return decorator
