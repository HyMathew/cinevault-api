"""Enterprise account HTTP boundary and transitional document-route helpers."""

import hmac
import json
import secrets
import time
import uuid
from pathlib import Path

import portalocker
from argon2 import PasswordHasher
from flask import Blueprint, current_app, g, jsonify, request
from werkzeug.exceptions import (
    BadRequest,
    RequestEntityTooLarge,
    ServiceUnavailable,
    UnsupportedMediaType,
)

from .circulation_model import PayloadError, RequestBody
from .client_context import InvalidClientContext, client_address, install_client_context
from .persistence.errors import CommitOutcomeUnknown, TransactionUnavailable
from .persistence.json import JsonPersistence
from .registration_admission import RegistrationAdmission, RegistrationBusy
from .repository import unique_pairs
from .services.authentication import (
    AuthenticationService,
    InvalidSession,
    attempt_key,
    normalize_username,
    public_profile,
    session_is_current,
    token_digest,
    valid_display,
    valid_password,
)
from .storage_v2 import V2Store
from .validation import ValidationError

bp = Blueprint("enterprise_auth", __name__, url_prefix="/api/v1")
hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


def error(status, code, message, fields=None):
    value = {"code": code, "message": message}
    if fields is not None:
        value["fields"] = fields
    return jsonify(error=value), status


def payload(required, optional=()):
    if not request.is_json:
        raise UnsupportedMediaType()
    try:
        value = json.loads(
            request.get_data(),
            object_pairs_hook=unique_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise BadRequest("JSON invalide.")
    if (
        not isinstance(value, dict)
        or not set(required).issubset(value)
        or set(value) - set(required) - set(optional)
    ):
        raise ValidationError({"body": "Champs invalides."})
    return value


def capture_body():
    """Defer transport rejection until its historical authorization/payload point."""
    try:
        return RequestBody(request.get_data(), request.is_json)
    except (BadRequest, RequestEntityTooLarge) as exc:
        return RequestBody(b"", request.is_json, error=exc.code)


def transact(operation):
    try:
        return current_app.extensions["enterprise_store"].transact(operation)
    except (OSError, ValueError, RecursionError, portalocker.exceptions.LockException) as exc:
        current_app.logger.error("Stockage Enterprise indisponible : %s", type(exc).__name__)
        raise ServiceUnavailable("Stockage indisponible.") from exc


def check_store():
    try:
        return current_app.extensions["enterprise_store"].read()
    except (OSError, ValueError, RecursionError, portalocker.exceptions.LockException) as exc:
        current_app.logger.error("Stockage Enterprise indisponible : %s", type(exc).__name__)
        raise ServiceUnavailable("Stockage indisponible.") from exc


def now():
    return int(current_app.config.get("AUTH_CLOCK", time.time)())


def bearer():
    value = request.headers.get("Authorization", "")
    parts = value.split(" ")
    return parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1] else ""


def find_session(document, token, timestamp):
    digest = token_digest(token)
    if digest is None:
        return None, None
    for session in document["sessions"]:
        if hmac.compare_digest(session["token_hash"], digest):
            user = next((u for u in document["users"] if u["id"] == session["user_id"]), None)
            if not session_is_current(session, user, timestamp):
                return None, None
            if user:
                session["last_seen"] = timestamp
                return user, session
    return None, None


def unauthorized():
    return error(401, "unauthorized", "Authentification requise ou invalide.")


def protected(operation):
    token, timestamp = bearer(), now()
    if token_digest(token) is None:
        return unauthorized()

    def action(doc):
        user, session = find_session(doc, token, timestamp)
        if user is None:
            raise InvalidSession()
        return operation(doc, user, session)

    try:
        return transact(action)
    except InvalidSession:
        return unauthorized()


def make_user(username, display_name, password, role, timestamp):
    return {
        "id": str(uuid.uuid4()),
        "username": username,
        "display_name": display_name,
        "password_hash": hasher.hash(password),
        "role": role,
        "enabled": True,
        "created_at": timestamp,
    }


@bp.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.before_request
def authenticate_client_context():
    if request.method == "POST" and request.endpoint in (
        "enterprise_auth.register",
        "enterprise_auth.login",
    ):
        try:
            g.auth_client_address = client_address(current_app, now())
        except InvalidClientContext:
            return unauthorized()


