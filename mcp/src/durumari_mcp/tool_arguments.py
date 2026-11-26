"""Argument errors a caller can correct.

MCP SDK 2.2 passes the client a pydantic ValidationError's text; any other exception
from argument validation reaches it only as "Error executing tool <name>", which says
neither what was wrong nor that the arguments were the problem. The error built here
names the declared argument that failed and why, never the caller's values or an
undeclared argument's name. Pydantic's own wording is kept only for error types whose
message describes the expected shape; others (custom validators, union tags) may quote
the input and get a fixed reason.
"""

from collections.abc import Collection

from pydantic import ValidationError
from pydantic_core import PydanticCustomError

LIMIT = 5
REASON_LIMIT = 160
# Error types whose message states only the expected shape, never the input.
SHAPE_MESSAGES = frozenset(
    {
        "missing",
        "extra_forbidden",
        "string_type",
        "string_too_short",
        "string_too_long",
        "string_pattern_mismatch",
        "literal_error",
        "enum",
        "int_type",
        "int_parsing",
        "int_from_float",
        "float_type",
        "float_parsing",
        "bool_type",
        "bool_parsing",
        "greater_than",
        "greater_than_equal",
        "less_than",
        "less_than_equal",
        "list_type",
        "dict_type",
        "model_type",
        "too_short",
        "too_long",
        "uuid_type",
    }
)


def argument_error(error: Exception, declared: Collection[str]) -> ValidationError:
    problems: list[tuple[str, str, str]] = []
    if isinstance(error, ValidationError):
        for item in error.errors(include_input=False, include_url=False)[:LIMIT]:
            location = item.get("loc") or ()
            name = location[0] if location and location[0] in declared else "unexpected_argument"
            kind = item.get("type", "invalid")
            reason = item["msg"][:REASON_LIMIT] if kind in SHAPE_MESSAGES else "Invalid value"
            problems.append((str(name), kind, reason))
    if not problems:
        problems = [("arguments", "invalid", "Arguments do not match the tool's input schema")]
    return ValidationError.from_exception_data(
        "invalid_tool_arguments",
        [
            {
                "type": PydanticCustomError(
                    "invalid_tool_arguments", "{reason} ({kind})", {"reason": reason, "kind": kind}
                ),
                "loc": (name,),
                "input": None,
            }
            for name, kind, reason in problems
        ],
        hide_input=True,
    )
