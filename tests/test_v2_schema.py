import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from app.storage_v2 import validate_document


def test_v2_schema_and_example_agree():
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "schemas" / "catalogue-v2.schema.json").read_text(encoding="utf-8"))
    example = json.loads(
        (root / "schemas" / "catalogue-v2.example.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(example)
    assert validate_document(example) == example
    example["users"].append({"id": "unimplemented"})
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(example)
