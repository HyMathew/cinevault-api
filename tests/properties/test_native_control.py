"""Command-flow tests only: this recorder is not a MongoDB validation engine."""

import copy
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bson import Int64
from pymongo.errors import OperationFailure

from . import native


class CommandRecorder:
    def __init__(self, *, reject_valid=False, corrupt_read=False, negative_code=121):
        self.reject_valid = reject_valid
        self.corrupt_read = corrupt_read
        self.negative_code = negative_code
        self.calls = []
        self.document = None

    def __getitem__(self, collection):
        assert collection == "users"
        return self

    def insert_one(self, document):
        self.calls.append(("insert", copy.deepcopy(document)))
        if type(document["created_at"]) is Int64:
            if self.reject_valid:
                raise OperationFailure("Synthetic positive rejection", code=121)
            self.document = copy.deepcopy(document)
            return SimpleNamespace(acknowledged=True)
        raise OperationFailure("Synthetic negative rejection", code=self.negative_code)

    def find_one(self, selector):
        self.calls.append(("find", selector.copy()))
        assert selector == {"_id": self.document["_id"]}
        result = copy.deepcopy(self.document)
        if self.corrupt_read:
            result["created_at"] = 0
        return result

    def delete_one(self, selector):
        self.calls.append(("delete", selector.copy()))
        assert set(selector) == {"_id"} and type(selector["_id"]) is str
        assert str(uuid.UUID(selector["_id"])) == selector["_id"]
        deleted = int(self.document is not None and self.document["_id"] == selector["_id"])
        if deleted:
            self.document = None
        return SimpleNamespace(deleted_count=deleted, acknowledged=True)


def invoke(recorder):
    # Only orchestration is under test. Real sequences/validators are separately
    # exercised on JSON and, when qualified by its owner, native MongoDB.
    with (
        patch.object(native, "MongoPersistence", return_value=object()),
        patch.object(native, "run_corpus", return_value={"sequences_completed": 8}),
    ):
        return native.exercise_corpus(recorder, {})


def test_positive_read_control_precedes_the_three_negative_attempts_and_cleans_each_uuid():
    recorder = CommandRecorder()
    result = invoke(recorder)
    assert result.get("positive_control_native_valid_insert") is True
    assert result["native_invalid_integer_field_types_rejected_121"] == 3
    assert [call[0] for call in recorder.calls] == [
        "insert",
        "find",
        "delete",
        "insert",
        "delete",
        "insert",
        "delete",
        "insert",
        "delete",
    ]
    documents = [value for operation, value in recorder.calls if operation == "insert"]
    assert len({document["_id"] for document in documents}) == 4
    positive = documents[0]
    assert type(positive["created_at"]) is Int64
    for negative in documents[1:]:
        assert {k: v for k, v in negative.items() if k not in ("_id", "created_at")} == {
            k: v for k, v in positive.items() if k not in ("_id", "created_at")
        }
    assert {value["_id"] for operation, value in recorder.calls if operation == "delete"} == {
        document["_id"] for document in documents
    }
    assert recorder.document is None


def test_rejected_positive_control_aborts_before_any_negative_and_attempts_targeted_cleanup():
    recorder = CommandRecorder(reject_valid=True)
    with pytest.raises(OperationFailure) as caught:
        invoke(recorder)
    assert caught.value.code == 121
    assert [call[0] for call in recorder.calls] == ["insert", "delete"]


def test_wrong_readback_type_aborts_and_cleans_positive_control():
    recorder = CommandRecorder(corrupt_read=True)
    with pytest.raises(AssertionError, match="Positive control"):
        invoke(recorder)
    assert [call[0] for call in recorder.calls] == ["insert", "find", "delete"]
    assert recorder.document is None


def test_non_validation_negative_error_is_not_claimed_as_code121_proof():
    recorder = CommandRecorder(negative_code=13)
    with pytest.raises(AssertionError, match="Expected native field validation"):
        invoke(recorder)
    assert [call[0] for call in recorder.calls] == ["insert", "find", "delete", "insert", "delete"]
