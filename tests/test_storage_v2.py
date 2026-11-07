import hashlib
import json
import subprocess
import sys
import uuid

import pytest

from app.storage_v2 import V2Store


def test_explicit_initialization_and_missing_file(tmp_path):
    path = tmp_path / "store.json"
    store = V2Store(path)
    with pytest.raises(FileNotFoundError):
        store.read()
    assert not path.exists()
    store.initialize()
    value = store.read()
    assert value["schema_version"] == 2
    assert value["revision"] == 0
    with pytest.raises(FileExistsError):
        store.initialize()
    assert json.loads(path.read_text(encoding="utf-8")) == value


def _film():
    return {
        "id": str(uuid.uuid4()),
        "title": "Étoile",
        "year": 2001,
        "director": "A",
        "actors": [],
        "synopsis": "",
        "genres": [],
    }


def _copy(movie_id):
    return {
        "id": str(uuid.uuid4()),
        "movie_id": movie_id,
        "support": "dvd",
        "location": "",
        "condition": "unknown",
    }


def test_transaction_validates_relations_and_is_atomic(tmp_path):
    store = V2Store(tmp_path / "data.json")
    store.initialize()
    movie = _film()

    def add(document):
        document["movies"].append(movie)
        document["copies"].append(_copy(movie["id"]))
        return "ok"

    assert store.transact(add) == "ok"
    assert store.read()["revision"] == 1
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.transact(lambda doc: doc["copies"].append(_copy(str(uuid.uuid4()))))
    assert store.path.read_bytes() == before
    with pytest.raises(RuntimeError):
        store.transact(lambda doc: (_ for _ in ()).throw(RuntimeError("abort")))
    assert store.path.read_bytes() == before


@pytest.mark.parametrize(
    "bad",
    [
        {"schema_version": 3},
        {"revision": True},
        {"unexpected": 1},
        {"users": [{}]},
    ],
)
def test_corrupt_or_unsupported_documents_fail_closed(tmp_path, bad):
    store = V2Store(tmp_path / "data.json")
    store.initialize()
    data = store.read()
    data.update(bad)
    store.path.write_text(json.dumps(data), encoding="utf-8")
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.read()
    with pytest.raises(ValueError):
        store.transact(lambda doc: None)
    assert store.path.read_bytes() == before


def test_snapshot_and_restore_are_verified_and_never_clobber(tmp_path):
    source = V2Store(tmp_path / "source.json")
    source.initialize()
    snap = tmp_path / "backup.json"
    digest = source.snapshot(snap)
    assert digest == hashlib.sha256(snap.read_bytes()).hexdigest()
    restored = V2Store(tmp_path / "restored.json")
    with pytest.raises(ValueError):
        restored.restore(snap, "0" * 64)
    assert not restored.path.exists()
    restored.restore(snap, digest)
    assert restored.read() == source.read()
    with pytest.raises(FileExistsError):
        restored.restore(snap, digest)
    with pytest.raises(FileExistsError):
        source.snapshot(snap)


def test_failed_replace_leaves_original_and_no_temporary_file(tmp_path, monkeypatch):
    store = V2Store(tmp_path / "data.json")
    store.initialize()
    original = store.path.read_bytes()

    def fail(*args):
        raise OSError("simulated interrupted write")

    monkeypatch.setattr("app.storage_v2.os.replace", fail)
    with pytest.raises(OSError):
        store.transact(lambda doc: doc["movies"].append(_film()))
    assert store.path.read_bytes() == original
    assert not list(tmp_path.glob(".cinevault-v2-*"))


def test_concurrent_processes_do_not_lose_updates(tmp_path):
    path = tmp_path / "data.json"
    V2Store(path).initialize()
    code = """
import sys, uuid
from app.storage_v2 import V2Store
s = V2Store(sys.argv[1])
for n in range(8):
    movie = {'id':str(uuid.uuid4()), 'title':sys.argv[2] + str(n), 'year':2001, 'director':'R', 'actors':[], 'synopsis':'', 'genres':[]}
    s.transact(lambda doc: doc['movies'].append(movie))
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(path), str(i)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for i in range(4)
    ]
    for process in processes:
        out, err = process.communicate(timeout=60)
        assert process.returncode == 0, (out, err)
    data = V2Store(path).read()
    assert data["revision"] == 32
    assert len(data["movies"]) == 32
    assert len({m["id"] for m in data["movies"]}) == 32


def test_nested_invalid_copy_reference_is_rejected(tmp_path):
    store = V2Store(tmp_path / "data.json")
    store.initialize()
    original = store.path.read_bytes()
    copy = _copy(str(uuid.uuid4()))
    copy["movie_id"] = []
    with pytest.raises(ValueError):
        store.transact(lambda doc: doc["copies"].append(copy))
    assert store.path.read_bytes() == original