def registration_problem(document, username):
    if any(user["username"] == username for user in document["users"]):
        return error(409, "conflict", "Identifiant indisponible.")
    if len(document["users"]) >= 10000:
        return error(503, "registration_capacity", "Inscription temporairement indisponible.")
    return None


@bp.post("/auth/register")
def register():
    data = payload(("username", "display_name", "password"))
    username = normalize_username(data["username"])
    if (
        not username
        or not valid_display(data["display_name"])
        or not valid_password(data["password"])
    ):
        raise ValidationError({"body": "Identifiant, nom ou mot de passe invalide."})
    problem = registration_problem(check_store(), username)
    if problem:
        return problem
    try:
        user = current_app.extensions["registration_admission"].compute(
            limit_key("register-ip:", g.auth_client_address),
            now(),
            lambda: make_user(username, data["display_name"], data["password"], "member", now()),
        )
    except RegistrationBusy as exc:
        response, status = error(429, "rate_limited", "Trop de tentatives d’inscription.")
        response.headers["Retry-After"] = str(exc.retry_after)
        return response, status
    except (OSError, ValueError, RecursionError, portalocker.exceptions.LockException) as exc:
        current_app.logger.error("Admission indisponible : %s", type(exc).__name__)
        raise ServiceUnavailable("Inscription temporairement indisponible.") from exc

    def add(doc):
        problem = registration_problem(doc, username)
        if problem:
            return problem
        doc["users"].append(user)
        return public_profile(user)

    profile = transact(add)
    if not isinstance(profile, dict):
        return profile
    return jsonify(profile), 201


def limit_key(prefix, value):
    key = current_app.extensions["auth_throttle_key"]
    return attempt_key(key, prefix, value)


def authentication_service():
    # Resolve the current crypto dependency per request; startup creates the dummy hash once.
    return AuthenticationService(
        current_app.extensions["enterprise_persistence"],
        hasher=hasher,
        dummy_hash=current_app.extensions["auth_dummy_hash"],
        throttle_key=current_app.extensions["auth_throttle_key"],
    )


def run_service(operation):
    try:
        result = operation()
    except PayloadError as exc:
        if exc.status == 415:
            raise UnsupportedMediaType() from None
        if exc.status == 413:
            raise RequestEntityTooLarge() from None
        raise BadRequest("JSON invalide.") from None
    except CommitOutcomeUnknown:
        current_app.logger.error("Commit Enterprise : issue inconnue")
        response, status = error(
            503, "commit_unknown", "Résultat incertain. Vérifiez l’état avant de réessayer."
        )
        response.status_code = status
        return response
    except TransactionUnavailable:
        current_app.logger.error("Transaction Enterprise indisponible")
        raise ServiceUnavailable("Stockage indisponible.") from None
    except (OSError, ValueError, RecursionError, portalocker.exceptions.LockException) as exc:
        current_app.logger.error("Stockage Enterprise indisponible : %s", type(exc).__name__)
        raise ServiceUnavailable("Stockage indisponible.") from exc
    if result.error is not None:
        response = jsonify(error=result.error)
    elif result.body is not None:
        response = jsonify(result.body)
    else:
        response = current_app.response_class()
    response.status_code = result.status
    response.headers.update(result.headers)
    return response


def render_service(operation):
    return run_service(lambda: operation(authentication_service()))


@bp.post("/auth/login")
def login():
    data = payload(("username", "password"))
    return render_service(
        lambda service: service.login(
            data["username"], data["password"], address=g.auth_client_address, timestamp=now()
        )
    )


@bp.get("/me")
def me():
    return render_service(lambda service: service.me(bearer(), timestamp=now()))


@bp.patch("/me")
def update_me():
    data = payload(("display_name",))
    return render_service(
        lambda service: service.update_me(bearer(), data["display_name"], timestamp=now())
    )


@bp.put("/me/password")
def change_password():
    data = payload(("current_password", "new_password"))
    return render_service(
        lambda service: service.change_password(
            bearer(), data["current_password"], data["new_password"], timestamp=now()
        )
    )


def forbidden():
    return error(403, "forbidden", "Accès interdit.")


