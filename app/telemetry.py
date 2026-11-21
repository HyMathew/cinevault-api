import json
import logging
from time import perf_counter_ns
from uuid import uuid4

from flask import g, request
from flask.logging import wsgi_errors_stream

HTTP_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})

ACCESS_LOG = logging.getLogger("cinevault.api.access")
ACCESS_LOG.setLevel(logging.INFO)
ACCESS_LOG.propagate = False
ACCESS_LOG.addHandler(logging.StreamHandler(wsgi_errors_stream))


def install_request_telemetry(app):
    @app.before_request
    def start_request():
        g.request_started_ns = perf_counter_ns()
        g.request_id = uuid4().hex

    @app.after_request
    def record_response(response):
        duration_ms = (perf_counter_ns() - g.request_started_ns) / 1_000_000
        response.headers["X-Request-ID"] = g.request_id
        event = {
            "service": "api",
            "request_id": g.request_id,
            "method": request.method if request.method in HTTP_METHODS else "OTHER",
            "route": request.url_rule.rule if request.url_rule is not None else "unmatched",
            "status": response.status_code,
            "duration_ms": duration_ms,
        }
        ACCESS_LOG.info("%s", json.dumps(event, separators=(",", ":"), sort_keys=True))
        return response
