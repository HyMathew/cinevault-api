"""Authenticate a web service's assertion of its observed client address."""

import hashlib
import hmac
import ipaddress
import os
import re
from pathlib import Path

from flask import request

HEADERS = (
    "X-CineVault-Client-IP",
    "X-CineVault-Client-Time",
    "X-CineVault-Client-Signature",
)


class InvalidClientContext(Exception):
    pass


def install_client_context(app):
    key = app.config.get("CLIENT_CONTEXT_KEY")
    filename = app.config.get("CLIENT_CONTEXT_KEY_FILE") or os.environ.get(
        "CLIENT_CONTEXT_KEY_FILE"
    )
    if key is None and filename:
        key = Path(filename).read_bytes()
    required = app.config.get(
        "CLIENT_CONTEXT_REQUIRED", os.environ.get("CLIENT_CONTEXT_REQUIRED", "").lower() == "true"
    )
    if (
        type(required) is not bool
        or (key is not None and (not isinstance(key, bytes) or len(key) < 32))
        or (required and key is None)
    ):
        raise RuntimeError("Client context requires a valid policy and a separate 32-byte key")
    app.extensions["client_context_key"] = key
    app.extensions["client_context_required"] = required


def client_address(app, timestamp):
    supplied = [request.headers.get(name) for name in HEADERS]
    if not any(value is not None for value in supplied):
        if app.extensions["client_context_required"]:
            raise InvalidClientContext()
        return request.remote_addr or "unknown"
    key = app.extensions["client_context_key"]
    if key is None or any(value is None for value in supplied):
        raise InvalidClientContext()
    address, issued, signature = supplied
    try:
        if (
            len(address) > 45
            or "%" in address
            or str(ipaddress.ip_address(address)) != address
            or not re.fullmatch(r"[0-9]{1,10}", issued)
            or str(int(issued)) != issued
            or abs(timestamp - int(issued)) > 30
            or not re.fullmatch(r"[0-9a-f]{64}", signature)
        ):
            raise InvalidClientContext()
        message = "\n".join(
            (
                "cinevault-client-v1",
                issued,
                address,
                request.method,
                request.path,
                hashlib.sha256(request.get_data()).hexdigest(),
            )
        )
        expected = hmac.new(key, message.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise InvalidClientContext()
    except (ValueError, UnicodeError):
        raise InvalidClientContext() from None
    return address