@bp.get("/users")
def list_users():
    def listing(doc, user, session):
        if user["role"] != "admin":
            return forbidden()
        try:
            limit = int(request.args.get("limit", "50"))
            offset = int(request.args.get("offset", "0"))
            if (
                not 1 <= limit <= 100
                or not 0 <= offset <= 10000
                or set(request.args) - {"limit", "offset"}
            ):
                raise ValueError()
        except ValueError:
            raise ValidationError({"query": "Pagination invalide."})
        users = sorted(doc["users"], key=lambda u: u["username"])
        return jsonify(
            users=[public_profile(u) for u in users[offset : offset + limit]],
            total=len(users),
            limit=limit,
            offset=offset,
        ), 200

    return protected(listing)


@bp.get("/users/<user_id>")
def get_user(user_id):
    def get(doc, user, session):
        if user["role"] != "admin" and user["id"] != user_id:
            return forbidden()
        target = next((u for u in doc["users"] if u["id"] == user_id), None)
        if target is None:
            return error(404, "not_found", "Compte introuvable.")
        return jsonify(public_profile(target)), 200

    return protected(get)


@bp.patch("/users/<user_id>")
def update_user(user_id):
    timestamp, namespace = now(), uuid.uuid4()

    def update(doc, user, session):
        if user["role"] != "admin":
            return forbidden()
        data = payload((), ("role", "enabled"))
        if (
            not data
            or ("role" in data and data["role"] not in ("member", "manager", "admin"))
            or ("enabled" in data and type(data["enabled"]) is not bool)
        ):
            raise ValidationError({"body": "Rôle ou statut invalide."})
        target = next((u for u in doc["users"] if u["id"] == user_id), None)
        if target is None:
            return error(404, "not_found", "Compte introuvable.")
        candidate = {**target, **data}
        if (
            target["role"] == "admin"
            and target["enabled"]
            and (candidate["role"] != "admin" or not candidate["enabled"])
        ):
            if not any(
                u["id"] != target["id"] and u["role"] == "admin" and u["enabled"]
                for u in doc["users"]
            ):
                return error(
                    409, "last_admin", "Le dernier administrateur actif doit être conservé."
                )
        changed = any(target[k] != v for k, v in data.items())
        target.update(data)
        if not target["enabled"]:
            from .reservation_service import cancel_disabled

            cancel_disabled(doc, target["id"], user["id"], timestamp, namespace=namespace)
        if changed:
            doc["sessions"][:] = [s for s in doc["sessions"] if s["user_id"] != target["id"]]
        return jsonify(public_profile(target)), 200

    return protected(update)


@bp.post("/auth/logout")
def logout():
    return render_service(lambda service: service.logout(bearer(), timestamp=now()))


def install_enterprise(app):
    path = app.config.get("ENTERPRISE_DATA_FILE")
    key = app.config.get("AUTH_THROTTLE_KEY")
    key_file = app.config.get("AUTH_THROTTLE_KEY_FILE")
    if key is None and key_file:
        key = Path(key_file).read_bytes()
    if not path or not isinstance(key, bytes) or len(key) < 32:
        raise RuntimeError("Enterprise requires an existing v2 store and a 32-byte throttle key")
    store = V2Store(path)
    store.read()
    # Transitional document routes and extracted services use the SAME file/lock.
    # MongoDB selection remains disabled until every business route uses ports.
    app.extensions["enterprise_store"] = store
    app.extensions["enterprise_persistence"] = JsonPersistence(path)
    app.extensions["auth_throttle_key"] = key
    app.extensions["auth_dummy_hash"] = hasher.hash(secrets.token_urlsafe(32))
    install_client_context(app)
    admission = RegistrationAdmission(path)
    reserved = {
        Path(str(value) + suffix).resolve()
        for value in (path, app.config["DATA_FILE"])
        for suffix in ("", ".lock")
    }
    if any(Path(value).resolve() in reserved for value in (admission.path, admission.lock_path)):
        raise RuntimeError("Registration admission must be separate from business files and locks")
    app.extensions["registration_admission"] = admission
    app.register_blueprint(bp)
    from .enterprise_catalogue import bp as catalogue_bp

    app.register_blueprint(catalogue_bp)
    from .enterprise_collections import bp as collections_bp

    app.register_blueprint(collections_bp)
    from .enterprise_lending import bp as lending_bp

    app.register_blueprint(lending_bp)
    from .enterprise_reservations import bp as reservations_bp

    app.register_blueprint(reservations_bp)
    from .enterprise_transfer import bp as transfer_bp

    app.register_blueprint(transfer_bp)
    from .enterprise_insights import bp as insights_bp

    app.register_blueprint(insights_bp)
    from .enterprise_metadata import install_metadata

    install_metadata(app)
