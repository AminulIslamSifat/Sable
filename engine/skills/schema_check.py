"""Lightweight JSON-Schema validator for tool arguments.

We don't pull in `jsonschema` — we only need the subset the tool schemas use:

    type, required, enum, const, minimum, maximum,
    minItems, maxItems, oneOf, properties

The point is to turn *runtime* handler errors ("missing x", "unknown action")
into *schema* errors emitted before the handler runs, with a message that tells
both the user and the model exactly what was wrong.

Design notes:
  - Never raises. Returns (ok, [error strings]).
  - Tolerant of unknown schema keywords (ignored).
  - oneOf is checked as: at least one branch validates. If a branch fails
    only because of a *missing* field, we surface that as the hint, since
    that's the most actionable message for the model.
"""

from __future__ import annotations

from typing import Any

_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def _type_ok(value: Any, expected: str) -> bool:
    """Type check with coercion.

    IMPORTANT: tag attrs reach the pipeline as *strings* because the parser
    runs every param through _stringify_params(). So a schema-valid
    ``timeout: 30`` arrives as ``"30"``. We must accept stringified
    primitives or every typed field would be falsely rejected.
    """
    if expected == "integer":
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        if isinstance(value, str):
            try:
                int(value.strip())
                return True
            except (ValueError, AttributeError):
                return False
        return False

    if expected == "number":
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float)):
            return True
        if isinstance(value, str):
            try:
                float(value.strip())
                return True
            except (ValueError, AttributeError):
                return False
        return False

    if expected == "boolean":
        if isinstance(value, bool):
            return True
        if isinstance(value, str):
            return value.strip().lower() in ("true", "false", "1", "0", "yes", "no")
        return False

    if expected == "array":
        if isinstance(value, list):
            return True
        if isinstance(value, str):
            s = value.strip()
            return s.startswith("[") and s.endswith("]")
        return False

    if expected == "object":
        if isinstance(value, dict):
            return True
        if isinstance(value, str):
            s = value.strip()
            return s.startswith("{") and s.endswith("}")
        return False

    py = _TYPE_MAP.get(expected)
    if py is None:
        return True  # unknown type -> don't block
    return isinstance(value, py)


def _coerce(value: Any, expected: str) -> Any:
    """Coerce a stringified attr back to its schema type for numeric bounds."""
    if not isinstance(value, str):
        return value
    s = value.strip()
    if expected == "integer":
        try:
            return int(s)
        except ValueError:
            return value
    if expected == "number":
        try:
            return float(s)
        except ValueError:
            return value
    if expected == "array":
        import json
        try:
            return json.loads(s)
        except (ValueError, TypeError):
            return value
    return value


def _check_branch(value: Any, schema: dict[str, Any]) -> list[str]:
    """Validate value against a single schema object. Returns error list."""
    errors: list[str] = []

    expected = schema.get("type")
    if expected and not _type_ok(value, expected):
        return [f"expected {expected}, got {type(value).__name__}"]

    if "const" in schema and value != schema["const"]:
        errors.append(f"must be {schema['const']!r}")

    enum = schema.get("enum")
    if enum is not None and value not in enum:
        errors.append(f"must be one of {enum}")

    # Coerce stringified attrs before numeric / array bounds checks
    coerced = _coerce(value, schema.get("type", ""))

    if isinstance(coerced, (int, float)) and not isinstance(coerced, bool):
        if "minimum" in schema and coerced < schema["minimum"]:
            errors.append(f"must be >= {schema['minimum']}")
        if "maximum" in schema and coerced > schema["maximum"]:
            errors.append(f"must be <= {schema['maximum']}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"must be at least {schema['minLength']} chars")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"must be at most {schema['maxLength']} chars")

    if isinstance(coerced, list):
        if "minItems" in schema and len(coerced) < schema["minItems"]:
            errors.append(f"needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(coerced) > schema["maxItems"]:
            errors.append(f"allows at most {schema['maxItems']} items")

    return errors


def _missing_required(value: Any, schema: dict[str, Any]) -> list[str]:
    """Which required keys are absent from a dict value."""
    if not isinstance(value, dict):
        return []
    required = schema.get("required") or []
    return [k for k in required if value.get(k) in (None, "")]


def validate_arguments(args: dict[str, Any], schema: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validate *args* against a tool's ``parameters`` schema.

    Returns (ok, errors). Empty errors + True means it passed.
    """
    if not isinstance(schema, dict):
        return True, []

    # --- top-level required ---
    missing = _missing_required(args, schema)
    if missing:
        return False, [f"missing required argument(s): {', '.join(missing)}"]

    # --- oneOf: a branch only applies if its const/enum constraints match ---
    one_of = schema.get("oneOf")
    if isinstance(one_of, list) and one_of:
        applicable = [
            b for b in one_of
            if isinstance(b, dict) and _branch_claims(args, b)
        ]

        if not applicable:
            # No branch's discriminator matched the args
            seen = ", ".join(
                _branch_label(b) for b in one_of if isinstance(b, dict)
            )
            return False, [f"invalid argument combination — expected one of: {seen}"]

        # A branch claimed the call. Its required[] must now be satisfied.
        for branch in applicable:
            missing = _missing_required(args, branch)
            if missing:
                return False, [
                    f"for {_branch_label(branch)}: missing required argument(s): "
                    + ", ".join(missing)
                ]
            # required present — validate the values
            errs: list[str] = []
            for key, sub in (branch.get("properties") or {}).items():
                if key in args and isinstance(sub, dict):
                    for err in _check_branch(args[key], sub):
                        errs.append(f"'{key}': {err}")
            if errs:
                return False, errs
            return True, []

    # --- per-property checks ---
    props = schema.get("properties") or {}
    errors: list[str] = []
    for key, subschema in props.items():
        if key not in args or not isinstance(subschema, dict):
            continue
        for err in _check_branch(args[key], subschema):
            errors.append(f"'{key}': {err}")

    return (not errors), errors


def _branch_claims(args: dict[str, Any], branch: dict[str, Any]) -> bool:
    """Return True if the branch's const/enum constraints match the args.

    This is the discriminator check. Without it, a branch whose only required
    key is the discriminator itself (e.g. required: ["action"]) would match
    every call regardless of the action's value.
    """
    props = branch.get("properties") or {}
    for key, sub in props.items():
        if not isinstance(sub, dict) or key not in args:
            continue
        val = args[key]
        if "const" in sub and val != sub["const"]:
            return False
        if "enum" in sub and val not in sub["enum"]:
            return False
    return True


def _branch_label(branch: dict[str, Any]) -> str:
    """Best-effort label for a oneOf branch, from its const/enum on 'action'."""
    props = branch.get("properties") or {}
    action = props.get("action") or {}
    if "const" in action:
        return str(action["const"])
    if "enum" in action:
        return "/".join(str(x) for x in action["enum"])
    return "branch"
