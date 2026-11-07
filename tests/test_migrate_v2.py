import hashlib
import json
import uuid

import pytest

from app.migrate_v2 import create_candidate, promote_candidate
from app.storage_v2 import V2Store


def test_migration_preserves_source_and_is_deterministic(tmp_path):
    source = tmp_path / "legacy.json"
    film_id = str(uuid.uuid4())
    legacy = {
        "films": [
            {
                "id": film_id,
                "titre": "Étoile",
                "annee": 2001,
                "realisateur": "A",
                "acteurs": ["B"],
                "support": "dvd",
                "synopsis": "Résumé",
            }
        ]
    }
    source.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    before = source.read_bytes()
    candidate1, candidate2 = tmp_path / "candidate1.json", tmp_path / "candidate2.json"
    report = create_candidate(source, candidate1)
    create_candidate(source, candidate2)
    assert source.read_bytes() == before
    assert report["source_sha256"] == hashlib.sha256(before).hexdigest()
    assert candidate1.read_bytes() == candidate2.read_bytes()
    data = json.loads(candidate1.read_text(encoding="utf-8"))
    assert data["movies"][0]["id"] == film_id
    assert data["movies"][0]["title"] == "Étoile"
    assert data["copies"][0]["movie_id"] == film_id
    assert data["copies"][0]["support"] == "dvd"
    target = tmp_path / "store.json"
    promote_candidate(
        candidate1,
        target,
        source=source,
        expected_source_sha256=report["source_sha256"],
        expected_candidate_sha256=report["candidate_sha256"],
    )
    assert V2Store(target).read() == data
    target_before = target.read_bytes()
    with pytest.raises(FileExistsError):
        promote_candidate(
            candidate2,
            target,
            source=source,
            expected_source_sha256=report["source_sha256"],
            expected_candidate_sha256=report["candidate_sha256"],
        )
    with pytest.raises(FileExistsError):
        create_candidate(source, candidate1)
    assert target.read_bytes() == target_before
    assert source.read_bytes() == before


@pytest.mark.parametrize(
    "content", ["{broken", '{"films":[],"films":[]}', '{"films":[{}]}', '{"films":[],"other":1}']
)
def test_invalid_source_produces_no_candidate(tmp_path, content):
    source, candidate = tmp_path / "legacy.json", tmp_path / "candidate.json"
    source.write_text(content, encoding="utf-8")
    before = source.read_bytes()
    with pytest.raises(ValueError):
        create_candidate(source, candidate)
    assert not candidate.exists()
    assert source.read_bytes() == before


def test_invalid_candidate_cannot_be_promoted(tmp_path):
    source, candidate, target = (
        tmp_path / "source.json",
        tmp_path / "candidate.json",
        tmp_path / "target.json",
    )
    source.write_text('{"films":[]}', encoding="utf-8")
    candidate.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError):
        promote_candidate(
            candidate,
            target,
            source=source,
            expected_source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            expected_candidate_sha256=hashlib.sha256(candidate.read_bytes()).hexdigest(),
        )
    assert not target.exists()


def test_changed_source_or_candidate_cannot_be_promoted(tmp_path):
    source, candidate, target = (
        tmp_path / "source.json",
        tmp_path / "candidate.json",
        tmp_path / "target.json",
    )
    source.write_text('{"films":[]}', encoding="utf-8")
    report = create_candidate(source, candidate)
    source.write_text('{ "films": [] }', encoding="utf-8")
    with pytest.raises(ValueError):
        promote_candidate(
            candidate,
            target,
            source=source,
            expected_source_sha256=report["source_sha256"],
            expected_candidate_sha256=report["candidate_sha256"],
        )
    assert not target.exists()
    source.write_text('{"films":[]}', encoding="utf-8")
    candidate.write_bytes(candidate.read_bytes() + b" ")
    with pytest.raises(ValueError):
        promote_candidate(
            candidate,
            target,
            source=source,
            expected_source_sha256=report["source_sha256"],
            expected_candidate_sha256=report["candidate_sha256"],
        )
    assert not target.exists()


def test_empty_catalogue_and_duplicate_ids(tmp_path):
    source, candidate = tmp_path / "source.json", tmp_path / "candidate.json"
    source.write_text('{"films":[]}', encoding="utf-8")
    report = create_candidate(source, candidate)
    assert report["movies"] == report["copies"] == 0
    assert json.loads(candidate.read_text(encoding="utf-8"))["movies"] == []
    film = {
        "id": str(uuid.uuid4()),
        "titre": "T",
        "annee": 2001,
        "realisateur": "R",
        "acteurs": [],
        "support": "dvd",
        "synopsis": "",
    }
    source.write_text(json.dumps({"films": [film, film]}), encoding="utf-8")
    with pytest.raises(ValueError):
        create_candidate(source, tmp_path / "invalid.json")
    assert not (tmp_path / "invalid.json").exists()
