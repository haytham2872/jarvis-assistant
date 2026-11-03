"""A04: validate tool arguments before anything executes.

Roadmap acceptance: *"malformed arguments never execute"*, and specifically: *"A
malformed tool call must become a structured validation failure, not `{}` followed by
execution."*

Both provider paths did exactly the forbidden thing:

    except json.JSONDecodeError:
        input_data = {}          # ...and then the tool runs

So a truncated or malformed argument string became an empty dict and the tool was called
anyway. For a tool with required parameters that surfaces as a confusing `TypeError`
("missing 1 required positional argument") attributed to the tool rather than to the
malformed call; for a tool whose parameters are all optional it silently runs with
defaults, doing something the model never asked for. Either way the model is told the
tool failed, not that its own arguments were unusable — so it cannot correct itself.

Validation is driven by the registry's own `input_schema`, so it cannot drift from what
the model was told the tool accepts.

Deliberately **shallow**: required-presence, unknown-name, and `enum` membership. Deep
JSON-Schema type checking is not attempted — the value of this layer is catching the
malformed call and the wrong action name, which is what actually happens in practice,
and a partial type checker that silently ignores half the schema would be worse than an
honest shallow one.
"""
from __future__ import annotations

from typing import Any

# Sentinel placed on a tool call whose arguments could not be parsed at all, so the
# executor can tell "the model sent nothing" from "the model sent garbage".
INVALID_ARGS_KEY = "_invalid_args"


def _schema_for(tool_name: str) -> dict | None:
    from backend.tools.registry import TOOLS

    for tool in TOOLS:
        if tool.get("name") == tool_name:
            return tool.get("input_schema") or {}
    return None


def validate(tool_name: str, args: Any, schema: dict | None = None) -> str | None:
    """Return an error message when the call must not execute, else None.

    An unknown tool is NOT rejected here — dispatch already reports that, and duplicating
    it would produce two different messages for one condition. A tool with no schema is
    accepted: absence of a schema is not evidence of a bad call.

    `schema` validates against a schema that is not in the public registry (G1a's goal
    session tools), with the same rules.
    """
    if not isinstance(args, dict):
        return f"arguments must be a JSON object, got {type(args).__name__}"

    schema = schema if schema is not None else _schema_for(tool_name)
    if schema is None:
        return None                     # unknown tool: dispatch's business, not ours

    properties = schema.get("properties") or {}
    required = schema.get("required") or []

    missing = [name for name in required if name not in args]
    if missing:
        return (f"missing required argument(s): {', '.join(sorted(missing))}. "
                f"Expected: {', '.join(sorted(properties)) or '(none)'}")

    if properties:
        unknown = [name for name in args if name not in properties]
        if unknown:
            return (f"unknown argument(s): {', '.join(sorted(unknown))}. "
                    f"Accepted: {', '.join(sorted(properties))}")

    for name, value in args.items():
        allowed = (properties.get(name) or {}).get("enum")
        if allowed and value not in allowed:
            return (f"invalid value for {name!r}: {value!r}. "
                    f"Must be one of: {', '.join(map(str, allowed))}")

    return None


def rejection_message(tool_name: str, reason: str) -> str:
    """The model-facing text for a rejected call.

    Prefixed `Error:` so it classifies as a failure through the normal path, and phrased
    so the model knows the problem is its OWN arguments and can retry — the previous
    behavior blamed the tool for a call the model had malformed.
    """
    return (f"Error: {tool_name} was not called — {reason}. "
            "Fix the arguments and call it again.")
