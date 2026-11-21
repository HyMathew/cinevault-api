import json
from uuid import UUID

from flask import request

from app import create_app


def access_records(stderr):
    return [json.loads(line) for line in stderr.splitlines() if line.startswith("{")]


def assert_access_record(record, response, method, route):
    request_id = response.headers["X-Request-ID"]
    parsed = UUID(hex=request_id)
    assert parsed.version == 4
    assert parsed.hex == request_id
    assert record.keys() == {
        "service",
        "request_id",
        "method",
        "route",
        "status",
        "duration_ms",
    }
    assert record["service"] == "api"
    assert record["request_id"] == request_id
    assert record["method"] == method
    assert record["route"] == route
    assert record["status"] == response.status_code
    assert record["duration_ms"] > 0


def test_health_emits_one_correlated_record_per_request_after_factory_recreation(tmp_path, capfd):
    config = {"TESTING": True, "DATA_FILE": str(tmp_path / "films.json")}
    first = create_app(config).test_client().get("/health")
    second = create_app(config).test_client().get("/health")

    records = access_records(capfd.readouterr().err)
    assert len(records) == 2
    for record, response in zip(records, (first, second), strict=True):
        assert response.status_code == 200
        assert response.json == {"status": "ok"}
        assert_access_record(record, response, "GET", "/health")
    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]


def test_access_records_exclude_request_values_and_preserve_http_errors(tmp_path, capfd):
    app = create_app({"TESTING": True, "DATA_FILE": str(tmp_path / "films.json")})
    client = app.test_client()
    client.set_cookie("session", "synthetic-cookie-marker")
    supplied_id = "c" * 32
    matched = client.get(
        "/api/films/synthetic-path-marker?q=synthetic-query-marker",
        headers={
            "Authorization": "Bearer synthetic-token-marker",
            "X-Request-ID": supplied_id,
        },
    )
    unknown_method = client.open("/api/films", method="SYNTHETIC-METHOD-MARKER")
    unmatched = client.get("/synthetic-unmatched-marker")

    stderr = capfd.readouterr().err
    records = access_records(stderr)
    assert len(records) == 3
    assert matched.status_code == unmatched.status_code == 404
    assert unknown_method.status_code == 405
    assert unknown_method.json["error"]["code"] == "method_not_allowed"
    assert "GET" in unknown_method.headers["Allow"]
    assert_access_record(records[0], matched, "GET", "/api/films/<film_id>")
    assert_access_record(records[1], unknown_method, "OTHER", "unmatched")
    assert_access_record(records[2], unmatched, "GET", "unmatched")
    assert matched.headers["X-Request-ID"] != supplied_id
    for value in (
        "synthetic-path-marker",
        "synthetic-query-marker",
        "synthetic-token-marker",
        "synthetic-cookie-marker",
        "SYNTHETIC-METHOD-MARKER",
        "synthetic-unmatched-marker",
        supplied_id,
    ):
        assert value not in stderr


def test_internal_error_logs_do_not_render_exception_details(tmp_path, capfd, caplog):
    app = create_app({"TESTING": True, "DATA_FILE": str(tmp_path / "films.json")})

    @app.get("/diagnostic-test")
    def fail():
        raise ValueError(request.headers["Authorization"])

    response = app.test_client().get(
        "/diagnostic-test", headers={"Authorization": "Bearer synthetic-exception-marker"}
    )
    assert response.status_code == 500
    assert response.json["error"]["code"] == "internal_error"
    stderr = capfd.readouterr().err
    assert "synthetic-exception-marker" not in stderr
    assert "synthetic-exception-marker" not in caplog.text
    (record,) = access_records(stderr)
    assert_access_record(record, response, "GET", "/diagnostic-test")
