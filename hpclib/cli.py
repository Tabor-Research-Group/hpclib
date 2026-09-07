"""
auto_argparser.py

Automatically build an argparse.ArgumentParser from a function's signature,
so you can turn any `main(...)`-style entry point into a CLI without writing
argparse boilerplate by hand.

How it works
------------
- Parameters WITHOUT a default, of ordinary kind (POSITIONAL_OR_KEYWORD),
  can be supplied EITHER positionally OR as `--flag value` - whichever
  reads better at the call site. Supplying neither is an error.
- Parameters explicitly marked POSITIONAL_ONLY in the signature (i.e.
  everything before a bare `/`) are ALWAYS plain positional CLI args,
  never a `--flag` - since that's what Python itself says about how
  they may be passed to the function. This holds whether or not they
  have a default (a defaulted positional-only param becomes an
  *optional* positional, via `nargs='?'`, rather than switching to a
  flag).
- Parameters marked KEYWORD_ONLY (i.e. everything after a bare `*`)
  are ALWAYS `--flag` args, never positional - the mirror image of the
  rule above, for the same reason.
- Parameters WITH a default (and not positional-only) are optional
  `--flag` arguments, as before.
- Type hints (int, float, str, Path, etc.) are used to cast CLI strings.
- `bool` parameters always become `--flag` / `--no-flag` switches (no
  value needed) regardless of kind - there's no sensible positional
  encoding of a boolean, so this is the one place kind is ignored.
- `Optional[X]` is unwrapped to `X` for typing purposes.
- List[...] / tuple[...] annotations become multi-value arguments,
  following the same positional/flag/dual rules as scalars.
- *args / **kwargs are skipped (argparse can't represent them generically).
- The function's docstring becomes the parser description.

Note on --help output: a dual-mode parameter shows up as both a
bracketed positional and a `--flag` in the usage line (argparse has no
concept of "one arg, two spellings"), which is slightly redundant but
unambiguous - only one of the two actually needs to be used.
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


def _add_dual(parser, name, flag, arg_type, *, sequence, required_names):
    """Registers `name` so it can be supplied EITHER positionally OR as
    `--flag value`, sharing one `dest`. Used only for ordinary
    (POSITIONAL_OR_KEYWORD) parameters with no default - i.e. ones
    Python itself doesn't force into a single calling convention.

    Neither individual action is marked `required=True` (argparse
    can't express "either of these two is required" at the
    per-action level) - `name` is appended to `required_names`
    instead, and `run()` checks post-parse that it actually got a
    value from one form or the other.
    """
    if sequence:
        # nargs='*' (not '+') so the positional form doesn't force
        # argparse to demand a value here specifically - the flag form
        # may supply it instead. Emptiness is caught by the
        # required-name check in run(), same as the scalar case.
        parser.add_argument(name, nargs="*", type=arg_type, default=None,
                             help=argparse.SUPPRESS)
        parser.add_argument(f"--{flag}", dest=name, nargs="+", type=arg_type,
                             default=None, help="(positional or flag; required)")
    else:
        parser.add_argument(name, nargs="?", type=arg_type, default=None,
                             help=argparse.SUPPRESS)
        parser.add_argument(f"--{flag}", dest=name, type=arg_type, default=None,
                             help="(positional or flag; required)")
    required_names.append(name)


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

    # Accumulate across possibly-multiple build_parser() calls against
    # the same parser (it supports being handed an existing one to extend).
    required_names = getattr(parser, "_cli_required_names", [])
    parser._cli_required_names = required_names

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

        positional_only = param.kind is inspect.Parameter.POSITIONAL_ONLY
        keyword_only = param.kind is inspect.Parameter.KEYWORD_ONLY

        # --- boolean flags: --flag / --no-flag, regardless of kind ---
        # (no sensible positional encoding of a boolean, so kind is
        # deliberately ignored here even for positional-only params)
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

        # --- sequence types ---
        if _is_sequence_type(annotation):
            elem_type = _element_type(annotation)
            elem_type = elem_type if callable(elem_type) else str

            if has_default:
                if positional_only:
                    parser.add_argument(name, nargs="*", type=elem_type, default=default,
                                         help=f"(default: {default})")
                else:
                    parser.add_argument(f"--{flag}", nargs="+", type=elem_type, default=default,
                                         help=f"(default: {default})")
            elif positional_only:
                parser.add_argument(name, nargs="+", type=elem_type, help="(positional-only, required)")
            elif keyword_only:
                parser.add_argument(f"--{flag}", nargs="+", type=elem_type, required=True,
                                     help="(flag-only, required)")
            else:
                _add_dual(parser, name, flag, elem_type, sequence=True, required_names=required_names)
            continue

        # --- plain scalar types ---
        if annotation is inspect.Parameter.empty or annotation is None:
            arg_type = str
        else:
            arg_type = annotation if callable(annotation) else str

        if has_default:
            if positional_only:
                # Still positional (that's what Python says about it),
                # but optional: nargs='?' with the function's own default.
                parser.add_argument(name, nargs="?", type=arg_type, default=default,
                                     help=f"(default: {default!r})")
            else:
                parser.add_argument(f"--{flag}", type=arg_type, default=default,
                                     help=f"(default: {default!r})")
        elif positional_only:
            parser.add_argument(name, type=arg_type, help="(positional-only, required)")
        elif keyword_only:
            parser.add_argument(f"--{flag}", type=arg_type, required=True, help="(flag-only, required)")
        else:
            _add_dual(parser, name, flag, arg_type, sequence=False, required_names=required_names)

    return parser


def run(func, argv=None):
    """
    Build a parser from `func`, parse argv (defaults to sys.argv[1:]),
    and call `func(**parsed_args)`.
    """
    parser = build_parser(func)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    # Dual-mode (positional-or-flag) params can't be marked
    # required=True on either individual argparse action, so check
    # here that each one actually got a value from *some* form.
    missing = [n for n in getattr(parser, "_cli_required_names", []) if getattr(args, n) in (None, [])]
    if missing:
        readable = ", ".join(f"{n} (positional or --{n.replace('_', '-')})" for n in missing)
        parser.error(f"the following arguments are required: {readable}")

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