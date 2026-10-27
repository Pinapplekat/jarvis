import pytest

from jarvis.contract.schema import check_schema, fill_defaults, validate

SCHEMA = {
    "type": "object",
    "required": ["expression"],
    "additionalProperties": False,
    "properties": {
        "expression": {"type": "string", "minLength": 1, "maxLength": 5},
        "precision": {"type": "integer", "minimum": 1, "maximum": 20, "default": 12},
        "mode": {"type": "string", "enum": ["a", "b"]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "ratio": {"type": "number"},
        "flag": {"type": "boolean"},
    },
}


def test_valid_and_defaults():
    value = fill_defaults(SCHEMA, {"expression": "1+1"})
    assert value == {"expression": "1+1", "precision": 12}
    assert validate(SCHEMA, value) == []


@pytest.mark.parametrize("value,problem", [
    ({}, "expression: required"),
    ({"expression": 3}, "expression: must be a string"),
    ({"expression": ""}, "expression: must be at least 1 characters"),
    ({"expression": "toolong"}, "expression: must be at most 5 characters"),
    ({"expression": "x", "precision": 21}, "precision: must be ≤ 20"),
    ({"expression": "x", "precision": 0}, "precision: must be ≥ 1"),
    ({"expression": "x", "precision": 1.5}, "precision: must be an integer"),
    ({"expression": "x", "precision": True}, "precision: must be an integer"),
    ({"expression": "x", "mode": "c"}, "mode: must be one of a, b"),
    ({"expression": "x", "tags": ["a", 1]}, "tags[1]: must be a string"),
    ({"expression": "x", "extra": 1}, "extra: not allowed"),
    ({"expression": "x", "ratio": "1"}, "ratio: must be a number"),
    ({"expression": "x", "flag": 1}, "flag: must be a boolean"),
    ("nope", "arguments: must be an object"),
])
def test_problems(value, problem):
    assert problem in validate(SCHEMA, value)


def test_extra_properties_allowed_by_default():
    assert validate({"type": "object", "properties": {}}, {"x": 1}) == []


def test_numbers_accept_ints():
    assert validate({"type": "number"}, 3) == []


def test_nested_paths():
    nested = {"type": "object", "properties": {"a": {"type": "object", "required": ["b"], "properties": {"b": {}}}}}
    assert validate(nested, {"a": {}}) == ["a.b: required"]


@pytest.mark.parametrize("schema,fragment", [
    ({"type": "object", "pattern": "x"}, "unsupported keyword 'pattern'"),
    ({"type": "object", "properties": {"a": {"type": "string", "format": "uri"}}}, "input.a: unsupported keyword"),
    ({"type": "object", "required": ["z"]}, "'z' is not a property"),
    ({"type": "thing"}, "'type' must be one of"),
    ({"type": "array", "items": {"type": "string", "oneOf": []}}, "unsupported keyword 'oneOf'"),
])
def test_check_schema_rejects(schema, fragment):
    assert any(fragment in p for p in check_schema(schema))


def test_check_schema_accepts_supported_subset():
    assert check_schema(SCHEMA) == []
