"""Pure circulation inputs must preserve HTTP rejection order and determinism."""

import importlib
import importlib.util
import uuid

import pytest

from app.validation import ValidationError


def model():
    assert importlib.util.find_spec("app.circulation_model") is not None, (
        "Shared pure circulation values are required"
    )
    return importlib.import_module("app.circulation_model")


def test_transport_payload_rejections_are_exceptions_not_committable_results():
    module = model()
    for raw, media, status in (
        (b"{}", False, 415),
        (b"{", True, 400),
        (b'{"movie_id":"a","movie_id":"b"}', True, 400),
        (b'{"movie_id":NaN}', True, 400),
    ):
        with pytest.raises(module.PayloadError) as caught:
            module.payload(module.RequestBody(raw, media), ("movie_id",))
        assert caught.value.status == status
    with pytest.raises(ValidationError):
        module.payload({}, ("movie_id",))
    assert module.payload({"movie_id": "abc"}, ("movie_id",)) == {"movie_id": "abc"}


def test_scope_refusal_precedes_bad_pagination_and_duplicate_parameters_are_rejected():
    module = model()
    with pytest.raises(module.ForbiddenScope):
        module.list_query([("scope", "all"), ("limit", "bad")], "loan", "member")
    with pytest.raises(ValidationError):
        module.list_query([("scope", "mine"), ("scope", "mine")], "loan", "member")
    query = module.list_query([("movie_id", "not-an-id")], "loan", "member")
    assert (query.scope, query.movie_id, query.limit, query.offset) == ("mine", "not-an-id", 24, 0)


def test_unknown_expiration_count_uses_stable_action_and_entity_scoped_ids():
    module = model()
    namespace = uuid.UUID(int=1)
    first = module.derived_event_id(namespace, "reservation.expired", str(uuid.UUID(int=2)))
    assert str(uuid.UUID(first)) == first
    assert first == module.derived_event_id(namespace, "reservation.expired", str(uuid.UUID(int=2)))
    assert first != module.derived_event_id(namespace, "reservation.expired", str(uuid.UUID(int=3)))
    assert first != module.derived_event_id(
        namespace, "reservation.cancelled", str(uuid.UUID(int=2))
    )
    with pytest.raises(ValueError):
        module.derived_event_id(namespace, "client-chosen-action", str(uuid.UUID(int=2)))


def test_fifo_allows_queued_head_only_and_nonqueued_surplus_strictly():
    module = model()
    assert module.priority_allowed(owned=True, is_head=False, free=4, reserved=2) is False
    assert module.priority_allowed(owned=True, is_head=True, free=1, reserved=2) is True
    assert module.priority_allowed(owned=False, is_head=False, free=2, reserved=2) is False
    assert module.priority_allowed(owned=False, is_head=False, free=3, reserved=2) is True
