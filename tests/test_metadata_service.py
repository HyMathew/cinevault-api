from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_readonly_authorization import NOW, fixtures

from app.persistence.json import JsonPersistence
from app.services.metadata import MetadataService
from app.storage_v2 import V2Store


@pytest.mark.parametrize("demote", (False, True))
def test_retry_rechecks_authority_without_repeating_provider_or_clock(tmp_path, demote):
    class Retry(Exception):
        pass

    path = tmp_path / "catalogue.json"
    V2Store(path).initialize()
    persistence = JsonPersistence(path)
    token, user, session, *_ = fixtures(persistence)
    first, after_wait = NOW + 1, NOW + 2
    clock = Mock(side_effect=[first, after_wait])
    provider = Mock()
    result = {"items": []}
    phases = []
    active = False

    def search(query):
        assert not active
        observed = persistence.query(
            lambda work: work.sessions.by_token_hash(session["token_hash"])
        )
        assert observed["last_seen"] == first
        return result

    def run(command):
        nonlocal active
        phases.append(command)
        active = True
        try:
            if len(phases) == 2:

                def rolled_back_attempt(work):
                    assert command(work).status == 200
                    raise Retry()

                with pytest.raises(Retry):
                    persistence.run(rolled_back_attempt)
                if demote:
                    persistence.run(lambda work: work.accounts.save({**user, "role": "member"}))
            return persistence.run(command)
        finally:
            active = False

    provider.search.side_effect = search
    retrying_persistence = SimpleNamespace(run=run)
    response = MetadataService(retrying_persistence, provider).search(
        token, [("q", "Metropolis")], clock=clock
    )
    assert len(phases) == 2
    assert clock.call_count == 2
    provider.search.assert_called_once_with("Metropolis")
    if demote:
        assert response.status == 403 and response.body is None
        assert response.error["code"] == "forbidden"
    else:
        assert response.status == 200 and response.body == result
    current = persistence.query(lambda work: work.sessions.by_token_hash(session["token_hash"]))
    assert current["last_seen"] == after_wait


def test_posters_reads_only_requested_sources_and_uses_cache_after_query():
    ids = [f"00000000-0000-4000-8000-{value:012d}" for value in range(1, 5)]
    matching, other_revision, unannotated, unknown = ids
    provider = Mock()
    catalogue = Mock(spec_set=["movies_by_ids"])
    catalogue.movies_by_ids.return_value = [
        {"id": matching, "external_source": {"qid": "Q151599", "revision": 1}},
        {"id": other_revision, "external_source": {"qid": "Q151599", "revision": 2}},
        {"id": unannotated},
    ]
    poster = {
        "image_url": "https://thumb.wikimedia.org/wikipedia/commons/thumb/a/ab/Test.jpg/500px-Test.jpg",
        "source_url": "https://commons.wikimedia.org/wiki/File:Test.jpg",
        "artist": "Artist",
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "attribution_required": True,
    }
    revision = 1
    active = False

    def query(callback):
        nonlocal active
        active = True
        try:
            return callback(SimpleNamespace(catalogue=catalogue))
        finally:
            active = False

    def peek_many(keys):
        assert not active
        assert keys == ["detail:Q151599"]
        return {
            "detail:Q151599": {"film": {"id": "Q151599", "revision": revision, "poster": poster}}
        }

    persistence = Mock(spec_set=["query"])
    persistence.query.side_effect = query
    provider.cache.peek_many.side_effect = peek_many
    service = MetadataService(persistence, provider)
    parameters = [("movie_id", identity) for identity in [*ids, matching]]
    assert service.posters(parameters).body == {"posters": {matching: poster}}
    persistence.query.assert_called_once()
    catalogue.movies_by_ids.assert_called_once_with(
        [matching, other_revision, unannotated, unknown]
    )
    provider.cache.peek_many.assert_called_once()
    provider.search.assert_not_called()
    provider.detail.assert_not_called()

    revision = True
    assert service.posters(parameters).body == {"posters": {}}


def test_poster_limit_counts_duplicate_ids_before_reading():
    from app.validation import ValidationError

    persistence = Mock()
    provider = Mock()
    service = MetadataService(persistence, provider)
    repeated = [("movie_id", "00000000-0000-4000-8000-000000000001")] * 101
    with pytest.raises(ValidationError) as error:
        service.posters(repeated)
    assert set(error.value.fields) == {"movie_id"}
    assert not persistence.mock_calls and not provider.mock_calls
