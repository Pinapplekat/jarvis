"""A small JSON Schema validator for the subset manifests may use (stdlib only)."""

from __future__ import annotations

import copy

TYPES = {"object", "string", "number", "integer", "boolean", "array"}
KEYWORDS = {
    "type", "properties", "required", "additionalProperties", "enum", "items", "description", "default",
    "minimum", "maximum", "minLength", "maxLength",
}


def check_schema(schema: object, path: str = "input") -> list[str]:
    """Problems with the schema itself: unsupported keywords, bad types, required without properties."""
    if not isinstance(schema, dict):
        return [f"{path}: must be a table"]
    problems = [f"{path}: unsupported keyword '{k}'" for k in sorted(set(schema) - KEYWORDS)]
    kind = schema.get("type")
    if kind not in TYPES:
        problems.append(f"{path}: 'type' must be one of {sorted(TYPES)}")
    props = schema.get("properties", {})
    if not isinstance(props, dict):
        problems.append(f"{path}: 'properties' must be a table")
        props = {}
    for name, sub in props.items():
        problems += check_schema(sub, f"{path}.{name}")
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
        problems.append(f"{path}: 'required' must be a list of names")
    else:
        problems += [f"{path}: required '{r}' is not a property" for r in required if r not in props]
    if "items" in schema:
        problems += check_schema(schema["items"], f"{path}[]")
    if ("properties" in schema or "required" in schema) and kind != "object":
        problems.append(f"{path}: 'properties'/'required' only apply to type object")
    if not isinstance(schema.get("enum", []), list):
        problems.append(f"{path}: 'enum' must be a list")
    return problems


def fill_defaults(schema: dict, value: object) -> object:
    """Return a copy of `value` with `default`s filled in for missing object properties."""
    if isinstance(value, dict) and schema.get("type") == "object":
        out = dict(value)
        for name, sub in schema.get("properties", {}).items():
            if name in out:
                out[name] = fill_defaults(sub, out[name])
            elif "default" in sub:
                out[name] = copy.deepcopy(sub["default"])
        return out
    if isinstance(value, list) and "items" in schema:
        return [fill_defaults(schema["items"], v) for v in value]
    return value


def _is(kind: str, v: object) -> bool:
    if kind == "string":
        return isinstance(v, str)
    if kind == "boolean":
        return isinstance(v, bool)
    if kind == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if kind == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if kind == "array":
        return isinstance(v, list)
    return isinstance(v, dict)


def validate(schema: dict, value: object, path: str = "") -> list[str]:
    """Readable problems with `value` (empty list when valid), e.g. 'expression: required'."""
    where = path or "arguments"
    kind = schema.get("type")
    if kind and not _is(kind, value):
        return [f"{where}: must be {'an' if kind in ('object', 'integer', 'array') else 'a'} {kind}"]
    problems: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{where}: must be one of {', '.join(map(str, schema['enum']))}")
    if kind in ("number", "integer"):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{where}: must be ≥ {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{where}: must be ≤ {schema['maximum']}")
    if kind == "string":
        if "minLength" in schema and len(value) < schema["minLength"]:
            problems.append(f"{where}: must be at least {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append(f"{where}: must be at most {schema['maxLength']} characters")
    if kind == "array" and "items" in schema:
        for i, item in enumerate(value):
            problems += validate(schema["items"], item, f"{path}[{i}]")
    if kind == "object":
        props = schema.get("properties", {})
        dot = f"{path}." if path else ""
        problems += [f"{dot}{r}: required" for r in schema.get("required", []) if r not in value]
        if schema.get("additionalProperties", True) is False:
            problems += [f"{dot}{k}: not allowed" for k in value if k not in props]
        for name, sub in props.items():
            if name in value:
                problems += validate(sub, value[name], f"{dot}{name}")
    return problems
