from pathlib import Path
from unittest.mock import patch

import pytest


def mongo_config(tmp_path, uri=None):
    path = tmp_path / "mongo-uri"
    path.write_text(
        uri
        or "mongodb://cinevault:synthetic@127.0.0.1:27017/"
        "?authSource=admin&authMechanism=SCRAM-SHA-256&replicaSet=cv&directConnection=true",
        encoding="utf-8",
    )
    return {
        "ENTERPRISE_ENABLED": True,
        "STORAGE_BACKEND": "mongodb",
        "MONGODB_URI_FILE": str(path),
        "MONGODB_DATABASE": "cinevault",
    }


def test_mongo_requires_explicit_enterprise_without_a_json_candidate(tmp_path):
    from app.persistence.factory import storage_backend

    cfg = mongo_config(tmp_path)
    assert storage_backend(cfg) == "mongodb"
    for invalid in (
        {**cfg, "ENTERPRISE_ENABLED": False},
        {**cfg, "ENTERPRISE_DATA_FILE": "forbidden.json"},
        {**cfg, "MONGODB_URI_FILE": None},
        {**cfg, "MONGODB_DATABASE": "admin"},
        {**cfg, "STORAGE_BACKEND": "typo"},
        {**cfg, "STORAGE_BACKEND": "json"},
    ):
        with pytest.raises(RuntimeError):
            storage_backend(invalid)


def test_factory_opens_only_authenticated_local_replica_set(tmp_path):
    from app.persistence.factory import open_mongo_persistence

    cfg = mongo_config(tmp_path)
    with patch("pymongo.MongoClient") as factory:
        factory.return_value.__getitem__.return_value.client = factory.return_value
        persistence = open_mongo_persistence(cfg)
        (uri,) = factory.call_args.args
        assert uri == Path(cfg["MONGODB_URI_FILE"]).read_text(encoding="utf-8")
        assert factory.call_args.kwargs["connect"] is False
        assert factory.call_args.kwargs["serverSelectionTimeoutMS"] == 5000
        factory.return_value.__getitem__.assert_called_once_with("cinevault")
        persistence.close()
        factory.return_value.close.assert_called_once_with()


@pytest.mark.parametrize(
    "uri",
    [
        "mongodb+srv://cinevault:sentinel@cluster.invalid/?replicaSet=cv",
        "mongodb://cinevault:sentinel@localhost/other?replicaSet=cv&authSource=admin",
        "mongodb://localhost/?replicaSet=cv&authSource=admin",
        "mongodb://cinevault:sentinel@localhost/?authSource=admin",
        "mongodb://cinevault:sentinel@localhost/?replicaSet=cv&authSource=admin&authSource=local",
        "mongodb://cinevault:sentinel@host.invalid/?replicaSet=cv&authSource=admin&authMechanism=SCRAM-SHA-256&directConnection=true",
        "mongodb://cinevault:sentinel@127.0.0.1/?replicaSet=cv&authSource=admin&authMechanism=SCRAM-SHA-256",
        "mongodb://cinevault:sentinel@host.invalid/?replicaSet=cv&authSource=admin&tls=true&tlsAllowInvalidCertificates=true",
        "mongodb://cinevault:sentinel@localhost/?replicaSet=cv&authSource=admin&readPreference=secondary",
    ],
)
def test_mongo_invalid_credentials_fail_closed_without_opening_a_client(tmp_path, uri):
    from app.persistence.factory import open_mongo_persistence

    with patch("pymongo.MongoClient") as factory:
        with pytest.raises(RuntimeError) as error:
            open_mongo_persistence(mongo_config(tmp_path, uri))
        assert "sentinel" not in str(error.value)
        factory.assert_not_called()


def test_mongo_remote_requires_verified_tls(tmp_path):
    from app.persistence.factory import open_mongo_persistence

    cfg = mongo_config(
        tmp_path,
        "mongodb://cinevault:synthetic@mongo.invalid/"
        "?authSource=admin&authMechanism=SCRAM-SHA-256&replicaSet=cv&tls=true",
    )
    with patch("pymongo.MongoClient") as factory:
        factory.return_value.__getitem__.return_value.client = factory.return_value
        open_mongo_persistence(cfg).close()
        factory.assert_called_once()


def test_mongo_http_health_and_routes_never_open_a_json_store(tmp_path):
    from app import create_app
    from app.persistence.errors import TransactionUnavailable

    cfg = {
        **mongo_config(tmp_path),
        "DATA_FILE": str(tmp_path / "legacy.json"),
        "AUTH_THROTTLE_KEY": b"x" * 32,
        "REGISTRATION_ADMISSION_FILE": str(tmp_path / "admission.json"),
    }
    with (
        patch("app.Repository", side_effect=AssertionError("legacy store opened")) as legacy,
        patch(
            "app.enterprise_auth.JsonPersistence", side_effect=AssertionError("JSON fallback")
        ) as json,
        patch("app.enterprise_auth.open_mongo_persistence") as mongo,
    ):
        app = create_app(cfg)
        assert "enterprise_store" not in app.extensions
        assert app.extensions["enterprise_persistence"] is mongo.return_value
        with app.test_client() as client:
            assert client.get("/health").json == {"status": "ok"}
            mongo.return_value.readiness.assert_called_once_with()
            response = client.get("/api/films")
            assert (
                response.status_code == 410
                and response.json["error"]["code"] == "legacy_unavailable"
            )
            assert client.post("/api/films", json={}).status_code == 403
            mongo.return_value.readiness.side_effect = TransactionUnavailable("synthetic")
            assert client.get("/health").status_code == 503
            mongo.return_value.query.side_effect = TransactionUnavailable("synthetic")
            assert client.get("/api/v1/movies").status_code == 503
        legacy.assert_not_called()
        json.assert_not_called()
    assert not (tmp_path / "legacy.json").exists()


def test_mongo_requires_independent_admission_and_enabled_cache_paths(tmp_path):
    from app import create_app

    cfg = {**mongo_config(tmp_path), "AUTH_THROTTLE_KEY": b"x" * 32}
    with patch("app.enterprise_auth.open_mongo_persistence") as mongo:
        with pytest.raises(RuntimeError, match="admission"):
            create_app(cfg)
        cfg["REGISTRATION_ADMISSION_FILE"] = str(tmp_path / "admission.json")
        with pytest.raises(RuntimeError, match="cache"):
            create_app({**cfg, "METADATA_ENABLED": True})
        with pytest.raises(RuntimeError, match="admission"):
            create_app({**cfg, "REGISTRATION_ADMISSION_FILE": cfg["MONGODB_URI_FILE"]})
        mongo.assert_not_called()
