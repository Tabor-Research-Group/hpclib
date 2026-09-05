"""
auto_argparser.py

Automatically build an argparse.ArgumentParser from a function's signature,
so you can turn any `main(...)`-style entry point into a CLI without writing
argparse boilerplate by hand.

How it works
------------
- Parameters WITHOUT a default become required positional arguments.
- Parameters WITH a default become optional `--flag` arguments.
- Type hints (int, float, str, Path, etc.) are used to cast CLI strings.
- `bool` parameters become `--flag` / `--no-flag` switches (no value needed).
- `Optional[X]` is unwrapped to `X` for typing purposes.
- List[...] / tuple[...] annotations become `nargs='+'` arguments.
- *args / **kwargs are skipped (argparse can't represent them generically).
- The function's docstring becomes the parser description.
"""

import argparse
import inspect
import sys
from typing import get_type_hints, get_origin, get_args, Union


def _unwrap_optional(annotation):
    """Turn Optional[X] (i.e. Union[X, None]) into X. Return annotation unchanged otherwise."""
    if get_origin(annotation) is Union:
        non_none = [a for a in get_args(annotation) if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return annotation


def _is_sequence_type(annotation):
    """Detect List[X], list[X], Tuple[X, ...], tuple[X, ...], and bare list/tuple."""
    origin = get_origin(annotation)
    if origin in (list, tuple):
        return True
    return annotation in (list, tuple)


def _element_type(annotation):
    """Get the element type of a sequence annotation, defaulting to str
    for bare `list`/`tuple` with no subscript."""
    args = get_args(annotation)
    return args[0] if args else str


def build_parser(func, parser=None):
    """
    Inspect `func`'s signature and build (or extend) an argparse.ArgumentParser
    that mirrors its parameters.
    """
    if parser is None:
        parser = argparse.ArgumentParser(
            prog=func.__name__,
            description=inspect.getdoc(func),
        )

    sig = inspect.signature(func)

    try:
        hints = get_type_hints(func)
    except Exception:
        hints = {}

    for name, param in sig.parameters.items():
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue  # can't represent *args/**kwargs generically

        annotation = hints.get(name, param.annotation)
        annotation = _unwrap_optional(annotation)
        has_default = param.default is not inspect.Parameter.empty
        default = param.default if has_default else None
        flag = name.replace("_", "-")

        # --- boolean flags: --flag / --no-flag ---
        if annotation is bool:
            if has_default and default:
                parser.add_argument(
                    f"--no-{flag}", dest=name, action="store_false",
                    default=default,
                    help=f"disable {name} (default: {default})",
                )
            else:
                parser.add_argument(
                    f"--{flag}", dest=name, action="store_true",
                    default=bool(default),
                    help=f"enable {name}" + (f" (default: {default})" if has_default else ""),
                )
            continue

        # --- sequence types: nargs='+' ---
        if _is_sequence_type(annotation):
            elem_type = _element_type(annotation)
            elem_type = elem_type if callable(elem_type) else str
            kwargs = {"nargs": "+", "type": elem_type}
            if has_default:
                kwargs["default"] = default
                kwargs["help"] = f"(default: {default})"
                parser.add_argument(f"--{flag}", **kwargs)
            else:
                parser.add_argument(name, **kwargs)
            continue

        # --- plain scalar types ---
        if annotation is inspect.Parameter.empty or annotation is None:
            arg_type = str
        else:
            arg_type = annotation if callable(annotation) else str

        kwargs = {"type": arg_type}
        if has_default:
            kwargs["default"] = default
            kwargs["help"] = f"(default: {default!r})"
            parser.add_argument(f"--{flag}", **kwargs)
        else:
            parser.add_argument(name, **kwargs)

    return parser


def run(func, argv=None):
    """
    Build a parser from `func`, parse argv (defaults to sys.argv[1:]),
    and call `func(**parsed_args)`.
    """
    parser = build_parser(func)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    return func(**vars(args))


def cli(func):
    """
    Decorator version: `@cli` on your entry point makes it runnable directly.

        @cli
        def main(name: str, count: int = 1, verbose: bool = False):
            ...

        if __name__ == "__main__":
            main()   # parses sys.argv automatically
    """
    def wrapper(*args, **kwargs):
        if args or kwargs:  # called normally, e.g. in tests
            return func(*args, **kwargs)
        return run(func)
    wrapper.__wrapped__ = func
    return wrapper


# # --------------------------------------------------------------------------
# # Example usage
# # --------------------------------------------------------------------------
# if __name__ == "__main__":
#
#     @cli
#     def main(name: str, count: int = 1, shout: bool = False, tags: list = None):
#         """Greet someone, optionally more than once."""
#         greeting = f"Hello, {name}!"
#         if shout:
#             greeting = greeting.upper()
#         for _ in range(count):
#             print(greeting)
#         if tags:
#             print("tags:", tags)
#
#     main()
