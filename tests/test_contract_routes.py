import json
from pathlib import Path

from test_enterprise_auth import enterprise as enterprise

from scripts import check_docs


def specification():
    path = Path(__file__).resolve().parents[1] / "docs/openapi-v1.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_contract_rejects_legal_but_unimplemented_trace(enterprise):
    client, _ = enterprise
    spec = specification()
    assert check_docs.route_errors(client.application, spec) == []
    spec["paths"]["/api/v1/absent-review-only"] = {
        "trace": {"responses": {"200": {"description": "Example"}}}
    }
    errors = check_docs.route_errors(client.application, spec)
    assert any("TRACE" in item and "absent-review-only" in item for item in errors)


def test_registered_methods_include_trace_explicit_head_and_options(enterprise):
    client, _ = enterprise
    app = client.application
    spec = specification()
    app.add_url_rule("/api/v1/review-trace", "review_trace", lambda: {}, methods=["TRACE"])
    app.add_url_rule("/api/v1/review-head", "review_head", lambda: {}, methods=["HEAD"])
    app.add_url_rule("/api/v1/review-options", "review_options", lambda: {}, methods=["OPTIONS"])
    errors = check_docs.route_errors(app, spec)
    for method in ("TRACE", "HEAD", "OPTIONS"):
        assert any(method in item for item in errors)
    for method in ("trace", "head", "options"):
        spec["paths"]["/api/v1/review-" + method] = {
            method: {"responses": {"200": {"description": "Example"}}}
        }
    # Automatically provided HEAD/OPTIONS for existing GET routes need no declaration.
    assert check_docs.route_errors(app, spec) == []
